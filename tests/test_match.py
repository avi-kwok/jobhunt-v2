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


def test_sort_key_newest_first_none_last():
    from datetime import datetime, timezone
    old = datetime(2026, 1, 1, tzinfo=timezone.utc)
    new = datetime(2026, 2, 1)  # naive counts as UTC
    assert sorted([None, old, new], key=sort_key) == [new, old, None]


from jobhunt.match import parse_grad_date, passes_grad_filter, term_start  # noqa: E402


def test_term_start_months():
    assert term_start("Winter 2028") == (2028, 1) and term_start("Spring 2028") == (2028, 3)
    assert term_start("Summer 2028") == (2028, 5) and term_start("Fall 2028") == (2028, 9)
    assert term_start("Autumn 2028") is None and term_start("Summer 28") is None


def test_grad_filter_rule():
    g = "2028-04"
    assert passes_grad_filter(None, ["Summer 2030"])  # no filter
    assert passes_grad_filter(g, [])  # no terms
    for t in ("Summer 2027", "Fall 2027", "Winter 2028"):
        assert passes_grad_filter(g, [t])
    assert not passes_grad_filter(g, ["Summer 2028"])
    assert passes_grad_filter(g, ["Spring 2028", "Fall 2029"])  # Spring starts 03 < 04
    assert passes_grad_filter(g, ["Summer 2029", "Fall 2027"])  # ANY term
    assert not passes_grad_filter(g, ["Summer 2028", "Fall 2028"])
    # strict: term starting exactly in the grad month fails
    assert not passes_grad_filter("2028-05", ["Summer 2028"])
    assert passes_grad_filter("2028-06", ["Summer 2028"])


def test_grad_filter_malformed_is_no_filter():
    for bad in ("", "2028", "2028-13", "2028-00", "28-04", "2028/04", "abcd-ef", "2028-4"):
        assert parse_grad_date(bad) is None
        assert passes_grad_filter(bad, ["Summer 2030"])
    assert passes_grad_filter("2028-04", ["garbage"])


def test_matches_applies_grad_filter_last():
    assert matches(sub(grad_date="2028-04"), tags(terms=["Fall 2027"]))
    assert not matches(sub(grad_date="2028-04"), tags(terms=["Summer 2028"]))
    assert matches(sub(grad_date="2028-04"), tags(terms=[]))
    assert matches(sub(), tags(terms=["Summer 2035"]))  # no grad_date attr at all
    assert not matches(sub(grad_date="2028-04", levels=["coop"]), tags(terms=["Fall 2027"]))
