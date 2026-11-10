"""Rippling ATS adapter — public board API, no auth.

Config mapping: `slug` = board slug in ats.rippling.com/<slug>/jobs (e.g. "freshbooks",
"d-wave-quantum", "dialogue-en"). Board host is always ats.rippling.com.

The list endpoint has no search, so we page the whole board (pageSize=100, hard page cap).
It returns one row per (job, location); rows are merged by job id. Descriptions/dates only
exist on the per-job detail call, so we fetch it just for intern-like titles, capped.
"""

from __future__ import annotations

import re
from datetime import datetime

import httpx

from ..models import CompanyConfig, Job
from .base import Adapter, html_to_text, register, request_with_retry

BASE = "https://ats.rippling.com/api/v2/board/{slug}/jobs"
PAGE_SIZE = 100
MAX_PAGES = 10
MAX_DETAILS = 15
INTERN_RE = re.compile(r"\bintern(ship)?s?\b|co-?op|student|new[ -]grad", re.I)


def _dt(value) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


@register
class RipplingAdapter(Adapter):
    ats = "rippling"

    def fetch(self, company: CompanyConfig) -> list[Job]:
        if not company.slug:
            raise ValueError(f"{company.name}: rippling adapter needs a slug")
        url = BASE.format(slug=company.slug)
        rows: dict[str, dict] = {}
        locs: dict[str, list[str]] = {}
        for page in range(MAX_PAGES):
            resp = request_with_retry(
                self.client, "GET", url, params={"page": page, "pageSize": PAGE_SIZE}
            )
            resp.raise_for_status()
            data = resp.json()
            for it in data.get("items") or []:
                jid = str(it.get("id"))
                rows.setdefault(jid, it)
                for loc in it.get("locations") or []:
                    name = loc.get("name")
                    if name and name not in locs.setdefault(jid, []):
                        locs[jid].append(name)
            if page + 1 >= (data.get("totalPages") or 0):
                break

        jobs: list[Job] = []
        details = 0
        for jid, it in rows.items():
            title = it.get("name", "")
            description = posted = etype = None
            if INTERN_RE.search(title) and details < MAX_DETAILS:
                details += 1
                try:
                    r = request_with_retry(self.client, "GET", f"{url}/{jid}")
                    r.raise_for_status()
                    d = r.json()
                    desc = d.get("description") or {}
                    description = html_to_text(
                        "\n".join(str(desc.get(k) or "") for k in ("role", "company"))
                    )
                    posted = _dt(d.get("createdOn"))
                    etype = (d.get("employmentType") or {}).get("id")
                except (httpx.HTTPError, ValueError):
                    pass
            jobs.append(
                Job(
                    source=self.ats,
                    company=company.name,
                    external_id=jid,
                    title=title,
                    location=" | ".join(locs.get(jid, [])) or None,
                    department=(it.get("department") or {}).get("name"),
                    employment_type=etype,
                    url=it.get("url") or f"https://ats.rippling.com/{company.slug}/jobs/{jid}",
                    description=description,
                    posted_at=posted,
                )
            )
        return jobs
