"""Per-subscriber matching + ordering."""

from types import SimpleNamespace

from jobhunt.classify import Classification
from jobhunt.match import matches, sort_key


def tags(**kw):
    base = dict(eligible=True, level="internship", categories=["swe"], countries=["CA"],
                cities=["toronto"], remote=False, location_opaque=False)
    base.update(kw)
    return Classification(**base)


def sub(**kw):
    return SimpleNamespace(**{**dict(levels=["internship", "coop"], countries=["CA", "US"]), **kw})


def test_basic_match():
    assert matches(sub(), tags())


def test_ineligible_never_matches():
    assert not matches(sub(), tags(eligible=False))


def test_level_filter():
    assert not matches(sub(levels=["coop"]), tags(level="internship"))
    assert matches(sub(levels=["coop"]), tags(level="coop"))


def test_country_filter_and_opaque_and_remote():
    assert not matches(sub(countries=["US"]), tags(countries=["CA"]))
    assert matches(sub(countries=["US"]), tags(countries=[], location_opaque=True))
    assert matches(sub(countries=["US"]), tags(countries=[], remote=True))
    assert matches(sub(countries=["us"]), tags(countries=["US"]))


def test_sort_key_tier_then_priority():
    items = [(3, 1), (1, 2), (1, 1), (2, 1)]
    assert sorted(items, key=lambda x: sort_key(*x)) == [(1, 1), (1, 2), (2, 1), (3, 1)]
