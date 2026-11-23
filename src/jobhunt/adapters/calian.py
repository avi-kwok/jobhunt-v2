"""Calian adapter — WordPress REST API for the public `career` post type, no auth.

Config mapping: `host` = careers.calian.com (the careers site hostname).

GET https://{host}/wp-json/wp/v2/career?per_page=100&page=N, paged until the
`x-wp-totalpages` header is reached (hard page cap). robots.txt allows the path and
sets no crawl-delay. Postings carry no taxonomy or ACF location, so the location is read
from "Location: City, PR" lines in the posting body when present.
"""

from __future__ import annotations

import html
import re
from datetime import datetime

from ..models import CompanyConfig, Job
from .base import Adapter, html_to_text, register, request_with_retry

BASE = "https://{host}/wp-json/wp/v2/career"
PER_PAGE = 100
MAX_PAGES = 10
LOCATION_RE = re.compile(r"Location\s*\d*\s*:\s*([^\n:]{2,60}?)\s*(?:\n|$)")


def _dt(value) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def _location(content_html: str) -> str | None:
    text = html.unescape(re.sub(r"<[^>]+>", "\n", content_html))
    found = list(dict.fromkeys(m.strip() for m in LOCATION_RE.findall(text)))
    return " | ".join(found) or None


@register
class CalianAdapter(Adapter):
    ats = "calian"

    def fetch(self, company: CompanyConfig) -> list[Job]:
        if not company.host:
            raise ValueError(f"{company.name}: calian adapter needs a host")
        url = BASE.format(host=company.host)
        jobs: list[Job] = []
        seen: set[str] = set()
        total_pages = 1
        page = 1
        while page <= min(total_pages, MAX_PAGES):
            resp = request_with_retry(
                self.client, "GET", url, params={"per_page": PER_PAGE, "page": page}
            )
            resp.raise_for_status()
            total_pages = int(resp.headers.get("x-wp-totalpages") or 1)
            items = resp.json()
            if not items:
                break
            for it in items:
                external_id = str(it.get("id"))
                if external_id in seen or not it.get("link"):
                    continue
                seen.add(external_id)
                content = (it.get("content") or {}).get("rendered") or ""
                jobs.append(
                    Job(
                        source=self.ats,
                        company=company.name,
                        external_id=external_id,
                        title=html.unescape((it.get("title") or {}).get("rendered") or ""),
                        url=it["link"],
                        location=_location(content),
                        description=html_to_text(content),
                        posted_at=_dt(it.get("date")),
                    )
                )
            page += 1
        return jobs
