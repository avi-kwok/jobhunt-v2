"""Radancy / TalentBrew adapter (Intuit, Synopsys, ...) — sitemap + capped detail pages.

TalentBrew career sites expose no JSON API, and robots.txt disallows ``/search-jobs/``
(the search UI + its results endpoint). What *is* allowed and public: ``/sitemap.xml``
(every posting as ``/job/<city>/<title-slug>/<org-id>/<job-id>``) and the ``/job/...``
pages themselves, which carry a schema.org JobPosting JSON-LD block.

Strategy (2 + up to MAX_DETAILS requests per scan):
1. GET /sitemap.xml once; keep only postings whose title slug looks like an internship
   (intern|co-op|student|new grad). Everything else is skipped, so a 600-job board
   yields a few dozen candidates.
2. For the newest MAX_DETAILS candidates, GET the posting page (>=1 s apart) and read
   the JSON-LD for the exact title, location, description and datePosted. If a detail
   request fails or is challenged (non-200) we stop fetching details and fall back to
   slug-derived title/location for the rest — we never retry around a block.

Config mapping: ``host`` = careers hostname (e.g. ``jobs.intuit.com``); it is also the
per-host politeness key. ``slug`` unused.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from xml.etree import ElementTree

import httpx

from ..models import CompanyConfig, Job
from .base import Adapter, html_to_text, register, request_with_retry

MAX_DETAILS = 25
DETAIL_DELAY = 1.0  # seconds between posting-page requests (politeness: ~1 req/s)

_HEADERS = {"Accept": "text/html,application/xml"}
_JOB_PATH_RE = re.compile(r"/job/([^/]+)/([^/]+)/(\d+)/(\d+)/?$")
_INTERN_SLUG_RE = re.compile(
    r"(?:^|-)(?:interns?|internships?|co-?ops?|students?|new-grad(?:uates?)?)(?:-|$)"
)
_LD_RE = re.compile(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S)


def _slug_title(slug: str) -> str:
    return re.sub(r"\s+", " ", slug.replace("-", " ")).strip().title()


def _dt(value) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _ld_posting(html: str) -> dict | None:
    for m in _LD_RE.finditer(html):
        try:
            data = json.loads(m.group(1))
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("@type") == "JobPosting":
            return data
    return None


def _ld_location(ld: dict) -> str | None:
    locs = ld.get("jobLocation") or []
    if isinstance(locs, dict):
        locs = [locs]
    out: list[str] = []
    for loc in locs:
        a = (loc or {}).get("address") or {}
        parts = [a.get("addressLocality"), a.get("addressRegion"), a.get("addressCountry")]
        s = ", ".join(str(p) for p in parts if p)
        if s and s not in out:
            out.append(s)
    return "; ".join(out) or None


@register
class RadancyAdapter(Adapter):
    ats = "radancy"

    def fetch(self, company: CompanyConfig) -> list[Job]:
        if not company.host:
            raise ValueError(f"{company.name}: radancy adapter needs a host")
        base = f"https://{company.host}"
        resp = request_with_retry(self.client, "GET", base + "/sitemap.xml", headers=_HEADERS)
        resp.raise_for_status()
        cands = self._candidates(resp.text, base)

        jobs: list[Job] = []
        fetch_details = True
        for n, (job_id, url, city, slug) in enumerate(cands):
            ld = None
            if fetch_details and n < MAX_DETAILS:
                if n:
                    time.sleep(DETAIL_DELAY)
                try:
                    r = request_with_retry(self.client, "GET", url, headers=_HEADERS)
                except httpx.HTTPError:
                    fetch_details = False
                else:
                    if r.status_code == 200:
                        ld = _ld_posting(r.text)
                    else:
                        fetch_details = False
            jobs.append(self._to_job(company, job_id, url, city, slug, ld))
        return jobs

    @staticmethod
    def _candidates(sitemap_xml: str, base: str) -> list[tuple[str, str, str, str]]:
        root = ElementTree.fromstring(sitemap_xml)
        if not (root.tag.endswith("urlset") or root.tag.endswith("sitemapindex")):
            raise ValueError(f"unexpected sitemap root <{root.tag}>")
        found: dict[str, tuple[str, str, str, str]] = {}
        locs = job_urls = 0
        for el in root.iter():
            if not el.tag.endswith("}loc") and el.tag != "loc":
                continue
            locs += 1
            url = (el.text or "").strip()
            m = _JOB_PATH_RE.search(url)
            if m:
                job_urls += 1
            if not m or not _INTERN_SLUG_RE.search(m.group(2)):
                continue
            found.setdefault(m.group(4), (m.group(4), url, m.group(1), m.group(2)))
        if locs and not job_urls and root.tag.endswith("urlset"):
            raise ValueError("sitemap has URLs but no /job/ postings (layout changed?)")
        # newest first (ids grow over time) so the detail cap covers fresh postings
        return sorted(found.values(), key=lambda c: int(c[0]), reverse=True)

    def _to_job(self, company, job_id, url, city, slug, ld) -> Job:
        ld = ld or {}
        return Job(
            source=self.ats,
            company=company.name,
            external_id=job_id,
            title=(ld.get("title") or "").strip() or _slug_title(slug),
            location=_ld_location(ld) or _slug_title(city),
            employment_type=ld.get("employmentType") or None,
            url=url,
            description=html_to_text(ld.get("description")),
            posted_at=_dt(ld.get("datePosted")),
        )
