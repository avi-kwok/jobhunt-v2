"""Adapter ABC, shared httpx client, HTML->text helpers, and a registry."""

from __future__ import annotations

import os
import random
import re
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from abc import ABC, abstractmethod
from html import unescape
from html.parser import HTMLParser
from typing import Callable, Type
from urllib.parse import urlsplit

import httpx

from ..models import CompanyConfig, Job

# Polite crawling convention: identify the tool and give sites a contact.
# Set JOBHUNT_CONTACT in .env to your own address — without it we send no
# contact at all, so a fork never scrapes career sites identifying as someone
# else. Keep your address out of the committed source.
_CONTACT = os.environ.get("JOBHUNT_CONTACT", "").strip()
USER_AGENT = (
    f"jobhunt/0.1 (personal internship scanner; contact {_CONTACT})"
    if _CONTACT
    else "jobhunt/0.1 (personal internship scanner)"
)
DEFAULT_TIMEOUT = 20.0
DESCRIPTION_MAX_CHARS = 2000


def make_client(timeout: float = DEFAULT_TIMEOUT) -> httpx.Client:
    """A shared-style httpx client with a descriptive UA and sane timeout."""
    return httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=timeout,
        follow_redirects=True,
    )


RETRY_AFTER_CAP = 60.0  # never sleep longer than this on a server's Retry-After


class HostStats:
    """Thread-safe per-host request counters for ONE scan (reset by begin_scan()).

    Adapters call request_with_retry from worker threads, so every update takes the lock.
    `consecutive` counts back-to-back 429/403 answers; any other answer resets it (the
    scheduler's circuit breaker reads it).
    """

    FIELDS = ("requests", "429", "403", "5xx", "retries", "retry_after_honored",
              "skipped_by_breaker")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hosts: dict[str, dict[str, int]] = {}
        self._consec: dict[str, int] = {}

    def _h(self, host: str) -> dict[str, int]:
        return self._hosts.setdefault(host, dict.fromkeys(self.FIELDS, 0))

    def add(self, host: str, field: str, n: int = 1) -> None:
        with self._lock:
            self._h(host)[field] += n

    def record_response(self, host: str, status: int) -> None:
        with self._lock:
            h = self._h(host)
            h["requests"] += 1
            if status in (429, 403):
                h[str(status)] += 1
                self._consec[host] = self._consec.get(host, 0) + 1
            else:
                self._consec[host] = 0
                if status >= 500:
                    h["5xx"] += 1

    def record_transport_error(self, host: str) -> None:
        with self._lock:
            self._h(host)["requests"] += 1

    def count(self, host: str, field: str) -> int:
        with self._lock:
            return self._hosts.get(host, {}).get(field, 0)

    def consecutive(self, host: str) -> int:
        with self._lock:
            return self._consec.get(host, 0)

    def snapshot(self) -> dict[str, dict[str, int]]:
        with self._lock:
            return {h: dict(v) for h, v in sorted(self._hosts.items())}


STATS = HostStats()


def begin_scan() -> HostStats:
    """Start a fresh per-scan stats object (module-global so Workday's inner threads share it)."""
    global STATS
    STATS = HostStats()
    return STATS


def parse_retry_after(value: str | None, now: datetime | None = None) -> float | None:
    """Retry-After as seconds: delta-seconds or an HTTP-date. None if absent/unparseable.
    Result is clamped to [0, RETRY_AFTER_CAP]."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return min(float(value), RETRY_AFTER_CAP)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    delta = (when - (now or datetime.now(timezone.utc))).total_seconds()
    return min(max(delta, 0.0), RETRY_AFTER_CAP)


def request_with_retry(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    retries: int = 3,
    backoff: float = 1.0,
    **kwargs,
) -> httpx.Response:
    """Issue a request, retrying transient failures with exponential backoff.

    On HTTP 429/503 the server's `Retry-After` header (seconds or HTTP-date, capped at
    RETRY_AFTER_CAP) is honored instead of the exponential schedule; a little random jitter
    is added to every wait so retries don't hammer in lockstep. Every attempt is counted in
    STATS under the URL's host.
    """
    host = urlsplit(url).hostname or ""
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            resp = client.request(method, url, **kwargs)
        except httpx.TransportError as exc:
            STATS.record_transport_error(host)
            last_exc = exc
            if attempt == retries - 1:
                break
            STATS.add(host, "retries")
            time.sleep(backoff * (2**attempt) + random.uniform(0, 0.5))
            continue

        STATS.record_response(host, resp.status_code)
        # retry on transient server / rate-limit statuses
        if resp.status_code in (429, 500, 502, 503, 504):
            last_exc = httpx.HTTPStatusError(
                f"transient {resp.status_code}", request=resp.request, response=resp
            )
            if attempt == retries - 1:
                break
            wait = backoff * (2**attempt)
            if resp.status_code in (429, 503):
                honored = parse_retry_after(resp.headers.get("Retry-After"))
                if honored is not None:
                    wait = honored
                    STATS.add(host, "retry_after_honored")
            STATS.add(host, "retries")
            time.sleep(wait + random.uniform(0, 0.5))
            continue

        return resp

    assert last_exc is not None
    raise last_exc


class _TextExtractor(HTMLParser):
    """Collapse HTML into readable plaintext, keeping paragraph breaks."""

    _BLOCK = {"p", "br", "li", "div", "tr", "h1", "h2", "h3", "h4", "ul", "ol"}

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self._BLOCK:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._BLOCK:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)


def html_to_text(html: str | None, max_chars: int = DESCRIPTION_MAX_CHARS) -> str | None:
    """Strip HTML to plaintext, collapse whitespace, and truncate."""
    if not html:
        return None
    parser = _TextExtractor()
    parser.feed(html)
    text = unescape(parser.text())
    # collapse runs of blank lines / spaces
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*", "\n\n", text)
    text = text.strip()
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "…"
    return text or None


class Adapter(ABC):
    """Base class for every ATS adapter."""

    # subclasses set this to their registry key (e.g. "greenhouse")
    ats: str = ""

    def __init__(self, client: httpx.Client | None = None):
        self._client = client
        self._owns_client = client is None

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = make_client()
            self._owns_client = True
        return self._client

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()

    @abstractmethod
    def fetch(self, company: CompanyConfig) -> list[Job]:
        """Return every current posting for the given company."""
        raise NotImplementedError


# ---- registry -------------------------------------------------------------

_REGISTRY: dict[str, Type[Adapter]] = {}


def register(cls: Type[Adapter]) -> Type[Adapter]:
    """Class decorator that adds an adapter to the registry under its `ats` key."""
    if not cls.ats:
        raise ValueError(f"{cls.__name__} must set a non-empty `ats` key")
    _REGISTRY[cls.ats] = cls
    return cls


def get_adapter(ats: str, client: httpx.Client | None = None) -> Adapter:
    """Instantiate the adapter registered for `ats`."""
    try:
        cls = _REGISTRY[ats]
    except KeyError as exc:
        raise KeyError(f"no adapter registered for ATS '{ats}'") from exc
    return cls(client=client)


def registered_ats() -> list[str]:
    return sorted(_REGISTRY)
