"""Pinpoint adapter — public <slug>.pinpointhq.com/postings.json, no auth.

Config mapping: `slug` = the Pinpoint subdomain (<slug>.pinpointhq.com). The company's own
careers domain may differ; Job.url uses the `url` field the API returns.

One call returns every posting with full description HTML (no pagination, no search, no
dates). Description = description + key_responsibilities + skills_knowledge_expertise.
"""

from __future__ import annotations

from ..models import CompanyConfig, Job
from .base import Adapter, html_to_text, register, request_with_retry


@register
class PinpointAdapter(Adapter):
    ats = "pinpoint"

    def fetch(self, company: CompanyConfig) -> list[Job]:
        if not company.slug:
            raise ValueError(f"{company.name}: pinpoint adapter needs a slug")
        resp = request_with_retry(
            self.client, "GET", f"https://{company.slug}.pinpointhq.com/postings.json"
        )
        resp.raise_for_status()

        jobs: list[Job] = []
        seen: set[str] = set()
        for p in resp.json().get("data") or []:
            jid = str(p.get("id"))
            if jid in seen:
                continue
            seen.add(jid)
            job = p.get("job") or {}
            loc = p.get("location") or {}
            html = "\n".join(
                p.get(k) or "" for k in ("description", "key_responsibilities", "skills_knowledge_expertise")
            )
            jobs.append(
                Job(
                    source=self.ats,
                    company=company.name,
                    external_id=jid,
                    title=(p.get("title") or "").strip(),
                    location=loc.get("name") or loc.get("city"),
                    department=(job.get("department") or {}).get("name"),
                    employment_type=p.get("employment_type_text"),
                    url=p.get("url") or f"https://{company.slug}.pinpointhq.com{p.get('path', '')}",
                    description=html_to_text(html),
                )
            )
        return jobs
