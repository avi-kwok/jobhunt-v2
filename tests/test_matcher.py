"""Prefilter: internship + role + exclude + location tiering; freshness; dedupe."""

from datetime import datetime, timedelta, timezone

from jobhunt.matcher import dedupe_by_uid, is_fresh, location_tier, prefilter
from jobhunt.models import Criteria, Job

CRITERIA = Criteria(
    internship_terms=["intern", "co-op", "coop"],
    role_terms=["software engineer", "swe", "backend"],
    exclude_terms=["senior", "new grad", "manager"],
    location_match_required=True,
)
TIERS = {
    "local": ["vancouver", "toronto"],
    "us": ["seattle", "new york"],
    "canada": ["canada", "montreal"],
    "remote": ["remote"],
}


def _job(title, location=None):
    return Job(source="greenhouse", company="X", external_id="1", title=title, url="u", location=location)


def test_pass_requires_internship_and_role():
    passed, _ = prefilter(_job("Software Engineer Intern", "Vancouver, BC"), CRITERIA, TIERS)
    assert passed


def test_reject_missing_internship_term():
    passed, _ = prefilter(_job("Software Engineer", "Vancouver"), CRITERIA, TIERS)
    assert not passed


def test_reject_missing_role_term():
    passed, _ = prefilter(_job("Marketing Intern", "Vancouver"), CRITERIA, TIERS)
    assert not passed


def test_reject_exclude_term():
    passed, _ = prefilter(_job("Senior Software Engineer Intern", "Vancouver"), CRITERIA, TIERS)
    assert not passed


def test_case_insensitive():
    passed, _ = prefilter(_job("SOFTWARE ENGINEER CO-OP", "TORONTO"), CRITERIA, TIERS)
    assert passed


def test_location_tiers_in_order():
    assert location_tier("Vancouver, BC", TIERS) == 1
    assert location_tier("Seattle, WA", TIERS) == 2
    assert location_tier("Montreal, QC", TIERS) == 3
    assert location_tier("Remote - US", TIERS) == 4
    assert location_tier("Berlin, Germany", TIERS) == 5
    assert location_tier(None, TIERS) == 5


def test_remote_accepted_even_when_no_geo_tier_match_required():
    # remote is its own tier here so tier != 5; still, verify remote isn't rejected
    passed, tier = prefilter(_job("Backend Intern", "Remote"), CRITERIA, TIERS)
    assert passed and tier == 4


def test_no_location_rejected_when_required():
    passed, tier = prefilter(_job("Backend Intern", "Berlin"), CRITERIA, TIERS)
    assert not passed and tier == 5


def test_no_location_allowed_when_not_required():
    crit = CRITERIA.model_copy(update={"location_match_required": False})
    passed, tier = prefilter(_job("Backend Intern", "Berlin"), crit, TIERS)
    assert passed and tier == 5


def test_readable_foreign_city_rejected():
    # a location we CAN read but that isn't in any tier -> still rejected
    passed, tier = prefilter(_job("Backend Intern", "China, Shanghai"), CRITERIA, TIERS)
    assert not passed and tier == 5


def test_opaque_multi_location_passes_gate():
    # Workday "N Locations" rollup can't be pinned to a city -> let it through
    for loc in ("3 Locations", "Multiple Locations", "12 locations"):
        passed, tier = prefilter(_job("Backend Intern", loc), CRITERIA, TIERS)
        assert passed and tier == 5, loc


def test_missing_location_passes_gate_when_required():
    # no location at all is unresolved, not "known-bad" -> don't silently drop
    passed, tier = prefilter(_job("Backend Intern", None), CRITERIA, TIERS)
    assert passed and tier == 5


# ---- freshness ------------------------------------------------------------

NOW = datetime(2026, 7, 2, 12, 0, tzinfo=timezone.utc)


def _dated(minutes_ago, tz=timezone.utc):
    j = _job("Software Engineer Intern", "Vancouver")
    posted = NOW - timedelta(minutes=minutes_ago)
    j.posted_at = posted.replace(tzinfo=tz) if tz else posted.replace(tzinfo=None)
    return j


def test_fresh_within_window():
    assert is_fresh(_dated(5), 20, now=NOW) is True


def test_stale_outside_window():
    assert is_fresh(_dated(25), 20, now=NOW) is False


def test_boundary_is_fresh():
    assert is_fresh(_dated(20), 20, now=NOW) is True


def test_undated_source_is_fresh():
    # Workday & co. have posted_at=None -> trust the seen-store
    j = _job("Software Engineer Intern", "Toronto")
    assert j.posted_at is None
    assert is_fresh(j, 20, now=NOW) is True


def test_disabled_freshness_always_fresh():
    assert is_fresh(_dated(9999), None, now=NOW) is True
    assert is_fresh(_dated(9999), 0, now=NOW) is True


def test_naive_timestamp_treated_as_utc():
    assert is_fresh(_dated(5, tz=None), 20, now=NOW) is True
    assert is_fresh(_dated(25, tz=None), 20, now=NOW) is False


def test_future_timestamp_counts_as_fresh():
    # clock skew: posted "in the future" is not stale
    assert is_fresh(_dated(-5), 20, now=NOW) is True


# ---- in-batch dedupe ------------------------------------------------------

def test_dedupe_by_uid_keeps_first():
    a = Job(source="workday", company="X", external_id="R1", title="t", url="u1")
    a_dup = Job(source="workday", company="X", external_id="R1", title="t", url="u2")
    b = Job(source="workday", company="X", external_id="R2", title="t", url="u3")
    out = dedupe_by_uid([a, a_dup, b])
    assert [j.external_id for j in out] == ["R1", "R2"]
    assert out[0].url == "u1"  # first occurrence kept


# ---- exclude_exempt_terms (PM-adjacent roles) ------------------------------

PM_CRITERIA = Criteria(
    internship_terms=["intern", "co-op", "coop"],
    role_terms=[
        "software engineer", "swe", "backend",
        "product manager", "product marketing", "technical program", "program manager",
    ],
    exclude_terms=["senior", "new grad", "manager", "director"],
    exclude_exempt_terms=["product manager", "product marketing manager", "program manager"],
    location_match_required=False,
)


def test_pm_intern_passes_via_exempt_term():
    for title in (
        "Product Manager Intern",
        "Associate Product Manager Intern - Summer 2027",
        "Technical Program Manager Co-op",
        "Product Marketing Manager Intern",
    ):
        passed, _ = prefilter(_job(title), PM_CRITERIA, TIERS)
        assert passed, title


def test_exempt_term_does_not_shield_other_excludes():
    # "product manager" is stripped, but "senior" still rejects it.
    passed, _ = prefilter(_job("Senior Product Manager Intern"), PM_CRITERIA, TIERS)
    assert not passed
    passed, _ = prefilter(_job("Senior Product Manager"), PM_CRITERIA, TIERS)
    assert not passed


def test_engineering_manager_still_excluded():
    # "Engineering Manager ... Program" contains no exempt phrase, so "manager"
    # survives stripping and the title is rejected.
    passed, _ = prefilter(
        _job("Engineering Manager Intern Program - Software Engineer"), PM_CRITERIA, TIERS
    )
    assert not passed


def test_swe_cases_unchanged_with_exempt_terms():
    assert prefilter(_job("Software Engineer Intern"), PM_CRITERIA, TIERS)[0]
    assert not prefilter(_job("Senior Software Engineer Intern"), PM_CRITERIA, TIERS)[0]
    assert not prefilter(_job("Software Engineer, New Grad"), PM_CRITERIA, TIERS)[0]
    assert not prefilter(_job("Software Engineering Manager Intern"), PM_CRITERIA, TIERS)[0]


def test_no_exempt_terms_keeps_manager_excluded():
    # The original CRITERIA has no exempt list -> "manager" rejects PM titles.
    crit = CRITERIA.model_copy(update={"role_terms": ["product manager"]})
    passed, _ = prefilter(_job("Product Manager Intern", "Vancouver"), crit, TIERS)
    assert not passed


def test_real_config_drops_pm_and_keeps_swe():
    from pathlib import Path

    from jobhunt.config import load_config

    cfg = load_config(Path("config.yaml"), resolved_path=Path("/nonexistent.yaml"))
    crit = cfg.criteria.model_copy(update={"location_match_required": False})
    tiers = cfg.location_tiers
    import re

    assert not any(
        re.search(r"\b(product|program|tpm)\b", t) for t in crit.role_terms
    )
    assert prefilter(_job("Software Engineer Intern"), crit, tiers)[0]
    for title in (
        "Product Manager Intern",
        "Technical Program Manager Intern",
        "Senior Product Manager",
        "Engineering Manager Intern Program",
        "UX Design Intern",
        "Business Analyst Intern",
    ):
        assert not prefilter(_job(title), crit, tiers)[0], title


def test_flexible_any_site_is_unresolved():
    from jobhunt.matcher import _is_unresolved_location

    assert _is_unresolved_location("Flexible - Any SpaceX Site")
    assert _is_unresolved_location("Any SpaceX Site")
    assert not _is_unresolved_location("Flexible - London, UK")
    assert not _is_unresolved_location("Hawthorne, CA")
