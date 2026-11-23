"""Clinch career sites — public /jobs/search listing pages (server-rendered HTML), no auth.

Config mapping: `host` = the career-site hostname (e.g. "careers.etsy.com"); the listing is
https://{host}/jobs/search?page=N. The host is also the politeness key.

Two layouts share the same anchors (a[id^=link_job_title] with href /jobs/<slug>): a card
layout (li.job-component-location, li.job-component-department, ...) and a table layout
(td.job-search-results-location, td.job-search-results-department, ...). Each job's fields
follow its title anchor, so one parser serves both. Pages follow rel="next" links.
robots.txt: /jobs/search is allowed (Crawl-delay 5), so pages are fetched DELAY seconds apart.
"""

from __future__ import annotations

import re
import time
from html.parser import HTMLParser
from urllib.parse import urljoin

from ..models import CompanyConfig, Job
from .base import Adapter, html_to_text, register, request_with_retry

MAX_PAGES = 20
DELAY = 5.0  # robots.txt Crawl-delay
_BOARD_RE = re.compile(r"job-search-results")  # listing container; a blank or challenge page lacks it
VOID = {"br", "img", "input", "hr", "meta", "link", "source", "wbr"}
# class token -> field name; exact token match so filter widgets (e.g. *-location-toggle) skip
FIELDS = {
    "job-component-location": "location",
    "job-search-results-location": "location",
    "job-component-workplace-type": "workplace",
    "job-search-results-workplace-types": "workplace",
    "job-component-department": "department",
    "job-search-results-department": "department",
    "job-component-employment-type": "employment_type",
    "job-search-results-summary": "summary",
}


class _ListParser(HTMLParser):
    def __init__(self, base: str) -> None:
        super().__init__()
        self.base = base
        self.jobs: list[dict] = []
        self.has_next = False
        self._cap: str | None = None  # field being captured
        self._depth = 0  # open tags inside the captured element
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a" and a.get("rel") == "next":
            self.has_next = True
        if self._cap is not None:
            if tag not in VOID:
                self._depth += 1
            return
        if tag == "a" and (a.get("id") or "").startswith("link_job_title") and a.get("href"):
            self.jobs.append({"href": urljoin(self.base, a["href"])})
            self._start("title")
            return
        for token in (a.get("class") or "").split():
            if token in FIELDS:
                self._start(FIELDS[token])
                return

    def handle_endtag(self, tag):
        if self._cap is None or tag in VOID:
            return
        if self._depth:
            self._depth -= 1
            return
        if self.jobs:  # table headers repeat the field classes before the first job
            self.jobs[-1].setdefault(self._cap, []).append("".join(self._buf))
        self._cap = None

    def handle_data(self, data):
        if self._cap is not None:
            self._buf.append(data)

    def _start(self, field: str) -> None:
        self._cap, self._depth, self._buf = field, 0, []


def _clean(parts: list[str]) -> str:
    return " | ".join(" ".join(p.split()) for p in parts if p.strip())


@register
class ClinchAdapter(Adapter):
    ats = "clinch"

    def fetch(self, company: CompanyConfig) -> list[Job]:
        if not company.host:
            raise ValueError(f"{company.name}: clinch adapter needs a host")
        base = f"https://{company.host}/"
        jobs: dict[str, Job] = {}
        for page in range(1, MAX_PAGES + 1):
            if page > 1:
                time.sleep(DELAY)
            resp = request_with_retry(
                self.client, "GET", f"{base}jobs/search", params={"page": page},
                headers={"Accept": "text/html"},
            )
            resp.raise_for_status()
            if not _BOARD_RE.search(resp.text):
                raise ValueError(f"{company.name}: clinch page has no job list container")
            p = _ListParser(base)
            p.feed(resp.text)
            for it in p.jobs:
                slug = it["href"].split("/jobs/", 1)[-1].split("?", 1)[0].split("#", 1)[0]
                title = _clean(it.get("title", []))
                if not slug or slug == "search" or not title or slug in jobs:
                    continue
                location = _clean(it.get("location", [])) or _clean(it.get("workplace", []))
                jobs[slug] = Job(
                    source=self.ats,
                    company=company.name,
                    external_id=slug,
                    title=title,
                    url=it["href"],
                    location=location or None,
                    department=_clean(it.get("department", [])) or None,
                    employment_type=_clean(it.get("employment_type", [])) or None,
                    description=html_to_text("".join(it.get("summary", []))),
                )
            if not p.jobs or not p.has_next:
                break
        return list(jobs.values())
