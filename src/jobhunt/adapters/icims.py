"""iCIMS adapter — three public, unauthenticated shapes behind one ``ats: icims`` key.

The shape is picked from ``company.host`` (also the scheduler's politeness key):

1. **Jibe** (default; any host not matched below) — iCIMS Jibe career sites
   (careers.amd.com, careers.docusign.com, careers.sig.com, www.github.careers ...)
   expose ``GET https://{host}/api/jobs?keywords=..&limit=100&page=N``. Keyword search is
   whole-word (``intern`` does not match ``internship``), so we union several terms and
   dedupe by ``slug``. Job URL: ``https://{host}/jobs/{slug}`` (the site redirects to its
   own ``/careers-home/jobs/..`` form when needed).
2. **Classic iCIMS portal** (host ends ``.icims.com``) — server-rendered
   ``https://{host}/jobs/search?ss=1&in_iframe=1&searchKeyword=..&pr=N`` (50 per page,
   ``pr`` is the 0-based page). Parsed with regexes; no per-job detail requests, so no
   description. ``slug`` may hold extra comma-separated portal hosts of the same company
   (MSCI splits its board per region); all are searched and deduped by job id.
3. **Atlassian listings** (host ``www.atlassian.com``) — one call to
   ``/endpoint/careers/listings`` (whole board, ~2 MB), filtered here to intern-like titles.

Politeness: ~1 request/second per host (``DELAYS`` overrides, e.g. AMD's robots.txt
crawl-delay of 5 s). Every fetch goes through ``request_with_retry``.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from html import unescape

from ..models import CompanyConfig, Job
from .base import Adapter, html_to_text, register, request_with_retry

JIBE_TERMS = ("intern", "internship", "co-op", "student", "new grad")
PORTAL_TERMS = ("intern", "co-op", "student", "new grad")  # portal search is substring-ish
INTERN_RE = re.compile(r"intern|co-?op|student|new[ -]?grad", re.I)

JIBE_LIMIT = 100
JIBE_MAX_PAGES = 5  # per term (=500 postings)
PORTAL_PAGE = 50
PORTAL_MAX_PAGES = 5

DEFAULT_DELAY = 1.0
DELAYS = {"careers.amd.com": 5.0}  # robots.txt crawl-delay

ATLASSIAN_HOST = "www.atlassian.com"
ATLASSIAN_URL = f"https://{ATLASSIAN_HOST}/endpoint/careers/listings"

_CARD_RE = re.compile(r'<li class="iCIMS_JobCardItem">(.*?)</li>', re.S)
_HREF_RE = re.compile(r'href="(https?://[^"]*?/jobs/(\d+)/[^"?]*)[^"]*"')
_TITLE_RE = re.compile(r"<h3[^>]*>(.*?)</h3>", re.S)
_POSTED_RE = re.compile(r'<span title="(\d+/\d+/\d{4} \d+:\d+ [AP]M)"')
_FIELD_RE = re.compile(
    r'<dt class="iCIMS_JobHeaderField">(.*?)</dt>\s*<dd class="iCIMS_JobHeaderData">(.*?)</dd>', re.S
)
_PAGES_RE = re.compile(r"Page \d+ of (\d+)")
_PORTAL_RE = re.compile(r"iCIMS|no results", re.I)  # portal chrome / explicit empty notice


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def _iso(value) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@register
class IcimsAdapter(Adapter):
    ats = "icims"

    def __init__(self, client=None):
        super().__init__(client)
        self._last = 0.0

    def _get(self, url: str, host: str, **kwargs):
        wait = DELAYS.get(host, DEFAULT_DELAY) - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        try:
            resp = request_with_retry(self.client, "GET", url, **kwargs)
        finally:
            self._last = time.monotonic()
        resp.raise_for_status()
        return resp

    def fetch(self, company: CompanyConfig) -> list[Job]:
        host = company.host
        if not host:
            raise ValueError(f"{company.name}: icims adapter needs a host")
        self._last = 0.0
        if host == ATLASSIAN_HOST:
            return self._atlassian(company)
        if host.endswith(".icims.com"):
            return self._portals(company, host)
        return self._jibe(company, host)

    # ---- Jibe -------------------------------------------------------------
    def _jibe(self, company: CompanyConfig, host: str) -> list[Job]:
        seen: dict[str, Job] = {}
        for term in JIBE_TERMS:
            for page in range(1, JIBE_MAX_PAGES + 1):
                resp = self._get(
                    f"https://{host}/api/jobs",
                    host,
                    params={"keywords": term, "limit": JIBE_LIMIT, "page": page},
                )
                payload = resp.json()
                if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
                    raise ValueError(f"{host}: unexpected Jibe payload (no 'jobs' list)")
                rows = payload["jobs"]
                for row in rows:
                    job = self._jibe_job(row.get("data") or {}, company, host)
                    if job and job.external_id not in seen:
                        seen[job.external_id] = job
                if len(rows) < JIBE_LIMIT or page * JIBE_LIMIT >= int(payload.get("totalCount") or 0):
                    break
        return list(seen.values())

    def _jibe_job(self, d: dict, company: CompanyConfig, host: str) -> Job | None:
        ext = d.get("slug") or d.get("req_id")
        if not ext:
            return None
        cats = d.get("categories") or []
        return Job(
            source=self.ats,
            company=company.name,
            external_id=str(ext),
            title=(d.get("title") or "").strip(),
            location=d.get("full_location") or d.get("location_name") or None,
            department=(cats[0].get("name") if cats and isinstance(cats[0], dict) else None),
            employment_type=d.get("employment_type"),
            url=f"https://{host}/jobs/{ext}",
            description=html_to_text(d.get("description")),
            posted_at=_iso(d.get("posted_date") or d.get("create_date")),
        )

    # ---- classic iCIMS portals ---------------------------------------------
    def _portals(self, company: CompanyConfig, host: str) -> list[Job]:
        hosts = [host] + [h.strip() for h in (company.slug or "").split(",") if h.strip()]
        seen: dict[str, Job] = {}
        for h in hosts:
            for term in PORTAL_TERMS:
                for pr in range(PORTAL_MAX_PAGES):
                    html = self._get(
                        f"https://{h}/jobs/search",
                        h,
                        params={"ss": 1, "in_iframe": 1, "searchKeyword": term, "pr": pr},
                    ).text
                    cards = _CARD_RE.findall(html)
                    if not cards and not _PORTAL_RE.search(html):
                        raise ValueError(f"{h}: unexpected iCIMS portal page (no markers)")
                    for card in cards:
                        job = self._portal_job(card, company)
                        if job and job.external_id not in seen:
                            seen[job.external_id] = job
                    m = _PAGES_RE.search(html)
                    if len(cards) < PORTAL_PAGE or not m or pr + 1 >= int(m.group(1)):
                        break
        return list(seen.values())

    def _portal_job(self, card: str, company: CompanyConfig) -> Job | None:
        href = _HREF_RE.search(card)
        if not href:
            return None
        t = _TITLE_RE.search(card)
        fields = {_text(k).lower(): _text(v) for k, v in _FIELD_RE.findall(card)}
        # The location <dt> carries only a screen-reader label, so its key is "job locations".
        location = next((v for k, v in fields.items() if "location" in k), None)
        posted = None
        pm = _POSTED_RE.search(card)
        if pm:
            try:  # portal-local time, tz not exposed; treated as UTC
                posted = datetime.strptime(pm.group(1), "%m/%d/%Y %I:%M %p").replace(tzinfo=timezone.utc)
            except ValueError:
                pass
        return Job(
            source=self.ats,
            company=company.name,
            external_id=href.group(2),
            title=_text(t.group(1)) if t else "",
            location=location,
            department=fields.get("category"),
            employment_type=fields.get("position type"),
            url=href.group(1),
            posted_at=posted,
        )

    # ---- Atlassian ----------------------------------------------------------
    def _atlassian(self, company: CompanyConfig) -> list[Job]:
        rows = self._get(ATLASSIAN_URL, ATLASSIAN_HOST).json()
        jobs: dict[str, Job] = {}
        for r in rows:
            title = (r.get("title") or "").strip()
            ext = r.get("id")
            if not ext or not INTERN_RE.search(title) or str(ext) in jobs:
                continue
            locs = [re.sub(r"\s+", " ", l).strip() for l in (r.get("locations") or [])]
            jobs[str(ext)] = Job(
                source=self.ats,
                company=company.name,
                external_id=str(ext),
                title=title,
                location="; ".join(locs) or None,
                department=r.get("category"),
                url=f"https://www.atlassian.com/company/careers/details/{ext}",
                description=html_to_text(r.get("responsibilities") or r.get("overview")),
            )
        return list(jobs.values())
