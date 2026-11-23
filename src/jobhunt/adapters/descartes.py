"""Descartes Systems adapter — server-rendered WordPress job grid, no auth.

Config mapping: `host` = careers.descartes.com (the careers site hostname).

GET https://{host}/join-our-team/ and /join-our-team/page/N/ while the grid links to the
next page. robots.txt allows the path with Crawl-delay: 10, so pages are fetched DELAY
seconds apart. Each `article.job_listing` carries the posting URL in `data-link`, the title
in `h2.entry-title`, the date in `span.published` and the location in the
`dp-dfg-cf-_job_location` field.
"""

from __future__ import annotations

import re
import time
from datetime import datetime
from html import unescape

from ..models import CompanyConfig, Job
from .base import Adapter, register, request_with_retry

PATH = "/join-our-team/"
DELAY = 10.0  # robots.txt Crawl-delay
MAX_PAGES = 10
ARTICLE_RE = re.compile(r"<article\b([^>]*\bjob_listing\b[^>]*)>(.*?)</article>", re.S)
LINK_RE = re.compile(r'data-link="([^"]+)"')
TITLE_RE = re.compile(r'<h2 class="entry-title">(.*?)</h2>', re.S)
DATE_RE = re.compile(r'<span class="published">(.*?)</span>', re.S)
LOCATION_RE = re.compile(r'dp-dfg-cf-_job_location.*?dp-dfg-custom-field-value">(.*?)</span>', re.S)
CONTAINER = "dp-dfg-items"


def _text(pattern: re.Pattern, body: str) -> str | None:
    m = pattern.search(body)
    return unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip() if m else None


def _date(value: str | None) -> datetime | None:
    try:
        return datetime.strptime(value, "%B %d, %Y") if value else None
    except ValueError:
        return None


@register
class DescartesAdapter(Adapter):
    ats = "descartes"

    def fetch(self, company: CompanyConfig) -> list[Job]:
        if not company.host:
            raise ValueError(f"{company.name}: descartes adapter needs a host")
        base = f"https://{company.host}{PATH}"
        jobs: list[Job] = []
        seen: set[str] = set()
        for page in range(1, MAX_PAGES + 1):
            if page > 1:
                time.sleep(DELAY)
            url = base if page == 1 else f"{base}page/{page}/"
            resp = request_with_retry(self.client, "GET", url)
            resp.raise_for_status()
            if CONTAINER not in resp.text:
                raise ValueError(f"{company.name}: descartes page has no job grid")
            for attrs, body in ARTICLE_RE.findall(resp.text):
                link = LINK_RE.search(attrs)
                if not link:
                    continue
                posting_url = unescape(link.group(1))
                external_id = posting_url.rstrip("/").rsplit("/", 1)[-1]
                if external_id in seen:
                    continue
                seen.add(external_id)
                jobs.append(
                    Job(
                        source=self.ats,
                        company=company.name,
                        external_id=external_id,
                        title=_text(TITLE_RE, body) or "",
                        url=posting_url,
                        location=_text(LOCATION_RE, body),
                        posted_at=_date(_text(DATE_RE, body)),
                    )
                )
            if f"/page/{page + 1}/" not in resp.text:
                break
        return jobs
