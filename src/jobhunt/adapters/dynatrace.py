"""Dynatrace careers adapter — first-party Coveo search proxy, no auth.

The careers page fills its job list from POST https://www.dynatrace.com/api/coveo/search/.
That endpoint is the site's own proxy (the Coveo token is supplied server side, so the client
sends none). robots.txt has no Disallow for this path or for /careers/jobs/.

Each result carries title, uri, epoch-ms date, department, office_locations and country in
`raw`, so the list alone is enough: no per-job detail requests. We page 100 results at a time
and stop at totalCount or an empty page (hard page cap).

Config mapping: `host` = www.dynatrace.com (politeness key). No slug is needed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from itertools import zip_longest

from ..models import CompanyConfig, Job
from .base import Adapter, html_to_text, register, request_with_retry

URL = "https://www.dynatrace.com/api/coveo/search/"
PAGE_SIZE = 100
MAX_PAGES = 5
FIELDS = [
    "title", "uri", "date", "department", "office_locations", "country", "description",
]


def _dt(ms) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc) if ms else None
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _location(raw: dict) -> str | None:
    parts = (
        ", ".join(p for p in pair if p)
        for pair in zip_longest(raw.get("office_locations") or [], raw.get("country") or [])
    )
    return " | ".join(p for p in parts if p) or None


@register
class DynatraceAdapter(Adapter):
    ats = "dynatrace"

    def fetch(self, company: CompanyConfig) -> list[Job]:
        jobs: dict[str, Job] = {}
        first = 0
        for _ in range(MAX_PAGES):
            resp = request_with_retry(
                self.client,
                "POST",
                URL,
                json={
                    "q": "",
                    "numberOfResults": PAGE_SIZE,
                    "firstResult": first,
                    "wildcards": True,
                    "fieldsToInclude": FIELDS,
                    "facets": [],
                },
            )
            resp.raise_for_status()
            data = resp.json()
            results = data.get("results") or []
            for r in results:
                uri = r.get("uri") or ""
                jid = uri.rstrip("/").rsplit("/", 1)[-1]
                if not jid or jid in jobs:
                    continue
                raw = r.get("raw") or {}
                jobs[jid] = Job(
                    source=self.ats,
                    company=company.name,
                    external_id=jid,
                    title=r.get("title") or "",
                    url=uri,
                    location=_location(raw),
                    department=" | ".join(raw.get("department") or []) or None,
                    description=html_to_text(raw.get("description")),
                    posted_at=_dt(raw.get("date")),
                )
            first += PAGE_SIZE
            if not results or first >= (data.get("totalCount") or 0):
                break
        return list(jobs.values())
