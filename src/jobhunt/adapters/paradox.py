"""Paradox-hosted career sites (e.g. careers.loblaw.ca) — public sitemap + capped posting pages.

Paradox-hosted career sites render their job search client-side, and the keyword search
page is a poor list: a keyword of ``intern`` also returns every "Interim ..." posting, and
paging it costs one request per 10 results. ``/sitemap.xml`` (an index, then one
``sitemap-<hash>-en.xml`` urlset) is allowed by robots.txt and lists every posting as
``/<title-slug>/job/<requisition id>``. Postings whose slug names an internship are kept
and, for up to ``MAX_DETAILS`` of them, the posting page is fetched for its
``application/ld+json`` JobPosting (exact title, location, description, datePosted).

Requests per scan: 2 (sitemap index + job sitemap) + one per kept posting, capped.
Postings without a usable detail page keep a slug-derived title and no location.

Config mapping: ``host`` = career-site hostname (e.g. ``careers.loblaw.ca``); it is
also the per-host politeness key. ``slug``, ``site`` and ``tenant`` are unused.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from urllib.parse import unquote
from xml.etree import ElementTree

import httpx

from ..models import CompanyConfig, Job
from .base import Adapter, html_to_text, register, request_with_retry

MAX_DETAILS = 20  # posting pages per scan
DETAIL_DELAY = 2.0  # seconds between posting pages
_HEADERS = {"Accept": "application/xml,text/xml,text/html"}

# Title terms only: "Interim" must not match, and intern/co-op slugs carry the title.
INTERN_RE = re.compile(r"(?<![a-z])(?:interns?|internships?|co-?ops?)(?![a-z])", re.I)
_SITEMAP_RE = re.compile(r"-en\.xml$")
_JOB_RE = re.compile(r"/job/([^/]+)/?$")  # requisition ids look like R2000646938
_LD_RE = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)
_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _posting(page: str) -> dict | None:
    """The JobPosting JSON-LD block of a posting page; None if there is none."""
    for raw in _LD_RE.findall(page):
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("@type") == "JobPosting":
            return data
    return None


def _location(data: dict) -> str | None:
    """'Barrie, ON, CA' per place; several places joined with ' | '."""
    places = []
    for place in data.get("jobLocation") or []:
        addr = (place or {}).get("address") or {}
        parts = [addr.get("addressLocality"), addr.get("addressRegion"), addr.get("addressCountry")]
        text = ", ".join(p.strip() for p in parts if p and p.strip())
        if text:
            places.append(text)
    return " | ".join(places) or None


def _posted(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


@register
class ParadoxAdapter(Adapter):
    ats = "paradox"

    def fetch(self, company: CompanyConfig) -> list[Job]:
        if not company.host:
            raise ValueError(f"{company.name}: paradox adapter needs host")
        base = f"https://{company.host}"
        index = self._xml(f"{base}/sitemap.xml")
        locs = [el.text.strip() for el in index.iter(f"{_NS}loc") if el.text]
        jobs_sitemap = next((u for u in locs if _SITEMAP_RE.search(u)), None)
        if not jobs_sitemap:
            raise ValueError(f"{company.name}: sitemap index has no -en.xml job sitemap")

        candidates: dict[str, tuple[str, str]] = {}  # requisition id -> (url, slug)
        urlset = self._xml(jobs_sitemap)
        for el in urlset.iter(f"{_NS}loc"):
            url = (el.text or "").strip()
            m = _JOB_RE.search(url)
            if not m:
                continue
            slug = unquote(url.split("/")[-3])
            if INTERN_RE.search(slug):
                candidates.setdefault(m.group(1), (url, slug))

        jobs: list[Job] = []
        fetch_details = True
        for n, (req_id, (url, slug)) in enumerate(candidates.items()):
            data = None
            if fetch_details and n < MAX_DETAILS:
                if n:
                    time.sleep(DETAIL_DELAY)
                try:
                    resp = request_with_retry(self.client, "GET", url, headers=_HEADERS)
                except httpx.HTTPError:
                    fetch_details = False
                else:
                    if resp.status_code == 200:
                        data = _posting(resp.text)
                    if data is None:  # non-200 or no JSON-LD: stop, no workaround
                        fetch_details = False
            title = (data or {}).get("title") or slug.replace("-", " ")
            jobs.append(Job(
                source=self.ats,
                company=company.name,
                external_id=req_id,
                title=title,
                location=_location(data) if data else None,
                url=url,
                description=html_to_text((data or {}).get("description")),
                posted_at=_posted((data or {}).get("datePosted")),
            ))
        return jobs

    def _xml(self, url: str) -> ElementTree.Element:
        resp = request_with_retry(self.client, "GET", url, headers=_HEADERS)
        resp.raise_for_status()
        try:
            return ElementTree.fromstring(resp.content)
        except ElementTree.ParseError as exc:
            raise ValueError(f"{url}: not XML (challenge/redesign?)") from exc
