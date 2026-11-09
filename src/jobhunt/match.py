"""Per-user matching + ordering over already-classified postings (no LLM)."""

from __future__ import annotations

from .classify import Classification
from .matcher import NO_LOCATION_TIER, location_tier
from .models import Job


def matches(prefs, tags: Classification, company: str | None = None) -> bool:
    """Whether a classified posting should be delivered to a user with `prefs`.

    `prefs` is any object with levels / countries / cities / remote_ok /
    muted_companies (see db.Prefs). `company` enables the muted-company check.
    """
    if not tags.eligible:
        return False
    if tags.level not in prefs.levels:
        return False
    if company and company.strip().lower() in {c.strip().lower() for c in prefs.muted_companies}:
        return False

    wanted = {c.upper() for c in prefs.countries}
    country_ok = (
        bool(wanted & {c.upper() for c in tags.countries})
        or tags.location_opaque
        or (tags.remote and prefs.remote_ok)
    )
    if not country_ok:
        return False

    if prefs.cities:
        want_cities = {c.lower() for c in prefs.cities}
        city_ok = (
            bool(want_cities & {c.lower() for c in tags.cities})
            or tags.location_opaque
            or tags.remote
        )
        if not city_ok:
            return False
    return True


def sort_key(
    prefs,
    job: Job,
    tags: Classification,
    company_priority: int,
    location_tiers: dict[str, list[str]] | None = None,
) -> tuple[int, int, int]:
    """Favorites first, then v1 location tier, then company priority (lower = earlier)."""
    favs = {c.strip().lower() for c in prefs.favorite_companies}
    fav_rank = 0 if job.company.strip().lower() in favs else 1
    tier = location_tier(job.location, location_tiers) if location_tiers else NO_LOCATION_TIER
    return (fav_rank, tier, company_priority)
