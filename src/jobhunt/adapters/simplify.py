"""Simplify fallback adapter: SimplifyJobs' community-maintained internship listings.

Only emits rows for in-scope companies we cannot scan directly (no `ats`, no `covered_by`);
the config loader computes that allowlist onto the SimplifyJobs pseudo-company. Data source:
https://github.com/SimplifyJobs/Summer2027-Internships (thanks to the SimplifyJobs maintainers).
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from ..models import CompanyConfig, Job
from .base import Adapter, register, request_with_retry

LISTINGS_URL = (
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/"
    "dev/.github/scripts/listings.json"
)
SIMPLIFY_MAX_AGE_DAYS = 2
DEFAULT_CACHE_DIR = Path("logs")
CACHE_BODY = "simplify_cache.json"
CACHE_ETAG = "simplify_cache.etag"
FETCH_TIMEOUT = 60.0  # ~13 MB body

# Simplify company name -> our canonical config name, where the two differ beyond case and
# punctuation. Applied only when the canonical name is on the allowlist, so it never adds
# a company we already scan directly.
SIMPLIFY_NAME_ALIASES = {
    "Walmart": "Walmart Global Tech",
}


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


@register
class SimplifyAdapter(Adapter):
    ats = "simplify"

    def __init__(self, client: httpx.Client | None = None, cache_dir: Path | str | None = None):
        super().__init__(client)
        self.cache_dir = Path(cache_dir) if cache_dir else DEFAULT_CACHE_DIR

    def _load(self) -> list[dict]:
        body_path = self.cache_dir / CACHE_BODY
        etag_path = self.cache_dir / CACHE_ETAG
        headers = {}
        if body_path.exists() and etag_path.exists():
            headers["If-None-Match"] = etag_path.read_text().strip()
        resp = request_with_retry(
            self.client, "GET", LISTINGS_URL, headers=headers, timeout=FETCH_TIMEOUT
        )
        if resp.status_code == 304 and body_path.exists():
            return json.loads(body_path.read_bytes())
        resp.raise_for_status()
        rows = resp.json()
        etag = resp.headers.get("etag")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write(body_path, resp.content)
        if etag:
            _atomic_write(etag_path, etag.encode())
        else:
            etag_path.unlink(missing_ok=True)
        return rows

    def fetch(self, company: CompanyConfig) -> list[Job]:
        canon = {_norm(n): n for n in (company.simplify_companies or [])}
        if not canon:
            return []
        for simplify_name, ours in SIMPLIFY_NAME_ALIASES.items():
            if _norm(ours) in canon:
                canon.setdefault(_norm(simplify_name), canon[_norm(ours)])
        cutoff = datetime.now(timezone.utc) - timedelta(days=SIMPLIFY_MAX_AGE_DAYS)
        jobs: list[Job] = []
        for row in self._load():
            name = canon.get(_norm(row.get("company_name") or ""))
            if name is None or not row.get("active") or row.get("is_visible") is False:
                continue
            degrees = row.get("degrees")
            if isinstance(degrees, list) and degrees and not any(
                re.match(r"\s*(?:bachelor|associate)", str(d), re.IGNORECASE) for d in degrees
            ):
                continue  # grad-only (Master's / MBA / PhD)
            ts = row.get("date_posted")
            if not isinstance(ts, (int, float)):
                continue
            posted = datetime.fromtimestamp(ts, timezone.utc)
            if posted < cutoff:
                continue
            # date_posted is when Simplify listed the row, to the second. Exactly midnight
            # UTC means a date-only entry: keep it undated (freshness from first-seen).
            # Otherwise it is a real timestamp, so the normal freshness window applies.
            exact = posted if (posted.hour, posted.minute, posted.second) != (0, 0, 0) else None
            locs = row.get("locations") or []
            raw_terms = row.get("terms")
            terms = [str(t) for t in raw_terms if t] if isinstance(raw_terms, list) else None
            jobs.append(
                Job(
                    source=self.ats,
                    company=name,
                    external_id=str(row.get("id")),
                    title=row.get("title") or "",
                    location="; ".join(locs) or None,
                    url=row.get("url") or "",
                    posted_at=exact,
                    terms=terms or None,
                    posted_note=None if exact else f"Posted {posted:%b} {posted.day} via SimplifyJobs",
                )
            )
        return jobs
