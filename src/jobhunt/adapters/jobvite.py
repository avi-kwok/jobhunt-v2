"""Jobvite adapter — public jobs.jobvite.com/<slug>/jobs HTML list, no auth.

Config mapping: `slug` = the company token in jobs.jobvite.com/<slug>/jobs
(e.g. "absolute" for Absolute Security/Software). Host is fixed: jobs.jobvite.com.

One page lists every opening grouped by department (`h3.h2` heading followed by a
`table.jv-job-list`; rows have `td.jv-job-list-name > a` and `td.jv-job-list-location`).
Stdlib HTMLParser only. No dates/descriptions on the list; no detail fetches.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

from ..models import CompanyConfig, Job
from .base import Adapter, register, request_with_retry

HOST = "https://jobs.jobvite.com"
_PAGE_RE = re.compile(r"jv-page|jv-job-list")  # careers-site page chrome, present even when empty


class _ListParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.items: list[dict] = []
        self._dept: str | None = None
        self._h3 = False
        self._cell: str | None = None  # "name" | "loc"
        self._cur: dict | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = (a.get("class") or "").split()
        if tag == "h3" and "h2" in cls:
            self._h3 = True
            self._dept = ""
        elif tag == "td" and "jv-job-list-name" in cls:
            self._cell = "name"
            self._cur = {"href": None, "title": "", "loc": "", "dept": self._dept}
            self.items.append(self._cur)
        elif tag == "td" and "jv-job-list-location" in cls:
            self._cell = "loc"
        elif tag == "a" and self._cell == "name" and self._cur is not None:
            self._cur["href"] = a.get("href")

    def handle_endtag(self, tag):
        if tag == "h3":
            self._h3 = False
        elif tag == "td":
            self._cell = None

    def handle_data(self, data):
        if self._h3:
            self._dept = (self._dept or "") + data
        elif self._cell == "name" and self._cur is not None:
            self._cur["title"] += data
        elif self._cell == "loc" and self._cur is not None:
            self._cur["loc"] += data


@register
class JobviteAdapter(Adapter):
    ats = "jobvite"

    def fetch(self, company: CompanyConfig) -> list[Job]:
        if not company.slug:
            raise ValueError(f"{company.name}: jobvite adapter needs a slug")
        resp = request_with_retry(
            self.client, "GET", f"{HOST}/{company.slug}/jobs", headers={"Accept": "text/html"}
        )
        resp.raise_for_status()
        if not _PAGE_RE.search(resp.text):
            raise ValueError(f"{company.name}: jobvite page has no careers-site markup")
        p = _ListParser()
        p.feed(resp.text)

        jobs: list[Job] = []
        seen: set[str] = set()
        for it in p.items:
            href = it["href"] or ""
            jid = href.rstrip("/").rsplit("/", 1)[-1]
            title = " ".join(it["title"].split())
            if not jid or not title or jid in seen:
                continue
            seen.add(jid)
            jobs.append(
                Job(
                    source=self.ats,
                    company=company.name,
                    external_id=jid,
                    title=title,
                    location=" ".join(it["loc"].split()) or None,
                    department=" ".join((it["dept"] or "").split()) or None,
                    url=href if href.startswith("http") else HOST + href,
                )
            )
        return jobs
