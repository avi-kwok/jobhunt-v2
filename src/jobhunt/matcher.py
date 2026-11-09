"""v1 title/location prefilter + freshness/dedupe helpers (no LLM in v2)."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from .models import Criteria, Job, MatchResult

NO_LOCATION_TIER = 5
REMOTE_KEYWORDS = ("remote",)
# Opaque location rollups we can't pin to a city — Workday collapses a posting
# open in several places into "3 Locations" / "Multiple Locations". We must not
# silently drop these on the location gate (one of those places may be a good
# city); keep them.
_UNRESOLVED_LOCATION_RE = re.compile(
    r"^\s*(multiple locations|\d+\s+locations?)\s*$", re.IGNORECASE
)


def _contains_any(haystack: str, needles: list[str]) -> bool:
    return any(n.lower() in haystack for n in needles)


def location_tier(location: str | None, location_tiers: dict[str, list[str]]) -> int:
    """1-based index of the first tier whose terms appear in `location`; 5 if none.

    Tiers are evaluated in config order, so `remote` gets whatever tier the config
    assigns it (its own tier) rather than being lumped with "no location".
    """
    if not location:
        return NO_LOCATION_TIER
    loc = location.lower()
    for idx, terms in enumerate(location_tiers.values(), start=1):
        if _contains_any(loc, terms):
            return idx
    return NO_LOCATION_TIER


def _is_remote(location: str | None) -> bool:
    if not location:
        return False
    loc = location.lower()
    return any(k in loc for k in REMOTE_KEYWORDS)


def _is_unresolved_location(location: str | None) -> bool:
    """True when we can't pin the posting to a specific city.

    Covers a missing location and opaque multi-location rollups ("3 Locations").
    A *readable* location that simply isn't in our tiers (e.g. "China, Shanghai")
    is NOT unresolved — that one we can and do reject.
    """
    if not location or not location.strip():
        return True
    return bool(_UNRESOLVED_LOCATION_RE.match(location.strip()))


def dedupe_by_uid(jobs: list[Job]) -> list[Job]:
    """Drop duplicate postings within a single fetch, keeping first occurrence.

    Guards against pagination overlap (Workday) or an adapter returning the same
    posting twice, which would otherwise notify once each before the DB catches it.
    """
    seen: set[str] = set()
    unique: list[Job] = []
    for job in jobs:
        if job.uid not in seen:
            seen.add(job.uid)
            unique.append(job)
    return unique


def is_fresh(
    job: Job, freshness_minutes: float | None, now: datetime | None = None
) -> bool:
    """Whether a posting is recent enough to notify on.

    - Disabled (None/0)          -> always fresh (rely on the seen-store alone).
    - No posting timestamp        -> fresh (undated sources trust the seen-store).
    - Has a timestamp             -> fresh iff its age <= the window.

    Naive timestamps are treated as UTC. Future timestamps (clock skew) count
    as fresh.
    """
    if not freshness_minutes:
        return True
    posted = job.posted_at
    if posted is None:
        return True
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    age = now - posted
    return age <= timedelta(minutes=freshness_minutes)


def _strip_exempt(title: str, exempt_terms: list[str]) -> str:
    """Blank out exempt phrases (longest first) from a lowercased title.

    Replaced with a space rather than removed so neighbouring words can't fuse
    into a new exclude match.
    """
    for term in sorted((t.lower() for t in exempt_terms if t), key=len, reverse=True):
        title = title.replace(term, " ")
    return title


def prefilter(
    job: Job, criteria: Criteria, location_tiers: dict[str, list[str]]
) -> tuple[bool, int]:
    """Cheap, deterministic title/location gate.

    Returns (passes, location_tier). Title (lowercased) must contain >=1
    internship term AND >=1 role term AND none of the exclude terms. Before the
    exclude check, any `exclude_exempt_terms` phrase is stripped from the title
    (so "Product Manager Intern" isn't rejected by the "manager" exclude, while
    "Senior Product Manager" still is via "senior"). Location tier is computed
    regardless; if location_match_required and the tier is 5 (and it isn't
    remote), the job is rejected.
    """
    title = (job.title or "").lower()

    tier = location_tier(job.location, location_tiers)

    if not _contains_any(title, criteria.internship_terms):
        return False, tier
    if not _contains_any(title, criteria.role_terms):
        return False, tier
    exclude_title = _strip_exempt(title, criteria.exclude_exempt_terms)
    if _contains_any(exclude_title, criteria.exclude_terms):
        return False, tier

    if criteria.location_match_required and tier == NO_LOCATION_TIER:
        # Reject only readable-but-non-preferred locations; keep remote and
        # unresolved/opaque ones.
        if not _is_remote(job.location) and not _is_unresolved_location(job.location):
            return False, tier

    return True, tier


def composite_key(match: MatchResult) -> tuple[int, int, int]:
    """Batch sort key: company priority, then location tier, then higher score first."""
    return (match.company_priority, match.location_tier, -match.score)
