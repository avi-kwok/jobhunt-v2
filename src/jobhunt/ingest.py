"""Validation + identity for externally pushed postings (POST /ingest/posting).

Pure functions, no I/O. Classification is NOT done here: the worker tags ingested postings
(pipeline.classify_untagged) so rules/config live in one place. See docs/architecture.md.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
import re
from datetime import date, datetime, timezone
from urllib.parse import urlsplit

SOURCE = "muse-pipeline"
MAX_BODY = 4096
CAPS = {"title": 300, "company": 200, "location": 200, "apply_url": 2000}
LEVELS = ("intern", "coop")
_DATE_ONLY = re.compile(r"\d{4}-\d{2}-\d{2}")
COUNTRY_NAMES = {"US": "United States", "CA": "Canada"}


class IngestError(ValueError):
    """Validation failure; str() is the client-facing '<field>: <reason>'."""

    def __init__(self, field: str, reason: str):
        super().__init__(f"{field}: {reason}")


@dataclass
class Posting:
    title: str
    company: str
    url: str
    location: str | None
    posted_at: datetime | None
    posted_note: str | None
    uid: str
    url_variants: list[str]


def _bad_chars(s: str) -> bool:
    return any(ord(c) < 32 or ord(c) == 127 or c.isspace() for c in s)


def _text(data: dict, field: str, *, required: bool) -> str | None:
    v = data.get(field)
    if v is None or v == "":
        if required:
            raise IngestError(field, "required")
        return None
    if not isinstance(v, str):
        raise IngestError(field, "must be a string")
    v = " ".join(v.split())  # collapse whitespace/newlines (shown in a Discord embed)
    if not v:
        raise IngestError(field, "required" if required else "empty")
    if len(v) > CAPS[field]:
        raise IngestError(field, f"longer than {CAPS[field]} characters")
    if any(ord(c) < 32 or ord(c) == 127 for c in v):
        raise IngestError(field, "control characters")
    return v


def _split_url(url: str):
    """Return (parts, host_port) for a valid https URL or raise IngestError."""
    if not isinstance(url, str) or not url:
        raise IngestError("apply_url", "required")
    if len(url) > CAPS["apply_url"]:
        raise IngestError("apply_url", f"longer than {CAPS['apply_url']} characters")
    if _bad_chars(url):
        raise IngestError("apply_url", "whitespace or control characters")
    try:
        p = urlsplit(url)
        port = p.port
    except ValueError:
        raise IngestError("apply_url", "not a valid URL") from None
    if p.scheme.lower() != "https":
        raise IngestError("apply_url", "must be https")
    if not p.hostname:
        raise IngestError("apply_url", "missing host")
    if "@" in p.netloc:
        raise IngestError("apply_url", "credentials not allowed")
    host = p.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    return p, host if port in (None, 443) else f"{host}:{port}"


def normalize_url(url: str) -> str:
    """https://host/path?query: lowercase host, default port and fragment dropped, path's trailing / stripped."""
    p, hostport = _split_url(url)
    return f"https://{hostport}{p.path.rstrip('/')}" + (f"?{p.query}" if p.query else "")


def url_variants(url: str) -> list[str]:
    """Forms a scanner may have stored for the same page (query kept): the original plus {http,https} x {/,no /}."""
    p, hostport = _split_url(url)
    q = f"?{p.query}" if p.query else ""
    path = p.path.rstrip("/")
    out = [url]
    for scheme in ("https", "http"):
        for slash in ("", "/"):
            out.append(f"{scheme}://{hostport}{path}{slash}{q}")
    return list(dict.fromkeys(out))


def _posted_at(v) -> tuple[datetime | None, str | None]:
    """(posted_at, posted_note). A bare date has no time, so it is kept as a note only and the
    posting counts as undated (fresh), like other undated sources."""
    if v is None or v == "":
        return None, None
    if not isinstance(v, str):
        raise IngestError("posted_at", "must be an ISO date or datetime")
    v = v.strip()
    try:
        if _DATE_ONLY.fullmatch(v):
            return None, f"posted {date.fromisoformat(v).isoformat()}"
        dt = datetime.fromisoformat(v)
    except ValueError:
        raise IngestError("posted_at", "must be an ISO date or datetime") from None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)), None


def parse(raw: bytes) -> Posting:
    """Validate a request body. `country`/`level` are checked but not trusted: rules derive both."""
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        raise IngestError("body", "must be a JSON object") from None
    if not isinstance(data, dict):
        raise IngestError("body", "must be a JSON object")
    title = _text(data, "title", required=True)
    company = _text(data, "company", required=True)
    location = _text(data, "location", required=False)
    url = data.get("apply_url")
    normalized = normalize_url(url)
    country = data.get("country")
    if not (isinstance(country, str) and len(country) == 2 and country.isascii() and country.isalpha()):
        raise IngestError("country", "must be a 2-letter code")
    if data.get("level") not in LEVELS:
        raise IngestError("level", "must be one of: intern, coop")
    if location is None:
        location = COUNTRY_NAMES.get(country.upper())
    company = company.replace(":", " -")  # uid is source:company:id and is split on ':'
    posted_at, posted_note = _posted_at(data.get("posted_at"))
    digest = hashlib.sha256(normalized.encode()).hexdigest()[:32]
    return Posting(
        title=title, company=company, url=url, location=location,
        posted_at=posted_at, posted_note=posted_note,
        uid=f"{SOURCE}:{company}:{digest}", url_variants=url_variants(url),
    )
