"""JazzHR adapter — public applytojob.com board page (HTML), no auth.

Config mapping: `slug` = the subdomain in <slug>.applytojob.com (host is derived from it).

One page lists every opening (no search/pagination). Parsed with the stdlib HTMLParser:
each `li.list-group-item` holds an `h3 > a` (title + /apply/<id>/<slug> url) and a
`ul.list-inline` of `li` items (location first, then department). No dates or descriptions
on the list, and we skip per-job detail fetches. An empty board (list container or the "no open positions" notice present, zero rows) is a
valid result; a page with neither marker raises, so a redesign / challenge page is not "no jobs".
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

from ..models import CompanyConfig, Job
from .base import Adapter, register, request_with_retry

ID_RE = re.compile(r"/apply/([^/?#]+)")
# Structural markers of a real JazzHR board page (rows, or the empty-board container/notice).
_BOARD_RE = re.compile(r"list-group|job-board-list|jobs-list|no open positions", re.I)


class _ListParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.items: list[dict] = []
        self._cur: dict | None = None
        self._in_a = False
        self._in_meta = False
        self._in_meta_li = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class") or ""
        if tag == "li" and "list-group-item" in cls.split() and "list-group-item-text" not in cls:
            self._cur = {"href": None, "title": "", "meta": []}
            self.items.append(self._cur)
        elif self._cur is None:
            return
        elif tag == "a" and a.get("href") and self._cur["href"] is None:
            self._cur["href"] = a["href"]
            self._in_a = True
        elif tag == "ul" and "list-inline" in cls:
            self._in_meta = True
        elif tag == "li" and self._in_meta:
            self._in_meta_li = True
            self._cur["meta"].append("")

    def handle_endtag(self, tag):
        if tag == "a":
            self._in_a = False
        elif tag == "ul":
            self._in_meta = False
        elif tag == "li":
            self._in_meta_li = False

    def handle_data(self, data):
        if self._cur is None:
            return
        if self._in_a:
            self._cur["title"] += data
        elif self._in_meta_li:
            self._cur["meta"][-1] += data


@register
class JazzHRAdapter(Adapter):
    ats = "jazzhr"

    def fetch(self, company: CompanyConfig) -> list[Job]:
        if not company.slug:
            raise ValueError(f"{company.name}: jazzhr adapter needs a slug")
        resp = request_with_retry(
            self.client, "GET", f"https://{company.slug}.applytojob.com/apply",
            headers={"Accept": "text/html"},
        )
        resp.raise_for_status()
        if not _BOARD_RE.search(resp.text):
            raise ValueError(f"{company.name}: jazzhr page has no job list container")
        p = _ListParser()
        p.feed(resp.text)

        jobs: list[Job] = []
        seen: set[str] = set()
        for it in p.items:
            m = ID_RE.search(it["href"] or "")
            title = " ".join(it["title"].split())
            if not m or not title or m.group(1) in seen:
                continue
            seen.add(m.group(1))
            meta = [" ".join(x.split()) for x in it["meta"] if x.strip()]
            jobs.append(
                Job(
                    source=self.ats,
                    company=company.name,
                    external_id=m.group(1),
                    title=title,
                    location=meta[0] if meta else None,
                    department=meta[1] if len(meta) > 1 else None,
                    url=it["href"],
                )
            )
        return jobs
