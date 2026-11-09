"""Per-subscriber matching + ordering over already-classified postings (no LLM)."""

from __future__ import annotations

from .classify import Classification


def matches(sub, tags: Classification) -> bool:
    """Whether a classified posting goes to `sub` (anything with .countries / .levels).

    Opaque locations ("Multiple Locations") and country-less remote roles pass.
    """
    if not tags.eligible or tags.level not in sub.levels:
        return False
    wanted = {c.upper() for c in sub.countries}
    return bool(wanted & {c.upper() for c in tags.countries}) or tags.location_opaque or tags.remote


def sort_key(location_tier: int, company_priority: int) -> tuple[int, int]:
    """Best first: v1 location tier, then company priority (lower = earlier)."""
    return (location_tier, company_priority)
