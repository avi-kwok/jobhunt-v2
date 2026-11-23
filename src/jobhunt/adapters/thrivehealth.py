"""Thrive Health adapter — public careers page (static Webflow HTML), no auth.

Config mapping: `host` = www.thrive.health (the list lives at https://<host>/careers).

One page lists every open role (no search, no pagination, no detail pages), so a scan makes
one GET. Each role is a `div.job-card` holding the title (`div.job-title`), the location
(`span.job-tag`) and a "View role" link whose relative href is resolved against the careers
page. The slug in that href is the external id. robots.txt allows the page with no
crawl-delay. A page without the `jobs-list` container raises, so a redesign or challenge page
is not read as "no jobs".
"""

from __future__ import annotations

from html.parser import HTMLParser
from urllib.parse import urljoin

from ..models import CompanyConfig, Job
from .base import Adapter, register, request_with_retry


class _ListParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.container = False
        self.items: list[dict] = []
        self._cur: dict | None = None
        self._depth = 0
        self._field: str | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        classes = (a.get("class") or "").split()
        if tag == "div" and "jobs-list" in classes:
            self.container = True
        if self._cur is None:
            if tag == "div" and "job-card" in classes:
                self._cur = {"href": None, "title": "", "location": ""}
                self.items.append(self._cur)
                self._depth = 1
            return
        if tag == "div":
            self._depth += 1
            if "job-title" in classes:
                self._field = "title"
        elif tag == "span" and "job-tag" in classes:
            self._field = "location"
        elif tag == "a" and self._cur["href"] is None:
            self._cur["href"] = a.get("href")

    def handle_endtag(self, tag):
        if self._cur is None:
            return
        if tag == "div":
            self._depth -= 1
            self._field = None
            if self._depth == 0:
                self._cur = None

    def handle_data(self, data):
        if self._cur is not None and self._field:
            self._cur[self._field] += data


@register
class ThriveHealthAdapter(Adapter):
    ats = "thrivehealth"

    def fetch(self, company: CompanyConfig) -> list[Job]:
        if not company.host:
            raise ValueError(f"{company.name}: thrivehealth adapter needs a host")
        base = f"https://{company.host}/careers"
        resp = request_with_retry(self.client, "GET", base, headers={"Accept": "text/html"})
        resp.raise_for_status()
        p = _ListParser()
        p.feed(resp.text)
        if not p.container:
            raise ValueError(f"{company.name}: thrivehealth page has no job list container")

        jobs: list[Job] = []
        seen: set[str] = set()
        for it in p.items:
            href = (it["href"] or "").strip()
            slug = href.rstrip("/").rsplit("/", 1)[-1]
            title = " ".join(it["title"].split())
            if not slug or not title or slug in seen:
                continue
            seen.add(slug)
            jobs.append(
                Job(
                    source=self.ats,
                    company=company.name,
                    external_id=slug,
                    title=title,
                    url=urljoin(base, href),
                    location=" ".join(it["location"].split()) or None,
                )
            )
        return jobs
