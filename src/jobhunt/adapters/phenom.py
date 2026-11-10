"""Phenom People adapter — the public ``/widgets`` search endpoint the careers page calls.

Config mapping (no new CompanyConfig fields):
  host  = careers site hostname, e.g. "careers.cboe.com" (also the politeness key)
  slug  = Phenom ``refNum`` of the site, e.g. "CBJCGMUS" (from ``phApp.ddo`` on any page)
  site  = "<country>/<lang>" path prefix of the site, e.g. "us/en", "ca/en", "global/en"

POST https://{host}/widgets with ddoKey ``refineSearch`` and a ``keywords`` query. We run the
union of SEARCH_TERMS (intern / co-op / student) server-side, page with ``from``/``size`` up to
MAX_PAGES, and dedupe by jobId. Descriptions use the list payload's ``descriptionTeaser`` (no
per-job detail requests).
"""

from __future__ import annotations

import re
from datetime import datetime

from ..models import CompanyConfig, Job
from .base import Adapter, html_to_text, register, request_with_retry

SEARCH_TERMS = ("intern", "co-op", "student")
PAGE_SIZE = 50
MAX_PAGES = 6  # per search term (=300 postings)


def _parse_date(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        try:  # "+0000" offset without colon on older Pythons
            return datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%S.%f%z")
        except ValueError:
            return None


def _slug(title: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", title).strip("-") or "job"


@register
class PhenomAdapter(Adapter):
    ats = "phenom"

    def fetch(self, company: CompanyConfig) -> list[Job]:
        missing = [f for f in ("host", "slug", "site") if not getattr(company, f)]
        if missing:
            raise ValueError(f"{company.name}: phenom adapter needs {', '.join(missing)}")
        country, _, lang = company.site.strip("/").partition("/")
        lang = lang or "en"
        base = f"https://{company.host}"

        seen: set[str] = set()
        jobs: list[Job] = []
        for term in SEARCH_TERMS:
            for raw in self._search(base, company, country, lang, term):
                job = self._to_job(raw, base, country, lang, company)
                if job and job.external_id not in seen:
                    seen.add(job.external_id)
                    jobs.append(job)
        return jobs

    def _search(self, base, company, country, lang, term) -> list[dict]:
        out: list[dict] = []
        offset = 0
        for _ in range(MAX_PAGES):
            body = {
                "lang": f"{lang}_{country}",
                "deviceType": "desktop",
                "country": country,
                "pageName": "search-results",
                "ddoKey": "refineSearch",
                "sortBy": "Most recent",
                "subsearch": "",
                "from": offset,
                "jobs": True,
                "counts": False,
                "all_fetch": False,
                "pageType": "landing",
                "size": PAGE_SIZE,
                "clearAll": False,
                "jdsource": "facets",
                "isSliderEnable": False,
                "pageId": "page11",
                "siteType": "external",
                "keywords": term,
                "global": True,
                "selected_fields": {},
                "locationData": {},
                "refNum": company.slug,
            }
            resp = request_with_retry(self.client, "POST", f"{base}/widgets", json=body)
            resp.raise_for_status()
            data = (resp.json().get("refineSearch") or {}).get("data") or {}
            page = data.get("jobs") or []
            out.extend(page)
            offset += PAGE_SIZE
            if len(page) < PAGE_SIZE:
                break
        return out

    @staticmethod
    def _to_job(p: dict, base: str, country: str, lang: str, company: CompanyConfig) -> Job | None:
        ext_id = str(p.get("jobId") or p.get("reqId") or p.get("jobSeqNo") or "")
        if not ext_id:
            return None
        title = p.get("title") or ""
        key = p.get("jobSeqNo") or ext_id
        return Job(
            source="phenom",
            company=company.name,
            external_id=ext_id,
            title=title,
            location=p.get("cityStateCountry") or p.get("location"),
            department=p.get("category"),
            employment_type=p.get("type"),
            url=f"{base}/{country}/{lang}/job/{key}/{_slug(title)}",
            description=html_to_text(p.get("descriptionTeaser")),
            posted_at=_parse_date(p.get("postedDate")),
        )
