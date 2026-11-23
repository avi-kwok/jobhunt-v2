"""SmartRecruiters careers-site adapter — public careers.smartrecruiters.com pages (HTML), no auth.

Config mapping: `host` = careers.smartrecruiters.com, `slug` = the company identifier in the
careers URL (IFS1 for https://careers.smartrecruiters.com/IFS1).

The careers page renders postings server-side, grouped by location (`section[data-qty]` with an
`h3` location heading). The page advertises its location pages in `data-groups-pages`; pages 1..N-1
come from `/<slug>/api/groups?page=N`, the same HTML fragment. A location group renders only a few
cards; its 'Show more jobs' link (`js-more`, `data-value` and `data-type`) pages the rest through
`/<slug>/api/more?page=N&type=..&value=..`, where page 0 is the rendered part, so paging starts at 1.
robots.txt on careers.smartrecruiters.com allows the careers page, the groups feed and the more feed
(no crawl-delay). Requests are sequential, DELAY seconds apart, and capped at MAX_PAGES and MAX_MORE.
"""

from __future__ import annotations

import html
import re
import time

from ..models import CompanyConfig, Job
from .base import Adapter, register, request_with_retry

MAX_PAGES = 20  # shortcut: location pages per board (18 for ServiceNow), upgrade if a board has more
MAX_MORE = 120  # shortcut: 'Show more jobs' requests per board (~70 for ServiceNow), upgrade if a board needs more
DELAY = 2.0  # seconds between requests to the host (app politeness floor)
GROUPS_RE = re.compile(r'data-groups-pages="(\d+)"')
LOCATION_RE = re.compile(r"<h3[^>]*>(.*?)</h3>", re.S)
MORE_RE = re.compile(r'<a [^>]*data-value="([^"]*)" data-type="([^"]*)"[^>]*\bjs-more"')
TITLE_RE = re.compile(r"<h4[^>]*>(.*?)</h4>", re.S)
DEPT_RE = re.compile(r'<span class="spl-me-1">([^<]*)</span>')


def _clean(s: str) -> str:
    return " ".join(html.unescape(s).split())


@register
class SmartRecruitersCareersAdapter(Adapter):
    ats = "smartrecruiters_careers"

    def fetch(self, company: CompanyConfig) -> list[Job]:
        if not company.host or not company.slug:
            raise ValueError(f"{company.name}: smartrecruiters_careers adapter needs a host and a slug")
        base = f"https://{company.host}/{company.slug}"
        anchor = re.compile(
            rf'<a href="(https://jobs\.smartrecruiters\.com/{re.escape(company.slug)}/(\d+)-[^"]*)"'
            r'[^>]*js-job-ad-link">(.*?)</a>',
            re.S,
        )

        resp = request_with_retry(self.client, "GET", base, headers={"Accept": "text/html"})
        resp.raise_for_status()
        if 'id="st-openings"' not in resp.text:
            raise ValueError(f"{company.name}: smartrecruiters_careers page has no openings container")
        groups = GROUPS_RE.search(resp.text)
        pages = min(int(groups.group(1)) if groups else 1, MAX_PAGES)

        jobs: dict[str, Job] = {}
        more_requests = 0

        def add(text: str, location: str | None, section: set[str]) -> None:
            for url, job_id, body in anchor.findall(text):
                title = TITLE_RE.search(body)
                if not title:
                    continue
                section.add(job_id)
                if job_id not in jobs:
                    dept = DEPT_RE.search(body)
                    jobs[job_id] = Job(
                        source=self.ats,
                        company=company.name,
                        external_id=job_id,
                        title=_clean(title.group(1)),
                        location=location,
                        department=_clean(dept.group(1)) if dept else None,
                        url=url,
                    )

        def collect(text: str) -> None:
            nonlocal more_requests
            parts = re.split(r'<section data-qty="(\d+)"', text)
            for qty, chunk in zip(parts[1::2], parts[2::2]):
                loc = LOCATION_RE.search(chunk)
                location = _clean(loc.group(1)) if loc else None
                section: set[str] = set()
                add(chunk, location, section)
                more = MORE_RE.search(chunk)
                if not more:
                    continue
                page = 1
                while len(section) < int(qty) and more_requests < MAX_MORE:
                    time.sleep(DELAY)
                    more_requests += 1
                    resp = request_with_retry(
                        self.client, "GET",
                        f"{base}/api/more?search=&page={page}&type={more.group(2)}&value={more.group(1)}",
                        headers={"Accept": "text/html"},
                    )
                    resp.raise_for_status()
                    before = len(section)
                    add(resp.text, location, section)
                    if len(section) == before:
                        break
                    page += 1

        collect(resp.text)
        for page in range(1, pages):
            time.sleep(DELAY)
            resp = request_with_retry(
                self.client, "GET", f"{base}/api/groups", params={"page": page},
                headers={"Accept": "text/html"},
            )
            resp.raise_for_status()
            collect(resp.text)
        return list(jobs.values())
