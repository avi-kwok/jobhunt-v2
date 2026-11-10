"""Renaissance Technologies adapter — custom server-rendered careers page, no auth.

Config: no slug/site needed (one fixed public page). Set ``host`` = ``www.rentec.com``
so the scheduler's politeness key matches the real hostname.

``GET https://www.rentec.com/Careers.action?jobs=true`` lists every opening on one page
(no search, no pagination, no dates; robots.txt allows all). Parsed with the stdlib
HTMLParser: each ``h2.Subhead_heading`` is a department, each
``<a href="...&selectedPosition=<key>">`` is a posting (key = external_id, link text =
title), and the next ``<div>`` after the link holds the location. Intern-like titles get
a capped detail fetch for the description. A page without the "Jobs - Renaissance" title
and a ``<main>`` element (redesign, challenge page) raises instead of returning no jobs;
the real page with zero links is a valid empty board.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

import httpx

from ..models import CompanyConfig, Job
from .base import Adapter, html_to_text, register, request_with_retry

BASE = "https://www.rentec.com/Careers.action"
LIST_URL = f"{BASE}?jobs=true"
MAX_DETAILS = 5
INTERN_RE = re.compile(r"\bintern(?:s|ship|ships)?\b|\bco-?ops?\b|\bstudent|new[ -]?grad", re.I)
# Structural marker of the real jobs page (kept even when no positions are listed).
_MARKER_RE = re.compile(r"<title>\s*Jobs\b[^<]*Renaissance", re.I)
# Detail body: the main column up to the apply form.
_DETAIL_RE = re.compile(r'class="lg:l-col-8">(.*?)<div id="apply"', re.S)


def _key(href: str) -> str | None:
    vals = parse_qs(urlsplit(href).query).get("selectedPosition")
    return vals[0].strip() if vals and vals[0].strip() else None


class _ListParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.items: list[dict] = []
        self._dept = ""
        self._in_dept = False
        self._cur: dict | None = None
        self._in_a = False
        self._loc_pending = False
        self._loc_depth = 0  # >0 while inside the location div

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "h2" and "Subhead_heading" in (a.get("class") or ""):
            self._in_dept, self._dept = True, ""
            self._cur, self._loc_pending, self._loc_depth = None, False, 0
        elif tag == "a" and (key := _key(a.get("href") or "")):
            self._cur = {"key": key, "title": "", "location": "", "department": self._dept}
            self.items.append(self._cur)
            self._in_a, self._loc_pending, self._loc_depth = True, False, 0
        elif tag == "div" and self._cur is not None:
            if self._loc_pending:
                self._loc_pending, self._loc_depth = False, 1
            elif self._loc_depth:
                self._loc_depth += 1

    def handle_endtag(self, tag):
        if tag == "h2":
            self._in_dept = False
        elif tag == "a" and self._in_a:
            self._in_a, self._loc_pending = False, True
        elif tag == "div" and self._loc_depth:
            self._loc_depth -= 1
            if not self._loc_depth:
                self._cur = None

    def handle_data(self, data):
        if self._in_dept:
            self._dept += data
        elif self._cur is not None and self._in_a:
            self._cur["title"] += data
        elif self._cur is not None and self._loc_depth:
            self._cur["location"] += data


def _clean(text: str) -> str:
    return " ".join(text.replace("\xa0", " ").split())


@register
class RentecAdapter(Adapter):
    ats = "rentec"

    def fetch(self, company: CompanyConfig) -> list[Job]:
        resp = request_with_retry(self.client, "GET", LIST_URL, headers={"Accept": "text/html"})
        resp.raise_for_status()
        html = resp.text
        if not _MARKER_RE.search(html) or "<main" not in html:
            raise ValueError(f"{company.name}: rentec response is not the jobs page")
        parser = _ListParser()
        parser.feed(html)

        jobs: list[Job] = []
        seen: set[str] = set()
        details = 0
        for it in parser.items:
            key, title = it["key"], _clean(it["title"])
            if key in seen or not title:
                continue
            seen.add(key)
            url = f"{LIST_URL}&selectedPosition={key}"
            description = None
            if INTERN_RE.search(title) and details < MAX_DETAILS:
                details += 1
                description = self._description(url)
            jobs.append(
                Job(
                    source=self.ats,
                    company=company.name,
                    external_id=key,
                    title=title,
                    location=_clean(it["location"]) or None,
                    department=_clean(it["department"]) or None,
                    url=url,
                    description=description,
                )
            )
        return jobs

    def _description(self, url: str) -> str | None:
        try:
            r = request_with_retry(self.client, "GET", url, headers={"Accept": "text/html"})
            r.raise_for_status()
        except httpx.HTTPError:
            return None
        m = _DETAIL_RE.search(r.text)
        return html_to_text(m.group(1)) if m else None
