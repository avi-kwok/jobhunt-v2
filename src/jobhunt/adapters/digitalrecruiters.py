"""Cegid DigitalRecruiters adapter — the public careers-site JSON API, no auth.

The careers site (a Nuxt app on the company's own domain, e.g. jobs.momentfactory.com)
calls ``POST https://api.digitalrecruiters.com/public/v1/careers-site/job-ads`` with
``domainName``/``limit``/``page``/``locale`` query params and a JSON search body.

Config:
- ``slug`` = the careers-site domain (the API's ``domainName``), e.g. ``jobs.momentfactory.com``.
- ``site`` = the careers-site path locale as it appears in posting URLs (``en``, ``fr_CA``,
  ``fr`` ...; default ``en``). It is mapped to the API locale the same way the site's own
  bundle does (``en`` -> ``en_GB``, ``fr`` -> ``fr_FR``, ``fr_CA`` -> ``fr_CA``); a locale
  the board doesn't publish answers HTTP 400.
- ``host`` = ``api.digitalrecruiters.com`` (every tenant's requests go to that shared host,
  so it is the politeness / breaker unit).

Strategy: page 1 of the unfiltered list carries ``count``. A board that fits in
MAX_LIST_PAGES pages is listed in full (cheapest and complete; the server-side search is
fuzzy full-text, so for small boards it saves nothing). A bigger board is searched
server-side (body ``{"q": term}``) for intern/co-op/student terms in English and French,
and the results are unioned with page 1 and deduped. The list payload has no description
or date, so only intern-like titles get a detail call (capped at MAX_DETAILS) for the
description and ``republished_at``. A response without an ``items`` list (redesign,
challenge page) raises instead of silently returning no jobs.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import httpx

from ..models import CompanyConfig, Job
from .base import Adapter, html_to_text, register, request_with_retry

API = "https://api.digitalrecruiters.com/public/v1/careers-site/job-ads"
LIMIT = 50
MAX_LIST_PAGES = 4  # boards with <= LIMIT * MAX_LIST_PAGES ads are listed in full
SEARCH_TERMS = ("intern", "co-op", "student", "stage", "stagiaire", "alternance")
SEARCH_PAGES = 2  # page cap per search term
MAX_DETAILS = 10  # detail calls per scan (intern-like titles only)

# The careers-site bundle's path-locale -> API-locale map (function tE / object eE).
_LOCALES = {
    "cs": "cs_CZ", "da": "da_DK", "de": "de_DE", "en": "en_GB", "es": "es_ES",
    "it": "it_IT", "ja": "ja_JP", "ko": "ko_KR", "fr": "fr_FR", "nl": "nl_BE",
    "pl": "pl_PL", "pt": "pt_PT", "ro": "ro_RO", "sv": "sv_SE", "tr": "tr_TR",
}

_INTERN_RE = re.compile(
    r"\b(intern|interns|internship|co-?op|student|stage|stagiaire|[ée]tudiant\w*"
    r"|alternan\w*|apprenti\w*|trainee)\b",
    re.IGNORECASE,
)


def api_locale(site: str) -> str:
    s = site.replace("-", "_")
    return s if "_" in s else _LOCALES.get(s, s)


def _parse_date(value) -> datetime | None:
    try:
        d = datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None
    return d.replace(tzinfo=timezone.utc)


@register
class DigitalRecruitersAdapter(Adapter):
    ats = "digitalrecruiters"

    def _page(self, company: CompanyConfig, locale: str, page: int, body: dict) -> dict:
        params = {"domainName": company.slug, "limit": LIMIT, "page": page, "locale": locale}
        resp = request_with_retry(self.client, "POST", API, params=params, json=body)
        resp.raise_for_status()
        try:
            data = resp.json()
        except ValueError:
            data = None
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise ValueError(f"{company.name}: digitalrecruiters response has no 'items' list")
        return data

    def _collect(self, company, locale, body, max_pages, out: dict, first: dict | None = None):
        for page in range(1, max_pages + 1):
            data = first if (page == 1 and first is not None) else self._page(
                company, locale, page, body)
            items = data["items"]
            for it in items:
                if it.get("id") is not None:
                    out.setdefault(str(it["id"]), it)
            if len(items) < LIMIT:
                return

    def _detail(self, company: CompanyConfig, locale: str, item: dict) -> dict | None:
        ad_id = item.get("job_ad_id")
        if not ad_id:
            return None
        params = {"domainName": item.get("career_domain") or company.slug, "locale": locale,
                  "external": int(bool(item.get("is_external")))}
        tail = str(item.get("id", "")).partition("-")[2]
        if tail:
            params["addressId"] = tail
        resp = request_with_retry(self.client, "GET", f"{API}/{ad_id}", params=params)
        if resp.status_code != 200:
            return None  # ad pulled between list and detail; keep the list data
        try:
            d = resp.json()
        except ValueError:
            return None
        return d if isinstance(d, dict) else None

    def fetch(self, company: CompanyConfig) -> list[Job]:
        if not company.slug:
            raise ValueError(f"{company.name}: digitalrecruiters adapter needs slug (domainName)")
        site = company.site or "en"
        locale = api_locale(site)

        items: dict[str, dict] = {}
        first = self._page(company, locale, 1, {})
        if int(first.get("count") or 0) <= LIMIT * MAX_LIST_PAGES:
            self._collect(company, locale, {}, MAX_LIST_PAGES, items, first)
        else:
            self._collect(company, locale, {}, 1, items, first)
            for term in SEARCH_TERMS:
                self._collect(company, locale, {"q": term}, SEARCH_PAGES, items)

        jobs: list[Job] = []
        details = 0
        for jid, it in items.items():
            title = (it.get("title") or "").strip()
            domain = it.get("career_domain") or company.slug
            url = it.get("careers_site_url") or f"https://{domain}/{site}/annonce/{it.get('url', '')}"
            description = posted_at = None
            location = it.get("location") or None
            if _INTERN_RE.search(title) and details < MAX_DETAILS:
                details += 1
                try:
                    d = self._detail(company, locale, it)
                except httpx.HTTPError:
                    # retries exhausted (429/5xx/transport): keep list data and stop
                    # detail calls for this scan rather than fail the whole company
                    d, details = None, MAX_DETAILS
                if d:
                    description = html_to_text(
                        "\n".join(d.get(k) or "" for k in ("description", "profile")))
                    posted_at = _parse_date(d.get("republished_at"))
                    location = d.get("formatted_address") or location
            jobs.append(
                Job(
                    source=self.ats,
                    company=company.name,
                    external_id=jid,
                    title=title,
                    location=location,
                    department=it.get("job") or None,
                    employment_type=it.get("contract") or None,
                    url=url,
                    description=description,
                    posted_at=posted_at,
                )
            )
        return jobs
