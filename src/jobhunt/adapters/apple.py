"""Apple adapter — jobs.apple.com public search API (the one the careers site calls).

Flow: GET /api/v1/CSRFToken (returns the token in the ``x-apple-csrf-token``
header plus a session cookie, kept by the shared httpx client), then
POST /api/v1/search with that header. No config needed beyond ``ats: apple``.

The search is fuzzy over title+description (``intern`` alone matches ~2300 of
~6400 postings), but ``sort: relevance`` ranks title matches first. So we run a
few short searches (INTERN_QUERIES), page through each only while pages still
contain internship-like titles (hard cap MAX_PAGES), keep only those titles,
and union by positionId.
"""

from __future__ import annotations

import re
from datetime import datetime

from ..models import CompanyConfig, Job
from .base import Adapter, html_to_text, register, request_with_retry

BASE = "https://jobs.apple.com"
CSRF_URL = f"{BASE}/api/v1/CSRFToken"
SEARCH_URL = f"{BASE}/api/v1/search"
INTERN_QUERIES = ("internship", "intern", "co-op")
INTERN_TITLE_RE = re.compile(r"intern|co-?op|student|new grad", re.IGNORECASE)
PAGE_SIZE = 20  # fixed by the API
MAX_PAGES = 10  # per query


def _dt(value) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value)[:19])
    except ValueError:
        return None


@register
class AppleAdapter(Adapter):
    ats = "apple"

    def fetch(self, company: CompanyConfig) -> list[Job]:
        resp = request_with_retry(self.client, "GET", CSRF_URL)
        resp.raise_for_status()
        token = resp.headers.get("x-apple-csrf-token")
        if not token:
            raise RuntimeError(f"{company.name}: apple CSRF token missing")
        headers = {"x-apple-csrf-token": token, "Accept": "application/json"}

        jobs: dict[str, Job] = {}
        for query in INTERN_QUERIES:
            for page in range(1, MAX_PAGES + 1):
                body = {
                    "query": query, "filters": {}, "page": page,
                    "locale": "en-us", "sort": "relevance",
                    "format": {"longDate": "MMMM D, YYYY", "mediumDate": "MMM D, YYYY"},
                }
                r = request_with_retry(self.client, "POST", SEARCH_URL, json=body, headers=headers)
                r.raise_for_status()
                results = (r.json().get("res") or {}).get("searchResults") or []
                hits = [p for p in results if INTERN_TITLE_RE.search(p.get("postingTitle") or "")]
                for p in hits:
                    pid = str(p.get("positionId") or p.get("id"))
                    if pid not in jobs:
                        jobs[pid] = self._to_job(p, pid, company)
                # relevance puts title matches first: a page with none ends the query
                if not hits or len(results) < PAGE_SIZE:
                    break
        return list(jobs.values())

    @staticmethod
    def _to_job(p: dict, pid: str, company: CompanyConfig) -> Job:
        locs = p.get("locations") or []
        location = "; ".join(dict.fromkeys(
            ", ".join(x for x in (l.get("city"), l.get("stateProvince"), l.get("countryName")) if x)
            for l in locs
        )) or None
        slug = p.get("transformedPostingTitle") or ""
        return Job(
            source="apple",
            company=company.name,
            external_id=pid,
            title=p.get("postingTitle", ""),
            location=location,
            department=(p.get("team") or {}).get("teamName"),
            url=f"{BASE}/en-us/details/{pid}/{slug}".rstrip("/"),
            description=html_to_text(p.get("jobSummary")),
            posted_at=_dt(p.get("postDateInGMT")),
        )
