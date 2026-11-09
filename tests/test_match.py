"""Per-user matching + ordering."""

from jobhunt.classify import Classification
from jobhunt.db import Prefs
from jobhunt.match import matches, sort_key
from jobhunt.models import Job

TIERS = {"local": ["vancouver", "toronto"], "us": ["seattle"], "canada": ["canada"], "remote": ["remote"]}


def tags(**kw):
    base = dict(eligible=True, level="internship", categories=["swe"], countries=["CA"],
                cities=["toronto"], remote=False, location_opaque=False)
    base.update(kw)
    return Classification(**base)


def prefs(**kw):
    return Prefs(**{**dict(levels=["internship", "coop"], countries=["CA", "US"], cities=[],
                           remote_ok=True), **kw})


def test_basic_match():
    assert matches(prefs(), tags())


def test_ineligible_never_matches():
    assert not matches(prefs(), tags(eligible=False))


def test_level_filter():
    assert not matches(prefs(levels=["coop"]), tags(level="internship"))
    assert matches(prefs(levels=["coop"]), tags(level="coop"))


def test_country_filter_and_opaque_and_remote():
    assert not matches(prefs(countries=["US"]), tags(countries=["CA"]))
    assert matches(prefs(countries=["US"]), tags(countries=[], location_opaque=True))
    assert matches(prefs(countries=["US"]), tags(countries=[], remote=True))
    assert not matches(prefs(countries=["US"], remote_ok=False), tags(countries=[], remote=True))


def test_city_filter():
    p = prefs(cities=["vancouver"])
    assert not matches(p, tags(cities=["toronto"]))
    assert matches(p, tags(cities=["vancouver", "toronto"]))
    assert matches(p, tags(cities=[], location_opaque=True))
    assert matches(p, tags(cities=[], remote=True, countries=[]))


def test_muted_company():
    p = prefs(muted_companies=["Acme"])
    assert not matches(p, tags(), company="acme")
    assert matches(p, tags(), company="Other")


def _job(company, loc):
    return Job(source="x", company=company, external_id="1", title="t", url="u", location=loc)


def test_sort_key_favorites_then_tier_then_priority():
    p = prefs(favorite_companies=["Fav"])
    t = tags()
    items = [
        (_job("Other", "Toronto"), 1),
        (_job("Fav", "Seattle"), 3),
        (_job("Other", "Seattle"), 1),
        (_job("Other", "Toronto"), 2),
    ]
    ordered = sorted(items, key=lambda x: sort_key(p, x[0], t, x[1], TIERS))
    assert [(j.company, j.location, pr) for j, pr in ordered] == [
        ("Fav", "Seattle", 3),
        ("Other", "Toronto", 1),
        ("Other", "Toronto", 2),
        ("Other", "Seattle", 1),
    ]
