"""Gem adapter — public job-board API (api.gem.com/job_board/v0/<slug>/job_posts/), no auth.

Config mapping: `slug` = board slug in jobs.gem.com/<slug>. Host is fixed: api.gem.com.

One call returns every posting (a JSON array) with descriptions; no pagination or search.
"""

from __future__ import annotations

from datetime import datetime

from ..models import CompanyConfig, Job
from .base import Adapter, html_to_text, register, request_with_retry


def _dt(value) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None
    except (AttributeError, ValueError):
        return None


@register
class GemAdapter(Adapter):
    ats = "gem"

    def fetch(self, company: CompanyConfig) -> list[Job]:
        if not company.slug:
            raise ValueError(f"{company.name}: gem adapter needs a slug")
        resp = request_with_retry(
            self.client, "GET", f"https://api.gem.com/job_board/v0/{company.slug}/job_posts/"
        )
        resp.raise_for_status()

        jobs: list[Job] = []
        seen: set[str] = set()
        for p in resp.json():
            jid = str(p.get("id"))
            if jid in seen:
                continue
            seen.add(jid)
            depts = p.get("departments") or []
            jobs.append(
                Job(
                    source=self.ats,
                    company=company.name,
                    external_id=jid,
                    title=p.get("title", ""),
                    location=(p.get("location") or {}).get("name"),
                    department=depts[0].get("name") if depts else None,
                    employment_type=p.get("employment_type"),
                    url=p.get("absolute_url") or f"https://jobs.gem.com/{company.slug}/{jid}",
                    description=html_to_text(p.get("content_plain") or p.get("content")),
                    posted_at=_dt(p.get("first_published_at") or p.get("created_at")),
                )
            )
        return jobs
