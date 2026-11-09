"""Deterministic classifier: whole-word terms, levels, exclusions, years rule, location."""

from pathlib import Path

import pytest

from jobhunt.classify import RULES_VERSION, classify, years_required
from jobhunt.config import load_config
from jobhunt.models import Job

CFG = load_config(Path("config.yaml"), resolved_path=Path("/nonexistent.yaml"))


def cls(title, location=None, description=None):
    job = Job(
        source="x", company="X", external_id="1", title=title, url="u",
        location=location, description=description,
    )
    return classify(job, CFG.criteria, CFG.location_tiers)


def test_rules_version():
    assert RULES_VERSION == 1
    assert cls("Software Engineer Intern").rules_version == 1


@pytest.mark.parametrize(
    "title",
    [
        "Software Developer - Internal Compute Frameworks (Python)",
        "Software Engineer, Cooperative AI",
        "Software Engineer, Internal Applications - Enterprise",
        "Software Engineer, International Payments",
        "Payments Product intern",  # PM is out of scope
        "Product Manager Intern",
        "Technical Program Manager Co-op",
        "Senior Software Engineer Intern",
        "Sr. Software Engineer Intern",
        "Staff Software Engineer, Intern Program",
        "Software Engineering Manager Intern",
        "Lead Software Engineer Intern",
        "Head of Engineering Intern",
        "Software Engineer",  # no intern term
        "Marketing Intern",
    ],
)
def test_rejected_titles(title):
    c = cls(title)
    assert not c.eligible
    assert c.reject_reason


@pytest.mark.parametrize(
    "title",
    [
        "Data Engineer Intern (Summer 2027)",
        "Wavelogic Embedded Software Developer Intern (Winter 2027)",
        "Student, Full Stack Developer",
        "Software Engineering Intern – Summer 2026",
        "Software Engineer Interns",
        "iOS Intern",
        "Machine Learning Engineer Intern",
        "Security Engineer Co-op",
        "Production Engineer Intern",
        "Firmware Intern",
        "Android Developer Internship",
    ],
)
def test_eligible_titles(title):
    c = cls(title)
    assert c.eligible, c.reject_reason
    assert c.categories == ["swe"]


def test_levels():
    assert cls("Co-op, Software Developer (Fall 2026)").level == "coop"
    assert cls("Software Developer Coop").level == "coop"
    assert cls("Software Developer Co op").level == "coop"
    assert cls("Software Developer Intern").level == "internship"
    assert cls("Software Developer Co-op / Intern").level == "coop"
    assert cls("Student, Full Stack Developer").level == "internship"


def test_swe_alone_without_level_is_not_eligible():
    c = cls("Software Engineer, Platform")
    assert c.level is None and c.categories == ["swe"] and not c.eligible


def test_exclude_terms_from_config():
    for t in ("Software Engineer Intern, New Grad", "PhD Software Engineer Intern",
              "Software Engineer Intern (Full-Time)"):
        assert not cls(t).eligible, t


# ---- years-of-experience rule ----------------------------------------------

@pytest.mark.parametrize(
    "desc,n",
    [
        ("We need 6+ years of experience building services.", 6),
        ("Requires 2-5 years of experience with Python.", 2),
        ("Minimum 3 years in a backend role", 3),
        ("At least 2 years of professional experience required.", 2),
        ("5 years of industry experience", 5),
        ("2 to 4 years working with distributed systems", 2),
    ],
)
def test_years_rule_positive(desc, n):
    assert years_required(desc) == n
    c = cls("Software Engineer Intern", description=desc)
    assert not c.eligible and "years" in c.reject_reason


@pytest.mark.parametrize(
    "desc",
    [
        "Pursuing a 4-year degree in Computer Science.",
        "Currently enrolled in a 4 year degree program.",
        "Graduating in 2 years or less.",
        "1+ years of coursework in algorithms",
        "0-1 years of experience",
        "3 years of coursework in CS",
        "We have been around for 20 years ago",
        "Up to 3 years of experience is fine",
        None,
        "",
    ],
)
def test_years_rule_negative(desc):
    assert years_required(desc) is None
    assert cls("Software Engineer Intern", description=desc).eligible


# ---- location ---------------------------------------------------------------

def test_location_canada_us():
    c = cls("Software Engineer Intern", "Toronto, ON")
    assert c.countries == ["CA"] and "toronto" in c.cities and c.eligible
    c = cls("Software Engineer Intern", "Seattle, WA")
    assert c.countries == ["US"] and c.eligible
    assert cls("Software Engineer Intern", "Vancouver, BC, CA").countries == ["CA"]
    assert cls("Software Engineer Intern", "San Jose, CA").countries == ["US"]
    assert cls("Software Engineer Intern", "Austin, Texas, United States").countries == ["US"]
    assert cls("Software Engineer Intern", "Halifax, Nova Scotia").countries == ["CA"]
    c = cls("Software Engineer Intern", "Toronto / New York")
    assert c.countries == ["CA", "US"]


def test_location_opaque():
    for loc in (None, "", "  ", "Multiple Locations", "3 Locations", "12 locations"):
        c = cls("Software Engineer Intern", loc)
        assert c.location_opaque and c.eligible, loc


def test_location_remote():
    c = cls("Software Engineer Intern", "Remote")
    assert c.remote and c.eligible and not c.location_opaque
    c = cls("Software Engineer Intern", "Remote - Canada")
    assert c.remote and c.countries == ["CA"]
    assert not cls("Software Engineer Intern", "Remote - Germany").eligible


def test_location_foreign():
    for loc in ("Berlin, Germany", "London, UK", "Shanghai", "Bangalore, India"):
        c = cls("Software Engineer Intern", loc)
        assert not c.eligible and c.reject_reason == "outside CA/US", loc
        assert not c.location_opaque


@pytest.mark.parametrize("title", [
    "Mobile Marketing Intern", "Mobile Sales Intern", "Mobile Operations Co-op",
])
def test_bare_mobile_is_not_swe(title):
    assert not cls(title).eligible


@pytest.mark.parametrize("title", [
    "Mobile Developer Intern", "Mobile Engineer Co-op", "Mobile Software Intern",
    "Mobile App Developer Intern",
])
def test_mobile_swe_phrases_eligible(title):
    assert cls(title, "Toronto, ON").eligible
