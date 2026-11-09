"""Amazon adapter — amazon.jobs public search JSON.

Amazon runs its own posting system rather than a standard ATS. The public search
endpoint returns JSON, so we implement it directly. If Amazon changes the shape
and a request fails, we surface a clear error and let the scan skip cleanly
(the scan loop catches per-company errors and never aborts the whole run).
"""

from __future__ import annotations

from datetime import datetime

from ..models import CompanyConfig, Job
from .base import Adapter, html_to_text, register, request_with_retry

# Public search JSON used by the amazon.jobs site itself.
SEARCH_URL = "https://www.amazon.jobs/en/search.json"
JOB_BASE = "https://www.amazon.jobs"
PAGE_SIZE = 100
MAX_PAGES = 20

# Narrow server-side with internship keywords only (each a few hundred hits at
# most, well under the page cap) and union them. The title-prefilter still
# requires an internship + role term, so this is a superset of eligible roles —
# SWE and PM/TPM internships alike.
#
# Deliberately NO category filter (verified live 2026-10):
# - A plain ``category=<slug>`` param is silently ignored by search.json (same hit
#   count as no filter); only ``category[]=<slug>`` (repeatable) actually filters.
# - Amazon's categories are unreliable for interns: eligible postings are spread
#   over 7+ categories, e.g. "Software Development Engineer Intern - Summer 2027
#   (CAN)" and "2027 Program/Product Manager Intern" are filed under
#   "Administrative Support", and PM interns also appear under "Operations, IT, &
#   Support Engineering" and "Business Intelligence" — not just
#   project-program-product-management-{technical,non-tech}. Filtering by
#   category would silently drop them, and "intern"+"co-op" is only ~6 requests.
BASE_QUERIES = ("intern", "co-op")


def _parse_dt(value) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%B %d, %Y"):
        try:
            return datetime.strptime(str(value), fmt)
        except ValueError:
            continue
    return None


@register
class AmazonAdapter(Adapter):
    ats = "amazon"

    def fetch(self, company: CompanyConfig) -> list[Job]:
        seen_uids: set[str] = set()
        jobs: list[Job] = []
        for query in BASE_QUERIES:
            for job in self._search(company, query):
                if job.uid not in seen_uids:
                    seen_uids.add(job.uid)
                    jobs.append(job)
        return jobs

    def _search(self, company: CompanyConfig, base_query: str) -> list[Job]:
        params_base = {
            "result_limit": PAGE_SIZE,
            "sort": "recent",
            "base_query": base_query,
        }
        jobs: list[Job] = []
        offset = 0
        for _ in range(MAX_PAGES):
            params = {**params_base, "offset": offset}
            try:
                resp = request_with_retry(
                    self.client, "GET", SEARCH_URL, params=params
                )
                resp.raise_for_status()
                payload = resp.json()
            except Exception as exc:  # noqa: BLE001 - surface a clear, skippable error
                raise NotImplementedError(
                    "amazon.jobs search endpoint failed or changed shape; "
                    f"supply a careers URL or fix the adapter ({exc})"
                ) from exc

            hits = payload.get("jobs", [])
            if not hits:
                break
            for j in hits:
                path = j.get("job_path", "")
                url = (JOB_BASE + path) if path else JOB_BASE
                jobs.append(
                    Job(
                        source=self.ats,
                        company=company.name,
                        external_id=str(j.get("id_icims") or j.get("id") or path),
                        title=j.get("title", ""),
                        location=j.get("location") or j.get("normalized_location"),
                        department=j.get("business_category"),
                        url=url,
                        description=html_to_text(
                            j.get("description") or j.get("basic_qualifications")
                        ),
                        posted_at=_parse_dt(j.get("posted_date")),
                    )
                )

            total = payload.get("hits", 0)
            offset += PAGE_SIZE
            if offset >= total:
                break

        return jobs
