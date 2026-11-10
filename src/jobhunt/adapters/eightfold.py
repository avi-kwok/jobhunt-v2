"""Eightfold PCS adapter -- public careers-site search APIs, no auth.

Config mapping (existing CompanyConfig fields, nothing new):
    host -> the careers host, e.g. "ericsson.eightfold.ai" or "apply.careers.microsoft.com"
            (also the per-host politeness key)
    slug -> the ``domain`` query param, e.g. "ericsson.com" (``site`` is accepted as a fallback)

Eightfold sites expose one of two public endpoints (both allowed by their robots.txt):
  * ``/api/pcsx/search``    -- newer; ``{"data": {"positions": [...], "count": N}}``
  * ``/api/apply/v2/jobs``  -- legacy; ``{"positions": [...], "count": N}``
Some tenants answer 403/404/HTML on one of them, so we try pcsx first and fall back to
the legacy endpoint (remembered per host for the life of the adapter). Tenants known to refuse pcsx can pin
``eightfold_mode: legacy`` (or ``pcsx``) in companies.resolved.yaml to skip the probe. A 403 on both
means the site blocks scripted access: the error propagates, no workaround.

Both endpoints take ``query`` (fuzzy text search) and return a fixed page of 10, so we
run a union of internship searches (QUERIES), paginate each by ``start`` under MAX_PAGES,
and dedupe by position id. The list payload carries no description.
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx

from ..models import CompanyConfig, Job
from .base import Adapter, register, request_with_retry

QUERIES = ("intern", "co-op", "student")
MAX_PAGES = 10  # per query (=100 postings); the search is already narrowed server-side
PAGE_SIZE = 10  # Eightfold's fixed page size; we advance by what we actually receive

PCSX = "https://{host}/api/pcsx/search"
LEGACY = "https://{host}/api/apply/v2/jobs"


def _ts(value) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc) if value else None
    except (ValueError, TypeError, OSError, OverflowError):
        return None


@register
class EightfoldAdapter(Adapter):
    ats = "eightfold"

    def __init__(self, client: httpx.Client | None = None):
        super().__init__(client)
        self._mode: dict[str, str] = {}  # host -> "pcsx" | "legacy"

    # -- one page, either endpoint ------------------------------------------
    def _page(self, host: str, domain: str, query: str, start: int, mode: str):
        url = (PCSX if mode == "pcsx" else LEGACY).format(host=host)
        params = {"domain": domain, "query": query, "start": start}
        if mode == "legacy":
            params["num"] = PAGE_SIZE
        resp = request_with_retry(self.client, "GET", url, params=params)
        resp.raise_for_status()
        body = resp.json()  # HTML for a tenant without this endpoint -> ValueError
        if mode == "pcsx":
            body = body.get("data") or {}
        positions = body.get("positions")
        if not isinstance(positions, list):
            raise ValueError(f"{host}: unexpected {mode} payload")
        return positions, body.get("count") or 0

    def _first_page(self, host: str, domain: str, query: str, pinned: str | None = None):
        """Probe pcsx, then legacy, once per host; pin the mode that worked.

        ``pinned`` (config ``eightfold_mode``) skips the probe entirely.
        """
        if pinned in ("pcsx", "legacy"):
            self._mode[host] = pinned
        elif pinned:
            raise ValueError(f"{host}: eightfold_mode must be 'pcsx' or 'legacy', got {pinned!r}")
        if host in self._mode:
            return self._page(host, domain, query, 0, self._mode[host])
        err: Exception | None = None
        for mode in ("pcsx", "legacy"):
            try:
                out = self._page(host, domain, query, 0, mode)
            except (httpx.HTTPStatusError, ValueError) as exc:
                err = err or exc
                continue
            self._mode[host] = mode
            return out
        assert err is not None
        raise err

    def fetch(self, company: CompanyConfig) -> list[Job]:
        host = company.host
        domain = company.slug or company.site
        if not host or not domain:
            raise ValueError(f"{company.name}: eightfold adapter needs host and slug (domain)")

        seen: dict[str, Job] = {}
        for query in QUERIES:
            positions, count = self._first_page(host, domain, query, company.eightfold_mode)
            start = 0
            for page in range(MAX_PAGES):
                for p in positions:
                    job = self._to_job(p, host, company)
                    if job:
                        seen.setdefault(job.external_id, job)
                start += len(positions)
                if not positions or start >= count or page == MAX_PAGES - 1:
                    break
                positions, count = self._page(host, domain, query, start, self._mode[host])
        return list(seen.values())

    def _to_job(self, p: dict, host: str, company: CompanyConfig) -> Job | None:
        pid = p.get("id")
        if pid is None:
            return None
        url = p.get("canonicalPositionUrl") or p.get("positionUrl") or f"/careers/job/{pid}"
        if url.startswith("/"):
            url = f"https://{host}{url}"
        locations = p.get("locations") or ([p["location"]] if p.get("location") else [])
        return Job(
            source=self.ats,
            company=company.name,
            external_id=str(pid),
            title=p.get("name") or p.get("posting_name") or "",
            location="; ".join(locations) or None,
            department=p.get("department"),
            url=url,
            posted_at=_ts(p.get("postedTs") or p.get("t_create") or p.get("creationTs")),
        )
