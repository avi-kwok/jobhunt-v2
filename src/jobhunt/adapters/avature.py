"""Avature adapter — server-rendered career-site search pages (no API, no auth).

Config (``CompanyConfig``):
  * ``host`` (required) — site host, also the scheduler's politeness key
    (``jobs.ea.com``, ``careers.lululemon.com``, ``bloomberg.avature.net``,
    ``synopsys.avature.net``, ``careers.twosigma.com``).
  * ``site`` (optional) — path of the job-list page, default ``careers/SearchJobs``.
    Some portals name it differently (Two Sigma: ``careers/OpenRoles``).

Each term is fetched as ``https://{host}/{site}?search=<term>&jobOffset=N``. Page size is
chosen by the portal (6-20), so the offset advances by the number of cards received.
Results are relevance ordered (title hits first) and the search is fuzzy/substring
(``intern`` also hits "International" and description text), so paging stops at the first
page with no intern-like title; ``MAX_PAGES`` is a hard cap. The RSS feed is not used: it
ignores the search term. Cards carry no description (Lululemon shows a snippet, kept).
A page with neither job cards nor the search form (``listFilterMode``) raises ValueError.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from html import unescape

from ..models import CompanyConfig, Job
from .base import Adapter, html_to_text, register, request_with_retry

TERMS = ("intern", "co-op", "student", "new grad")
INTERN_RE = re.compile(r"\bintern(?:s|ship|ships)?\b|\bco-?ops?\b|\bstudent|new[ -]?grad", re.I)
DEFAULT_SITE = "careers/SearchJobs"
MAX_PAGES = 6
DELAY = 1.0

_ARTICLE_RE = re.compile(r"<article\b.*?</article>", re.S)
_LINK_RE = re.compile(r'<a\b[^>]*href="([^"]*/JobDetail/([^"?#]+))[^"]*"[^>]*>(.*?)</a>', re.S)
_SPAN_RE = re.compile(r"<span\b([^>]*)>(.*?)</span>", re.S)
_CONTENT_RE = re.compile(r'<div class="article__content">(.*?)</div>', re.S)
_POSTED_RE = re.compile(r"Posted\s+(\d{1,2}-[A-Za-z]{3}-\d{4})")


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


@register
class AvatureAdapter(Adapter):
    ats = "avature"

    def __init__(self, client=None):
        super().__init__(client)
        self._last = 0.0

    def _get(self, url: str, params: dict):
        wait = DELAY - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        try:
            resp = request_with_retry(self.client, "GET", url, params=params)
        finally:
            self._last = time.monotonic()
        resp.raise_for_status()
        return resp.text

    def fetch(self, company: CompanyConfig) -> list[Job]:
        if not company.host:
            raise ValueError(f"{company.name}: avature adapter needs a host")
        url = f"https://{company.host}/{(company.site or DEFAULT_SITE).strip('/')}"
        self._last = 0.0
        seen: dict[str, Job] = {}
        for term in TERMS:
            offset = 0
            for _ in range(MAX_PAGES):
                html = self._get(url, {"search": term, "jobOffset": offset})
                cards = _ARTICLE_RE.findall(html)
                jobs = [j for j in (self._job(c, company, company.host) for c in cards) if j]
                if not jobs and "listFilterMode" not in html:
                    raise ValueError(f"{company.host}: unexpected Avature page (no job cards or search form)")
                for j in jobs:
                    seen.setdefault(j.external_id, j)
                if not jobs or not any(INTERN_RE.search(j.title) for j in jobs):
                    break
                offset += len(jobs)
        return list(seen.values())

    def _job(self, card: str, company: CompanyConfig, host: str) -> Job | None:
        m = _LINK_RE.search(card)
        if not m:
            return None
        href, tail, anchor = m.group(1), m.group(2), m.group(3)
        title = _text(anchor)
        ext = tail.rstrip("/").split("/")[-1]
        if not title or not ext:
            return None
        head = re.split(r'<div class="article__(?:content|footer)', card, maxsplit=1)[0]
        by_class: dict[str, str] = {}
        plain: list[str] = []
        for attrs, inner in _SPAN_RE.findall(head):
            txt = _text(inner)
            if not txt or "separator" in attrs:
                continue
            c = re.search(r"list-item-(\w+)", attrs)
            if c:
                by_class[c.group(1)] = txt
            elif "<span" not in inner:
                plain.append(txt)
        location = by_class.get("location") or (plain.pop(0) if plain else None)
        department = by_class.get("department") or (plain.pop(0) if plain else None)
        etype = by_class.get("workerType") or by_class.get("hireType") or (plain.pop(0) if plain else None)
        posted = None
        pm = _POSTED_RE.search(by_class.get("posted", ""))
        if pm:
            try:
                posted = datetime.strptime(pm.group(1), "%d-%b-%Y").replace(tzinfo=timezone.utc)
            except ValueError:
                pass
        cm = _CONTENT_RE.search(card)
        return Job(
            source=self.ats,
            company=company.name,
            external_id=ext,
            title=title,
            location=location,
            department=department,
            employment_type=etype,
            url=unescape(href),
            description=html_to_text(cm.group(1)) if cm else None,
            posted_at=posted,
        )
