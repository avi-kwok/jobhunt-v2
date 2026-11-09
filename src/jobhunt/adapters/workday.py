"""Workday adapter — public CXS search API, POST + offset pagination.

Large Workday boards (banks, big tech) carry *thousands* of postings. A blind
``searchText=""`` fetch is capped by ``MAX_PAGES`` and, on boards past that cap,
silently drops everything below the cap — including internships that happen to
sort low in Workday's default order. That is exactly how a live TD "Software
Engineer Intern/Co-op" posting was once never seen. So we never page a whole
board; we narrow server-side first, two ways:

1. **Internship facet (preferred).** The first request (empty facets) also
   returns the board's ``facets`` list. We scan its values — recursing into
   nested facet groups, skipping location-type facets ("Main Campus",
   "University Ave") — for intern-like descriptors (``INTERN_FACET_RE``: intern,
   internship, co-op, student, campus, university; deliberately *not* new-grad /
   graduate / apprentice). For each facet parameter with a hit we run ONE search
   with ``appliedFacets={param: [ids]}`` and empty ``searchText``, then union.
   Values within a facet OR together; separate facet params would AND, which is
   why each param gets its own search. This returns every intern posting on the
   board — SWE and PM roles alike — in one or two short queries.

2. **Phrase searches (fallback).** If no intern facet exists, or the company sets
   ``workday_facets: false`` in companies.resolved.yaml (escape hatch for boards
   whose facet is incomplete), we run the ``SEARCH_TERMS`` role × internship
   phrase searches and union them. ``searchText`` uses AND semantics over
   title+description, so ``"software intern"`` returns only postings mentioning
   both — a few hundred at most, under the page cap. Because the title-prefilter
   downstream requires a role term AND an internship term, these queries form a
   superset of anything that could pass the filter.

Either way results are deduped by uid, a persistent 429 triggers a cooldown and
a sequential retry, and ``MAX_PAGES`` caps every individual search.
"""

from __future__ import annotations

import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

from ..models import CompanyConfig, Job
from .base import Adapter, register, request_with_retry

JOBS_URL = "https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
JOB_BASE = "https://{tenant}.{wd}.myworkdayjobs.com/{site}"

PAGE_LIMIT = 20
MAX_PAGES = 50  # per-search safety valve (=1000 postings) so a search can't loop forever

# Fallback role × internship phrase searches (used when a board has no intern
# facet, or `workday_facets: false`). AND semantics keep each result set small and
# under the page cap while together covering the role terms real internship titles
# use. "data" was dropped: its role-word isn't a strong role_term for the
# downstream title-prefilter, so it mostly pulled non-eligible postings.
# "technology" catches bank "Technology Analyst" co-op/intern roles without
# pulling whole boards the way a bare "intern" would. The product/program terms
# cover PM-adjacent internships (product manager/owner/marketing, TPM).
SEARCH_TERMS = (
    "software intern",
    "software co-op",
    "engineer intern",
    "engineer co-op",
    "developer intern",
    "developer co-op",
    "technology intern",
    "technology co-op",
    "product intern",
    "product co-op",
    "program intern",
)

# Facet *values* whose descriptor marks an internship/co-op/student posting, e.g.
# "Intern (Fixed Term)", "Intern/Co-op", "Student/Coop (Fixed Term)", "Campus",
# "University", "Students and Rotational Programs". Word boundaries keep
# "Internal Audit" out. New-grad/graduate/apprentice are intentionally excluded.
INTERN_FACET_RE = re.compile(
    r"\b(?:interns?|internships?|co-?ops?|students?|campus|universit(?:y|ies))\b",
    re.IGNORECASE,
)
# Facets (by parameter or descriptor) that describe *where* a job is — their
# values are addresses/cities that can contain "Campus"/"University", so skip them.
_LOCATION_FACET_RE = re.compile(
    r"locat|countr|city|cities|state|region|province|distance|remote|site|address",
    re.IGNORECASE,
)

SEARCH_CONCURRENCY = 2  # run this many per-board searches at once; low by design so we stay a polite, low-burst client and never trip a host's rate limit

_RATE_LIMIT_COOLDOWN = 5.0
_HEADERS = {"Content-Type": "application/json", "Accept": "application/json"}

# One search = (searchText, appliedFacets).
Query = tuple[str, dict[str, list[str]]]


def find_intern_facets(facets: list[dict] | None) -> dict[str, list[str]]:
    """Map facetParameter -> [value ids] for every intern-like facet value.

    Walks nested facet groups (a value that itself carries ``facetParameter`` and
    ``values``), skipping location-type facets. Returns {} if nothing matches.
    """
    found: dict[str, list[str]] = {}

    def walk(group: list) -> None:
        for facet in group or []:
            if not isinstance(facet, dict):
                continue
            param = facet.get("facetParameter") or ""
            label = f"{param} {facet.get('descriptor') or ''}"
            if _LOCATION_FACET_RE.search(label):
                continue
            for value in facet.get("values") or []:
                if not isinstance(value, dict):
                    continue
                if "facetParameter" in value:  # nested facet group
                    walk([value])
                    continue
                vid = value.get("id")
                if param and vid and INTERN_FACET_RE.search(value.get("descriptor") or ""):
                    ids = found.setdefault(param, [])
                    if vid not in ids:
                        ids.append(vid)

    walk(facets or [])
    return found


@register
class WorkdayAdapter(Adapter):
    ats = "workday"

    def fetch(self, company: CompanyConfig) -> list[Job]:
        missing = [f for f in ("tenant", "wd", "site") if not getattr(company, f)]
        if missing:
            raise ValueError(
                f"{company.name}: workday adapter needs {', '.join(missing)}"
            )

        url = JOBS_URL.format(tenant=company.tenant, wd=company.wd, site=company.site)
        job_base = JOB_BASE.format(
            tenant=company.tenant, wd=company.wd, site=company.site
        )

        try:
            return self._fetch_board(url, job_base, company, workers=SEARCH_CONCURRENCY)
        except httpx.HTTPStatusError as exc:
            if exc.response is None or exc.response.status_code != 429:
                raise
            # request_with_retry exhausted its 429 retries: the board is rate-limiting.
            # Cool off, then retry the whole board sequentially (gentler burst).
            time.sleep(_RATE_LIMIT_COOLDOWN)
            print(
                f"[workday] {company.name}: rate-limited, retrying sequentially",
                file=sys.stderr,
            )
            return self._fetch_board(url, job_base, company, workers=1)

    def plan_queries(self, url: str, company: CompanyConfig) -> list[Query]:
        """Decide which searches to run for this board.

        Facet path: one discovery request (empty facets) to read the facet list,
        then one empty-searchText search per intern-like facet parameter. Falls
        back to the SEARCH_TERMS phrase searches when the board has no intern
        facet or the company opts out with ``workday_facets: false``.
        """
        if company.workday_facets is not False:
            facets = self._discover_facets(url)
            applied = find_intern_facets(facets)
            if applied:
                return [("", {param: ids}) for param, ids in applied.items()]
        return [(term, {}) for term in SEARCH_TERMS]

    def _discover_facets(self, url: str) -> list[dict]:
        body = {"appliedFacets": {}, "limit": PAGE_LIMIT, "offset": 0, "searchText": ""}
        resp = request_with_retry(self.client, "POST", url, json=body, headers=_HEADERS)
        resp.raise_for_status()
        return resp.json().get("facets") or []

    def _fetch_board(
        self, url: str, job_base: str, company: CompanyConfig, workers: int
    ) -> list[Job]:
        queries = self.plan_queries(url, company)
        return self._run_queries(url, job_base, company, queries, workers=workers)

    def _run_queries(
        self,
        url: str,
        job_base: str,
        company: CompanyConfig,
        queries: list[Query],
        workers: int,
    ) -> list[Job]:
        """Run each search and union results, deduped by uid.

        Results are unioned in the original ``queries`` order regardless of thread
        finish order, so output ordering is deterministic. ``workers=1`` runs the
        searches sequentially; higher values run up to ``workers`` at once.
        """
        if workers <= 1 or len(queries) <= 1:
            per_query = [
                self._search(url, job_base, company, text, facets)
                for text, facets in queries
            ]
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [
                    pool.submit(self._search, url, job_base, company, text, facets)
                    for text, facets in queries
                ]
                # .result() re-raises worker exceptions (e.g. the 429) here.
                per_query = [f.result() for f in futures]

        seen_uids: set[str] = set()
        jobs: list[Job] = []
        for query_jobs in per_query:
            for job in query_jobs:
                if job.uid not in seen_uids:
                    seen_uids.add(job.uid)
                    jobs.append(job)
        return jobs

    def _search(
        self,
        url: str,
        job_base: str,
        company: CompanyConfig,
        term: str,
        facets: dict[str, list[str]] | None = None,
    ) -> list[Job]:
        """Paginate one search (searchText and/or appliedFacets) to exhaustion.

        Multi-word queries return an unreliable ``total`` (often 0), so we page
        until a short/empty page rather than trusting the count.
        """
        jobs: list[Job] = []
        offset = 0
        for _ in range(MAX_PAGES):
            body = {
                "appliedFacets": facets or {},
                "limit": PAGE_LIMIT,
                "offset": offset,
                "searchText": term,
            }
            resp = request_with_retry(
                self.client, "POST", url, json=body, headers=_HEADERS
            )
            resp.raise_for_status()
            payload = resp.json()

            postings = payload.get("jobPostings", [])
            if not postings:
                break

            for p in postings:
                jobs.append(self._to_job(p, job_base, company))

            offset += PAGE_LIMIT
            if len(postings) < PAGE_LIMIT:  # last (short) page — stop
                break

        return jobs

    @staticmethod
    def _to_job(p: dict, job_base: str, company: CompanyConfig) -> Job:
        external_path = p.get("externalPath", "")
        full_url = job_base + external_path if external_path else job_base
        bullets = p.get("bulletFields") or []
        # bulletFields often carries the req id; use it as the stable id.
        external_id = str(bullets[0]) if bullets else external_path
        description = " | ".join(str(b) for b in bullets) or None
        return Job(
            source="workday",
            company=company.name,
            external_id=external_id,
            title=p.get("title", ""),
            location=p.get("locationsText"),
            url=full_url,
            description=description,
            posted_at=None,  # the list endpoint only gives a relative string…
            posted_note=p.get("postedOn"),  # …so keep it for display ("Posted Today")
        )
