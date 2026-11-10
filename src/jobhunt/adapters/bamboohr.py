"""BambooHR adapter — public careers list JSON, no auth.

Config mapping: `slug` = the company subdomain in <slug>.bamboohr.com (host is derived from it).

One list call returns every opening (no search, no pagination). Descriptions and posted dates
exist only on /careers/<id>/detail, so we fetch it just for intern-like titles, capped.
"""

from __future__ import annotations

import re
from datetime import datetime

import httpx

from ..models import CompanyConfig, Job
from .base import Adapter, html_to_text, register, request_with_retry

MAX_DETAILS = 15
INTERN_RE = re.compile(r"\bintern(ship)?s?\b|co-?op|student|new[ -]grad", re.I)


def _location(loc: dict | None, remote) -> str | None:
    loc = loc or {}
    parts = [p for p in (loc.get("city"), loc.get("state")) if p]
    text = ", ".join(parts)
    if remote and str(remote) not in ("0", "False", "false"):
        text = f"Remote, {text}" if text else "Remote"
    return text or None


@register
class BambooHRAdapter(Adapter):
    ats = "bamboohr"

    def fetch(self, company: CompanyConfig) -> list[Job]:
        if not company.slug:
            raise ValueError(f"{company.name}: bamboohr adapter needs a slug")
        base = f"https://{company.slug}.bamboohr.com/careers"
        resp = request_with_retry(self.client, "GET", f"{base}/list")
        resp.raise_for_status()
        rows = resp.json().get("result") or []

        jobs: list[Job] = []
        seen: set[str] = set()
        details = 0
        for it in rows:
            jid = str(it.get("id"))
            if jid in seen:
                continue
            seen.add(jid)
            title = it.get("jobOpeningName", "")
            description = None
            posted = None
            if INTERN_RE.search(title) and details < MAX_DETAILS:
                details += 1
                try:
                    r = request_with_retry(self.client, "GET", f"{base}/{jid}/detail")
                    r.raise_for_status()
                    op = (r.json().get("result") or {}).get("jobOpening") or {}
                    description = html_to_text(op.get("description"))
                    if op.get("datePosted"):
                        posted = datetime.fromisoformat(op["datePosted"])
                except (httpx.HTTPError, ValueError):
                    pass
            jobs.append(
                Job(
                    source=self.ats,
                    company=company.name,
                    external_id=jid,
                    title=title,
                    location=_location(it.get("location"), it.get("isRemote")),
                    department=it.get("departmentLabel"),
                    employment_type=it.get("employmentStatusLabel"),
                    url=f"{base}/{jid}",
                    description=description,
                    posted_at=posted,
                )
            )
        return jobs
