"""Recruitee adapter — public careers-site JSON, one call per board, no auth.

Config: ``slug`` = Recruitee subdomain (``<slug>.recruitee.com``; Huawei Canada is
``huaweicanada``). Set ``host`` = ``<slug>.recruitee.com`` so the scheduler's
politeness key matches the real hostname.

``GET /api/offers/`` returns every published offer with its description in one
response (no search, no pagination). A body without an ``offers`` list (redesign,
challenge page) raises instead of silently returning no jobs.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ..models import CompanyConfig, Job
from .base import Adapter, html_to_text, register, request_with_retry

BASE = "https://{slug}.recruitee.com/api/offers/"


def _parse_date(value) -> datetime | None:
    if not value:
        return None
    try:
        d = datetime.strptime(str(value).replace(" UTC", ""), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return d.replace(tzinfo=timezone.utc)


@register
class RecruiteeAdapter(Adapter):
    ats = "recruitee"

    def fetch(self, company: CompanyConfig) -> list[Job]:
        if not company.slug:
            raise ValueError(f"{company.name}: recruitee adapter needs a slug")
        resp = request_with_retry(self.client, "GET", BASE.format(slug=company.slug))
        resp.raise_for_status()
        try:
            offers = resp.json().get("offers")
        except (ValueError, AttributeError):
            offers = None
        if not isinstance(offers, list):
            raise ValueError(f"{company.name}: recruitee response has no 'offers' list")

        seen: set[str] = set()
        jobs: list[Job] = []
        for o in offers:
            oid = str(o.get("id") or o.get("guid") or "")
            if not oid or oid in seen or o.get("status", "published") != "published":
                continue
            seen.add(oid)
            location = o.get("location") or ", ".join(
                x for x in (o.get("city"), o.get("state_name"), o.get("country")) if x
            ) or None
            if o.get("remote"):
                location = f"{location} (Remote)" if location else "Remote"
            url = o.get("careers_url") or o.get("careers_apply_url") or ""
            if not url and o.get("slug"):
                url = f"https://{company.slug}.recruitee.com/o/{o['slug']}"
            jobs.append(
                Job(
                    source=self.ats,
                    company=company.name,
                    external_id=oid,
                    title=o.get("title", ""),
                    location=location,
                    department=o.get("department") or None,
                    employment_type=o.get("employment_type_code") or None,
                    url=url,
                    description=html_to_text(o.get("description")),
                    posted_at=_parse_date(o.get("published_at") or o.get("created_at")),
                )
            )
        return jobs
