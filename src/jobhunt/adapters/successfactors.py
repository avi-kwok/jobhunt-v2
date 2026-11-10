"""SAP SuccessFactors Recruiting Marketing (career site) adapter — public HTML search.

Sites like jobs.rogers.com / careers.telus.com / jobs.scotiabank.com serve
``/search/?q=<term>&startrow=<n>`` as server-rendered HTML, 25 rows per page. We use
that page (robots.txt allows /search/ but disallows /services/, so the RSS feed is off
limits) and parse it with the stdlib HTMLParser.

Config mapping: ``host`` = career-site hostname (e.g. ``jobs.rogers.com``); ``slug``
and ``site`` are unused. ``host`` is also the per-host politeness key.

Efficiency: instead of paging the whole board we union the searches in ``SEARCH_TERMS``
(intern / co-op / student), each paged by ``startrow`` up to ``MAX_PAGES``, deduped by
the numeric job id at the end of the posting URL. Search is fuzzy (it also matches
descriptions), so some results are not internships; the downstream title filter handles
that. The list page has no description, so ``description`` is left empty.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from html.parser import HTMLParser

from ..models import CompanyConfig, Job
from .base import Adapter, register, request_with_retry

SEARCH_TERMS = ("intern", "co-op", "student")
PAGE_SIZE = 25
MAX_PAGES = 10  # per search (=250 rows)
_HEADERS = {"Accept": "text/html,application/xhtml+xml"}
_ID_RE = re.compile(r"/(\d+)/?$")
# Structural markers: the results label (pages with >=1 hit) or the RMK keyword search box
# (also on zero-hit pages, which carry no paginationLabel).
_MARKERS = ("paginationLabel", "keywordsearch-q")
_TOTAL_RE = re.compile(r"of\s*<b>\s*([\d,]+)\s*</b>", re.IGNORECASE)


class _ResultsParser(HTMLParser):
    """Collect (href, title, location, date) from each ``tr.data-row``."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[dict[str, str]] = []
        self._row: dict[str, str] | None = None
        self._field: str | None = None  # "title" | "location" | "date"
        self._buf: list[str] = []
        self._span_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        a = dict(attrs)
        cls = (a.get("class") or "").split()
        if tag == "tr" and "data-row" in cls:
            self._row = {}
            return
        if self._row is None:
            return
        if tag == "a" and "jobTitle-link" in cls and "href" not in self._row:
            self._row["href"] = a.get("href") or ""
            self._field, self._buf = "title", []
        elif tag == "td" and "colLocation" in cls:
            self._field, self._buf = "location", []
        elif tag == "td" and "colDate" in cls:
            self._field, self._buf = "date", []

    def handle_endtag(self, tag: str) -> None:
        if self._row is None:
            return
        if tag == "a" and self._field == "title":
            self._finish()
        elif tag == "td" and self._field in ("location", "date"):
            self._finish()
        elif tag == "tr":
            if self._row.get("href"):
                self.rows.append(self._row)
            self._row, self._field = None, None

    def handle_data(self, data: str) -> None:
        if self._field:
            self._buf.append(data)

    def _finish(self) -> None:
        text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
        if self._field and text:
            self._row.setdefault(self._field, text)
        self._field, self._buf = None, []


def _parse_date(text: str | None) -> datetime | None:
    if not text:
        return None
    for fmt in ("%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(text.strip(), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def parse_results(html: str) -> list[dict[str, str]]:
    p = _ResultsParser()
    p.feed(html)
    return p.rows


@register
class SuccessFactorsAdapter(Adapter):
    ats = "successfactors"

    def fetch(self, company: CompanyConfig) -> list[Job]:
        if not company.host:
            raise ValueError(f"{company.name}: successfactors adapter needs host")
        base = f"https://{company.host}"
        seen: dict[str, Job] = {}
        for term in SEARCH_TERMS:
            for page in range(MAX_PAGES):
                resp = request_with_retry(
                    self.client, "GET", f"{base}/search/",
                    params={"q": term, "startrow": page * PAGE_SIZE},
                    headers=_HEADERS,
                )
                resp.raise_for_status()
                rows = parse_results(resp.text)
                if not rows and not any(m in resp.text for m in _MARKERS):
                    raise ValueError(f"{company.name}: successfactors page has no results container")
                for r in rows:
                    m = _ID_RE.search(r["href"])
                    ext = m.group(1) if m else r["href"]
                    if ext in seen:
                        continue
                    href = r["href"]
                    seen[ext] = Job(
                        source=self.ats,
                        company=company.name,
                        external_id=ext,
                        title=r.get("title", ""),
                        location=r.get("location"),
                        url=href if href.startswith("http") else base + href,
                        posted_at=_parse_date(r.get("date")),
                    )
                total_m = _TOTAL_RE.search(resp.text)
                total = int(total_m.group(1).replace(",", "")) if total_m else None
                if not rows or len(rows) < PAGE_SIZE or (
                    total is not None and (page + 1) * PAGE_SIZE >= total
                ):
                    break
        return list(seen.values())
