"""Deterministic classifier: whole-word terms, levels, exclusions, years rule, location."""

from pathlib import Path

import pytest

from jobhunt.classify import RULES_VERSION, classify, years_required
from jobhunt.config import load_config
from jobhunt.models import Job

CFG = load_config(Path("config.yaml"), resolved_path=Path("/nonexistent.yaml"))


def cls(title, location=None, description=None, company="X"):
    job = Job(
        source="x", company=company, external_id="1", title=title, url="u",
        location=location, description=description,
    )
    return classify(job, CFG.criteria, CFG.location_tiers)


def test_rules_version():
    assert RULES_VERSION == 3
    assert cls("Software Engineer Intern").rules_version == 3


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
        "Production Engineer Intern (Cloud Infrastructure)",
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


# ---- RULES_VERSION 2: SWE-only precision + recall fixes ----------------------

@pytest.mark.parametrize("title", [
    # ambiguous discipline names with a hardware-discipline veto word (any company)
    "Radar Systems Engineer - Intern",
    "Acoustic Systems Engineering Co-op",
    "Flight Controls Systems Engineer Intern",
    "Industrial Automation Engineer Intern",
    "Facilities Controls and Automation Engineering Intern",
    "Display Systems Engineering Co-Op (Summer/Fall 2027) - Onsite",
    "Mississauga Intern, Systems Engineering Hydro Mechanics (Winter 2027)",
    "Automation Engineer Intern - Electrical",
    "Systems Engineer (Specialty) - Intern",
    "Data Center Infrastructure Engineer Internship 2027",
    "Intern, Model Based Systems Engineering",
    "Vehicle Systems Engineer Intern",
    "Embedded Hardware Engineer Intern",
    # non-software "developer"
    "Model Developer Intern",
    "Business Developer Intern",
    "Curriculum Developer Intern",
    "Content Developer Co-op",
    "Product Developer Intern",
    "Real Estate Developer Intern",
    "CNC Programmer Co-op - Spring 2027",
    "PLC Programming Intern",
    # bare French "stage" outside intern positions
    "Launch Engineer, Stage 0 Propellant Generation",
    "Propulsion Engineer, Stage Systems",
    "Oncology Late Stage Clinical Development Scientist",
    "HR Stage",
    "PhD Software Engineer Intern",
])
def test_v2_rejected(title):
    c = cls(title, "Denver, CO")
    assert not c.eligible, (title, c.categories, c.level)


@pytest.mark.parametrize("title", [
    "IT Systems Engineer Intern",
    "IT Platform Engineer Intern",
    "Software Systems Engineer Co-op",
    "Cloud Infrastructure Engineer Intern",
    "Test Automation Engineer Intern",
    "Software Automation Co-op",
    "Front End Engineer Intern",
    "Frontend Developer Intern",
    "Embedded Engineer Intern",
    "Software Intern",
    "Software Internship - Summer 2027",
    "Software QA Intern",
    "Software Test Co-op",
    "Software Tools Intern",
    "Quantitative Developer Intern",
    "AI Developer Co-op",
    "AI Engineer Intern",
    "ML Engineer Intern",
    "Machine Learning Intern",
    "Developer Intern",
    "Programmer Intern",
    "Cybersecurity Intern",
    "Cyber Engineer Co-op",
    "Software Engineering Co-ops (Fall 2027)",
    "2027 Winter Co-Ops - Software Development",
    "Software Engineer Intern - BS/MS/PhD",
    "Software Engineering Intern (MS/PhD)",
    "Stagiaire, Développement logiciel (Hiver 2027)",
    "Stagiaire Programmation de Moteur - Beenox (Hiver 2027)",
    "Stage - Développeur Web",
    "Stage en développement logiciel",
    "Werkstudent Software Engineering",
])
def test_v2_eligible(title):
    c = cls(title, "Toronto, ON")
    assert c.eligible, (title, c.reject_reason)
    assert c.categories == ["swe"]


# ---- ambiguous role terms: veto word / software anchor / hardware-heavy company ----

@pytest.mark.parametrize("company,title", [
    ("RTX", "CH-53K Systems Engineering Co-Op - Summer/Fall 2027 - Onsite"),
    ("RTX", "Systems Engineering Intern (Summer 2027)"),
    ("Lockheed Martin", "Radar Systems Engineer - Intern"),
    ("Lockheed Martin", "Systems Engineer - Intern"),
    ("Northrop Grumman", "2027 Systems Engineering Intern - Colorado Springs CO"),
    ("Bose", "Acoustic Systems Engineering Co-op"),
    ("Garmin", "Flight Controls Systems Engineer Intern"),
    ("Garmin", "Aviation Systems Engineer Intern"),
    ("Texas Instruments", "Facilities Controls and Automation Engineering Intern"),
    ("Texas Instruments", "Systems Engineering Intern"),
    ("Accenture", "Industrial Automation Engineer Intern"),
    ("Analog Devices", "System Integration Engineer Intern"),
    ("Anduril", "2027 Systems Engineer Intern"),
    ("SpaceX", "Integration Engineer Intern (Super Heavy Booster)"),
    ("SpaceX", "Build Engineer Intern (Starship)"),
    ("SpaceX", "Production Engineer Intern (Starship Electronics)"),
    ("Micron", "Intern - Front-End Materials Procurement Operations"),
    ("Lockheed Martin", "Platform Engineer Intern"),  # same title is in at DRW
    ("lockheed martin", "Platform Engineer Intern"),  # company match is case-insensitive
])
def test_ambiguous_rejected_at_hardware_heavy_or_vetoed(company, title):
    c = cls(title, "Denver, CO", company=company)
    assert not c.eligible and c.reject_reason == "no swe-category term in title", (company, title)


@pytest.mark.parametrize("company,title", [
    ("DRW", "Platform Engineer Intern"),
    ("Schonfeld", "2027 Platform Engineering Intern"),
    ("Palantir", "Forward Deployed Infrastructure Engineer, Internship - US Government"),
    ("Later", "AI Automation Engineer Co-op"),
    ("Akuna Capital", "Platform Engineer Intern, Summer 2027"),
    ("Jump Trading", "Campus Systems Engineer (Intern)"),
    ("X", "Platform Engineering Intern"),
    # a software anchor counts even at a hardware-heavy employer
    ("Lockheed Martin", "Systems Engineering/IT-Intern"),
    ("RTX", "Software Systems Engineer Co-op"),
    ("Marvell", "AI-Native Development Platform Engineer Intern, MS - Summer 2027"),
    ("General Motors", "Vehicle Software Integration Engineer Intern"),  # software lifts the vehicle veto
])
def test_ambiguous_accepted_at_software_firm_or_with_anchor(company, title):
    c = cls(title, "Denver, CO", company=company)
    assert c.eligible and c.categories == ["swe"], (company, title, c.reject_reason)


def test_ambiguous_veto_word_beats_software_firm_and_anchor():
    # the veto applies at software firms too, and even when a software anchor is present
    assert not cls("Radar Systems Engineer - Intern", company="DRW").eligible
    assert not cls("Hardware Platform Engineer Intern, AI", company="DRW").eligible
    # strong role terms are never vetoed
    assert cls("Hardware Software Engineer Intern", company="RTX").eligible


def test_hardware_heavy_companies_configured():
    hw = set(CFG.criteria.hardware_heavy_companies)
    watch = {c.name for c in CFG.companies}
    assert {"RTX", "Lockheed Martin", "Bose", "Garmin", "Texas Instruments", "Accenture"} <= hw
    assert hw <= watch, hw - watch  # exact watchlist names
    assert not {"DRW", "Schonfeld", "Palantir", "Later"} & hw


def test_coop_plural_level():
    assert cls("2027 Winter Co-Ops - Software Development - Demonware").level == "coop"


def test_stage_level_only_in_intern_positions():
    assert cls("Stage - Développeur").level == "internship"
    assert cls("Stage de développement logiciel").level == "internship"
    assert cls("Propulsion Engineer, Stage Systems").level is None


@pytest.mark.parametrize("loc", [
    "Littleton, CO", "Highlands Ranch, CO; Louisville, CO", "Longmont, CO 80501",
    "Hillsboro,OR", "Camden, AR", "USA - Dover, DE", "Indianapolis, IN 46204",
    "Honolulu, HI", "Portland, ME", "Tulsa, OK",
])
def test_omitted_state_codes_as_city_state_segment(loc):
    c = cls("Software Engineer Intern - Summer 2027", loc)
    assert c.countries == ["US"] and c.eligible, loc


@pytest.mark.parametrize("loc", ["Bogotá, Colombia", "Pune, India", "Medellin, CO, Colombia"])
def test_state_code_rule_does_not_claim_foreign(loc):
    c = cls("Software Engineer Intern", loc)
    assert "US" not in c.countries and not c.eligible, loc


@pytest.mark.parametrize("loc", ["Flexible - Any SpaceX Site", "Any SpaceX Site", "Flexible"])
def test_spacex_flexible_location_is_opaque(loc):
    c = cls("Summer 2027 Software Engineering Internship/Co-op", loc)
    assert c.location_opaque and c.eligible


def test_state_code_ignored_inside_foreign_segment():
    c = cls("Software Tools Engineering Intern", "United Kingdom, Edinburgh, SC, Freer")
    assert c.countries == [] and not c.eligible
    # a US segment next to a foreign one still counts
    assert cls("Software Engineer Intern", "Austin, TX; Bangalore, India").countries == ["US"]


# ---- city gazetteer ("CA" = Canada or California) ----------------------------


@pytest.mark.parametrize("loc,countries", [
    ("Fremont, CA", ["US"]),
    ("Fremont, CA 94538", ["US"]),
    ("Palo Alto, CA", ["US"]),
    ("Toronto, ON", ["CA"]),
    ("Vancouver, CA", ["CA"]),          # Vancouver exists in BC and WA, not California
    ("Remote - CA", ["CA"]),            # no city: unchanged fallback
    ("Waterloo, ON; Fremont, CA", ["CA", "US"]),
    ("Boulder, CO", ["US"]),
    ("Amsterdam, NL", []),              # Netherlands, not Newfoundland
])
def test_gazetteer_country(loc, countries):
    assert cls("Software Engineer Intern", loc).countries == countries


# ---- graduate-degree titles ---------------------------------------------------


@pytest.mark.parametrize("title", [
    "Software Engineer Intern - Master's", "Software Engineer Intern (Masters)",
    "Software Engineering Graduate Student Intern", "Software Engineer Intern, MBA Candidate",
])
def test_grad_titles_excluded(title):
    c = cls(title, "Toronto, ON")
    assert not c.eligible and c.reject_reason.startswith("excluded title term")


@pytest.mark.parametrize("title", [
    "Software Engineer Intern (BS/MS)", "Software Engineer Intern, Bachelor's/Master's",
    "Software Engineer Intern - Bachelors/Masters", "Software Engineer Intern (Bachelor's or Master's)",
])
def test_mixed_undergrad_grad_titles_survive(title):
    assert cls(title, "Toronto, ON").eligible


# ---- term tags ----------------------------------------------------------------


@pytest.mark.parametrize("text,terms", [
    ("Software Engineer Intern, Summer 2027", ["Summer 2027"]),
    ("SWE Intern (Winter/Spring 2027)", ["Winter 2027", "Spring 2027"]),
    ("Fall 2026 Co-op", ["Fall 2026"]),
    ("2027 Summer Software Intern", ["Summer 2027"]),
    ("Autumn 2026 Intern", ["Fall 2026"]),
    ("Summer 2027 or Fall 2027", ["Summer 2027", "Fall 2027"]),
    ("Software Engineer Intern", []),
])
def test_parse_terms(text, terms):
    from jobhunt.classify import parse_terms

    assert parse_terms(text) == terms


def test_terms_merge_title_and_job_terms():
    job = Job(source="x", company="X", external_id="1", title="SWE Intern Summer 2027", url="u",
              location="Toronto, ON", terms=["Summer 2027", "Fall 2027"])
    assert classify(job, CFG.criteria, CFG.location_tiers).terms == ["Summer 2027", "Fall 2027"]


def test_curly_apostrophe_masters_excluded():
    """Titles with a typographic apostrophe ("Master’s") hit the same exclude as "master's"."""
    from jobhunt.config import load_config
    from jobhunt.models import Job

    cfg = load_config()
    for title in ("Software Engineer Intern (Master’s)", "Software Engineer Intern (Master's)"):
        job = Job(source="t", company="Acme", external_id="1", title=title,
                  url="https://x.example", location="Toronto, ON")
        c = classify(job, cfg.criteria, cfg.location_tiers)
        assert not c.eligible and "master" in (c.reject_reason or "")
