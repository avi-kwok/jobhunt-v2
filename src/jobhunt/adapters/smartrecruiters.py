"""SmartRecruiters adapter — public Posting API, no auth.

Config: ``slug`` = SmartRecruiters company identifier (case-sensitive, e.g. "Freshworks").

The API supports full-text search (``q``), so we only fetch internship-relevant
postings: the union of ``SEARCH_TERMS`` searches, each paginated (limit 100) up to
``MAX_PAGES``, deduped by posting id. The list payload carries no description and
we avoid per-job detail calls, so ``description`` is None.
"""

from __future__ import annotations

from datetime import datetime

from ..models import CompanyConfig, Job
from .base import Adapter, register, request_with_retry

BASE = "https://api.smartrecruiters.com/v1/companies/{slug}/postings"
JOB_URL = "https://jobs.smartrecruiters.com/{slug}/{id}"
SEARCH_TERMS = ("intern", "co-op", "student")
PAGE_LIMIT = 100
MAX_PAGES = 5  # per search (=500 postings)


def _parse_dt(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


@register
class SmartRecruitersAdapter(Adapter):
    ats = "smartrecruiters"

    def fetch(self, company: CompanyConfig) -> list[Job]:
        if not company.slug:
            raise ValueError(f"{company.name}: smartrecruiters adapter needs a slug")
        url = BASE.format(slug=company.slug)
        seen: set[str] = set()
        jobs: list[Job] = []
        for term in SEARCH_TERMS:
            offset = 0
            for _ in range(MAX_PAGES):
                resp = request_with_retry(
                    self.client, "GET", url,
                    params={"q": term, "limit": PAGE_LIMIT, "offset": offset},
                )
                resp.raise_for_status()
                content = resp.json().get("content") or []
                for p in content:
                    pid = str(p.get("id"))
                    if pid in seen:
                        continue
                    seen.add(pid)
                    jobs.append(self._to_job(p, company))
                offset += PAGE_LIMIT
                if len(content) < PAGE_LIMIT:
                    break
        return jobs

    def _to_job(self, p: dict, company: CompanyConfig) -> Job:
        loc = p.get("location") or {}
        location = loc.get("fullLocation") or ", ".join(
            x for x in (loc.get("city"), loc.get("country")) if x
        ) or None
        if loc.get("remote") and location:
            location += " (Remote)"
        pid = str(p.get("id"))
        return Job(
            source=self.ats,
            company=company.name,
            external_id=pid,
            title=p.get("name", ""),
            location=location,
            department=(p.get("department") or {}).get("label"),
            employment_type=(p.get("typeOfEmployment") or {}).get("label"),
            url=JOB_URL.format(slug=company.slug, id=pid),
            posted_at=_parse_dt(p.get("releasedDate")),
        )
