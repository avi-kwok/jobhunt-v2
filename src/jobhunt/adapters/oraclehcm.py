"""Oracle Recruiting Cloud (HCM Candidate Experience) adapter — public REST, no auth.

Config mapping (existing CompanyConfig fields only):
  host -> the fa host that serves the careers page, e.g. "jpmc.fa.oraclecloud.com"
          (also the scheduler's per-host politeness key)
  site -> the Candidate Experience siteNumber, e.g. "CX_1001" (Dell uses "careers")

The finder searches keywords server-side (relevancy-sorted; matches title AND
description, so it is fuzzy). We never page a whole board: for each term in
SEARCH_TERMS we page newest-relevance-first, stopping at MAX_PAGES or at the first
page with no internship-looking title, then union by requisition Id.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from ..models import CompanyConfig, Job
from .base import Adapter, html_to_text, register, request_with_retry

API = "https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
JOB_URL = "https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{id}"

SEARCH_TERMS = ("intern", "co-op", "student")
PAGE_LIMIT = 25
MAX_PAGES = 4  # per term (=100 results)

# A page with no title matching this means relevancy has run out of intern postings.
_INTERNISH_RE = re.compile(
    r"intern|co-?op|student|summer|trainee|placement|graduate|campus|apprentice"
    r"|early career|new grad|analyst program",
    re.IGNORECASE,
)


def _parse_date(value) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


@register
class OracleHcmAdapter(Adapter):
    ats = "oraclehcm"

    def fetch(self, company: CompanyConfig) -> list[Job]:
        if not company.host or not company.site:
            raise ValueError(f"{company.name}: oraclehcm adapter needs host and site")
        jobs: dict[str, Job] = {}
        for term in SEARCH_TERMS:
            for job in self._search(company, term):
                jobs.setdefault(job.external_id, job)
        return list(jobs.values())

    def _search(self, company: CompanyConfig, term: str) -> list[Job]:
        url = API.format(host=company.host)
        out: list[Job] = []
        for page in range(MAX_PAGES):
            finder = (
                f'findReqs;siteNumber={company.site},keyword="{term}",'
                f"limit={PAGE_LIMIT},offset={page * PAGE_LIMIT}"
            )
            resp = request_with_retry(
                self.client, "GET", url,
                params={"onlyData": "true", "expand": "requisitionList", "finder": finder},
            )
            resp.raise_for_status()
            items = resp.json().get("items") or []
            reqs = (items[0].get("requisitionList") or []) if items else []
            if not reqs:
                break
            out.extend(self._to_job(r, company) for r in reqs)
            if len(reqs) < PAGE_LIMIT or not any(
                _INTERNISH_RE.search(r.get("Title") or "") for r in reqs
            ):
                break
        return out

    @staticmethod
    def _to_job(r: dict, company: CompanyConfig) -> Job:
        rid = str(r.get("Id"))
        desc = " ".join(
            x for x in (r.get("ShortDescriptionStr"), r.get("ExternalResponsibilitiesStr")) if x
        )
        return Job(
            source="oraclehcm",
            company=company.name,
            external_id=rid,
            title=r.get("Title") or "",
            location=r.get("PrimaryLocation"),
            department=r.get("JobFamily"),
            employment_type=r.get("JobSchedule") or r.get("ContractType"),
            url=JOB_URL.format(host=company.host, site=company.site, id=rid),
            description=html_to_text(desc),
            posted_at=_parse_date(r.get("PostedDate")),
        )
