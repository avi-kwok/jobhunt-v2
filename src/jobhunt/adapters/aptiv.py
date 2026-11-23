"""Aptiv careers adapter — public HawkSearch search-widget API, no auth.

Config mapping: `host` = the HawkSearch API host from the careers page's
careers-search-bar data-settings (SearchUrl), e.g. "aptivcareers.searchapi-na.hawksearch.com".
`slug` = the widget ClientId from the same data-settings (sent as ClientGuid in the body).
Both are shipped in the public careers page HTML; the widget needs no cookie or login.

robots.txt: the API host has no robots file (404, so no restriction); www.aptiv.com/en/jobs
is allowed. The search is keyword-based, so we query intern-like terms only and merge the
hits by posting id. Posting ids are the last path segment of link[0].
"""

from __future__ import annotations

from datetime import datetime

from ..models import CompanyConfig, Job
from .base import Adapter, html_to_text, register, request_with_retry

SEARCH_TERMS = ("intern", "co-op", "student")
MAX_PER_PAGE = 96  # widget maximum; one page covers all intern-like hits for a term today
MAX_PAGES = 5


def _first(doc: dict, key: str) -> str | None:
    vals = doc.get(key) or []
    return vals[0] if vals else None


def _dt(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


@register
class AptivAdapter(Adapter):
    ats = "aptiv"

    def fetch(self, company: CompanyConfig) -> list[Job]:
        if not company.host or not company.slug:
            raise ValueError(f"{company.name}: aptiv adapter needs a host and a slug")
        url = f"https://{company.host}/api/v2/search/"
        docs: dict[str, dict] = {}  # posting id -> Document
        for term in SEARCH_TERMS:
            for page in range(1, MAX_PAGES + 1):
                resp = request_with_retry(
                    self.client,
                    "POST",
                    url,
                    json={
                        "ClientGuid": company.slug,
                        "Keyword": term,
                        "Page": page,
                        "MaxPerPage": MAX_PER_PAGE,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                new = 0
                for hit in data.get("Results") or []:
                    doc = hit.get("Document") or {}
                    link = _first(doc, "link")
                    if not link or not _first(doc, "title"):
                        continue
                    pid = link.rsplit("/", 1)[-1]
                    if pid not in docs:
                        docs[pid] = doc
                        new += 1
                # shortcut: the widget ignored Page in testing (page 2 repeated page 1), so stop
                # on a page that adds nothing new; upgrade if Aptiv's hits exceed 96 per term.
                pages = (data.get("Pagination") or {}).get("NofPages") or 0
                if new == 0 or page >= pages:
                    break

        return [
            Job(
                source=self.ats,
                company=company.name,
                external_id=pid,
                title=_first(doc, "title") or "",
                url=_first(doc, "link") or "",
                location=_first(doc, "primarylocation"),
                department=_first(doc, "jobfamily"),
                description=html_to_text(_first(doc, "jobdescription")),
                posted_at=_dt(_first(doc, "created_moment")),
            )
            for pid, doc in docs.items()
        ]
