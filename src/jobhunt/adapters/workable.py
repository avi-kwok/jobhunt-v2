"""Workable adapter — public widget API, no auth, one call per board.

Config: ``slug`` = Workable account subdomain (apply.workable.com/<slug>).

``?details=true`` returns the whole (small) board with descriptions in a single
request; the widget has no search, so there is no pagination.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ..models import CompanyConfig, Job
from .base import Adapter, html_to_text, register, request_with_retry

BASE = "https://apply.workable.com/api/v1/widget/accounts/{slug}"


def _parse_date(value) -> datetime | None:
    if not value:
        return None
    try:
        d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


@register
class WorkableAdapter(Adapter):
    ats = "workable"

    def fetch(self, company: CompanyConfig) -> list[Job]:
        if not company.slug:
            raise ValueError(f"{company.name}: workable adapter needs a slug")
        resp = request_with_retry(
            self.client, "GET", BASE.format(slug=company.slug), params={"details": "true"}
        )
        resp.raise_for_status()
        seen: set[str] = set()
        jobs: list[Job] = []
        for j in resp.json().get("jobs") or []:
            sc = str(j.get("shortcode") or "")
            if not sc or sc in seen:
                continue
            seen.add(sc)
            parts = [j.get("city"), j.get("state"), j.get("country")]
            location = ", ".join(x for x in parts if x) or None
            if j.get("telecommuting"):
                location = f"{location} (Remote)" if location else "Remote"
            jobs.append(
                Job(
                    source=self.ats,
                    company=company.name,
                    external_id=sc,
                    title=j.get("title", ""),
                    location=location,
                    department=j.get("department") or None,
                    employment_type=j.get("employment_type"),
                    url=j.get("url") or j.get("shortlink") or f"https://apply.workable.com/j/{sc}",
                    description=html_to_text(j.get("description")),
                    posted_at=_parse_date(j.get("published_on") or j.get("created_at")),
                )
            )
        return jobs
