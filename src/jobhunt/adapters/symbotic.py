"""Symbotic adapter — public careers page (server-rendered HTML), no auth.

Config mapping: `host` = www.symbotic.com (the careers list lives at https://<host>/careers/jobs/).

robots.txt allows /careers/jobs/ with Crawl-delay: 10. A scan makes one GET to that page, so
the delay never applies and no sleep is needed. The page lists every opening in the markup:
visible rows are `div.job` and rows hidden by the client-side filter are `div.job d-none`;
both are parsed. Title and URL come from the `job__content-title` link, department from
`data-category-title`, and location from `data-location-title` ("USA Wilmer, TX" becomes
"Wilmer, TX, USA"). A page without the `block-job-listings` container raises, so a redesign
or challenge page is not read as "no jobs".
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import urljoin

from ..models import CompanyConfig, Job
from .base import Adapter, register, request_with_retry

ID_RE = re.compile(r"/careers/jobs/(R\d+)")
COUNTRY_PREFIX_RE = re.compile(r"^([A-Z]{2,3}) (.+)$")


class _ListParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.container = False
        self.items: list[dict] = []
        self._cur: dict | None = None
        self._depth = 0
        self._in_title = False
        self._in_a = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        classes = (a.get("class") or "").split()
        if "block-job-listings" in classes:
            self.container = True
        if self._cur is None:
            if tag == "div" and "job" in classes:
                self._cur = {"attrs": a, "href": None, "title": ""}
                self.items.append(self._cur)
                self._depth = 1
            return
        if tag == "div":
            self._depth += 1
        elif tag == "p" and "job__content-title" in classes:
            self._in_title = True
        elif tag == "a" and self._in_title and self._cur["href"] is None:
            self._cur["href"] = a.get("href")
            self._in_a = True

    def handle_endtag(self, tag):
        if self._cur is None:
            return
        if tag == "div":
            self._depth -= 1
            if self._depth == 0:
                self._cur = None
                self._in_title = self._in_a = False
        elif tag == "a":
            self._in_a = False

    def handle_data(self, data):
        if self._in_a:
            self._cur["title"] += data


def _location(raw: str | None) -> str | None:
    text = " ".join((raw or "").split())
    m = COUNTRY_PREFIX_RE.match(text)
    if m:
        text = f"{m.group(2)}, {m.group(1)}"
    return text or None


@register
class SymboticAdapter(Adapter):
    ats = "symbotic"

    def fetch(self, company: CompanyConfig) -> list[Job]:
        if not company.host:
            raise ValueError(f"{company.name}: symbotic adapter needs a host")
        base = f"https://{company.host}/careers/jobs/"
        resp = request_with_retry(self.client, "GET", base, headers={"Accept": "text/html"})
        resp.raise_for_status()
        p = _ListParser()
        p.feed(resp.text)
        if not p.container:
            raise ValueError(f"{company.name}: symbotic page has no job list container")

        jobs: list[Job] = []
        seen: set[str] = set()
        for it in p.items:
            m = ID_RE.search(it["href"] or "")
            title = " ".join(it["title"].split())
            if not m or not title or m.group(1) in seen:
                continue
            seen.add(m.group(1))
            attrs = it["attrs"]
            department = " ".join((attrs.get("data-category-title") or "").split())
            jobs.append(
                Job(
                    source=self.ats,
                    company=company.name,
                    external_id=m.group(1),
                    title=title,
                    url=urljoin(base, it["href"]),
                    location=_location(attrs.get("data-location-title")),
                    department=department or None,
                )
            )
        return jobs
