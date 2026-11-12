"""Per-subscriber matching + ordering over already-classified postings (no LLM)."""

from __future__ import annotations

from datetime import datetime, timezone

from .classify import Classification


_TERM_START_MONTH = {"Winter": 1, "Spring": 3, "Summer": 5, "Fall": 9}


def term_start(term: str) -> tuple[int, int] | None:
    """(year, month) a canonical "Season YYYY" term starts, or None if malformed."""
    season, _, year = term.partition(" ")
    if season not in _TERM_START_MONTH or not (year.isdigit() and len(year) == 4):
        return None
    return int(year), _TERM_START_MONTH[season]


def parse_grad_date(value: str | None) -> tuple[int, int] | None:
    """(year, month) from "YYYY-MM"; None for NULL or anything malformed."""
    if not isinstance(value, str) or len(value) != 7 or value[4] != "-":
        return None
    y, m = value[:4], value[5:]
    if not (y.isdigit() and m.isdigit()) or not 1 <= int(m) <= 12:
        return None
    return int(y), int(m)


def passes_grad_filter(grad_date: str | None, terms: list[str]) -> bool:
    """True if no filter, no stated term, or ANY term starts strictly before the grad month."""
    grad = parse_grad_date(grad_date)
    if grad is None or not terms:
        return True
    starts = [t for t in map(term_start, terms) if t is not None]
    if not starts:  # only unparseable terms: treat as no stated term
        return True
    return any(t < grad for t in starts)


def matches(sub, tags: Classification) -> bool:
    """Whether a classified posting goes to `sub` (anything with .countries / .levels, optional .grad_date).

    Opaque locations ("Multiple Locations") and country-less remote roles pass.
    """
    if not tags.eligible or tags.level not in sub.levels:
        return False
    wanted = {c.upper() for c in sub.countries}
    if not (bool(wanted & {c.upper() for c in tags.countries}) or tags.location_opaque or tags.remote):
        return False
    return passes_grad_filter(getattr(sub, "grad_date", None), tags.terms)


def sort_key(posted_at: datetime | None) -> tuple[int, float]:
    """Newest first; postings without a timestamp go last. Naive times count as UTC."""
    if posted_at is None:
        return (1, 0.0)
    if posted_at.tzinfo is None:
        posted_at = posted_at.replace(tzinfo=timezone.utc)
    return (0, -posted_at.timestamp())
