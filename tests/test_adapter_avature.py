"""Avature adapter against mocked responses (no network)."""

from pathlib import Path

import httpx
import pytest
import respx

import jobhunt.adapters.avature as avature
from jobhunt.models import CompanyConfig

FIX = Path(__file__).parent / "fixtures" / "avature"


def _page(name):
    return httpx.Response(200, text=(FIX / name).read_text())


def _adapter():
    return avature.AvatureAdapter(client=httpx.Client())


def _co(name="Electronic Arts", host="jobs.ea.com", site=None):
    return CompanyConfig(name=name, ats="avature", host=host, site=site)


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(avature, "DELAY", 0.0)


def _by_search(url, mapping, default="empty.html"):
    def side(request):
        term = request.url.params.get("search")
        off = request.url.params.get("jobOffset")
        return _page(mapping.get((term, off), default))
    return respx.get(url).mock(side_effect=side)


def test_registered():
    from jobhunt.adapters.base import get_adapter

    assert type(get_adapter("avature")).__name__ == "AvatureAdapter"


@respx.mock
def test_parses_paginates_and_dedupes():
    route = _by_search(
        "https://jobs.ea.com/careers/SearchJobs",
        {("intern", "0"): "ea_p1.html", ("intern", "3"): "ea_p2.html", ("intern", "5"): "ea_noise.html",
         ("co-op", "0"): "coop.html"},
    )
    jobs = _adapter().fetch(_co())
    ids = [j.external_id for j in jobs]
    assert sorted(ids) == ["1", "10", "2", "3", "4", "5", "9"]
    assert len(ids) == len(set(ids))  # Data Intern (2) repeated across pages -> deduped
    j = next(j for j in jobs if j.external_id == "1")
    assert j.uid == "avature:Electronic Arts:1"
    assert j.title == "AI Intern"
    assert j.location == "Vancouver, Canada"
    assert j.department == "CT & Data" and j.employment_type == "Intern - Temporary Employee"
    assert j.url == "https://jobs.ea.com/en_US/careers/JobDetail/AI-Intern/1"
    # offset advanced by cards received (3 then 2)
    offs = [r.request.url.params["jobOffset"] for r in route.calls if r.request.url.params["search"] == "intern"]
    assert offs == ["0", "3", "5"]


@respx.mock
def test_stops_when_page_has_no_intern_title():
    route = _by_search("https://jobs.ea.com/careers/SearchJobs", {}, default="ea_noise.html")
    jobs = _adapter().fetch(_co())
    assert route.call_count == len(avature.TERMS)  # one page per term, then stop
    assert len(jobs) == 2


@respx.mock
def test_page_cap(monkeypatch):
    monkeypatch.setattr(avature, "MAX_PAGES", 2)
    route = _by_search("https://jobs.ea.com/careers/SearchJobs", {}, default="ea_p1.html")
    _adapter().fetch(_co())
    assert route.call_count == 2 * len(avature.TERMS)


@respx.mock
def test_custom_site_and_unclassed_spans():
    _by_search("https://careers.twosigma.com/careers/OpenRoles", {("intern", "0"): "twosigma.html"})
    jobs = _adapter().fetch(_co("Two Sigma", "careers.twosigma.com", "careers/OpenRoles"))
    j = jobs[0]
    assert j.external_id == "14289"
    assert (j.location, j.department, j.employment_type) == ("United States - NY New York", "Engineering", "Internship")


@respx.mock
def test_posted_date_and_query_stripped():
    _by_search("https://synopsys.avature.net/careers/SearchJobs", {("intern", "0"): "synopsys.html"})
    j = _adapter().fetch(_co("Synopsys", "synopsys.avature.net"))[0]
    assert j.external_id == "19617" and j.employment_type == "Intern"
    assert j.posted_at.year == 2026 and j.posted_at.month == 11 and j.posted_at.tzinfo is not None
    assert "?" not in j.url


@respx.mock
def test_description_snippet_and_plain_location():
    _by_search("https://careers.lululemon.com/careers/SearchJobs", {("intern", "0"): "lulu.html"})
    j = _adapter().fetch(_co("Lululemon", "careers.lululemon.com"))[0]
    assert j.location == "Canada · BC · Vancouver"
    assert "makes gear" in j.description


@respx.mock
def test_empty_board_with_marker_is_ok():
    _by_search("https://jobs.ea.com/careers/SearchJobs", {})
    assert _adapter().fetch(_co()) == []


@respx.mock
def test_redesign_page_raises():
    respx.get("https://jobs.ea.com/careers/SearchJobs").mock(return_value=_page("redesign.html"))
    with pytest.raises(ValueError):
        _adapter().fetch(_co())


@respx.mock
def test_http_error_propagates():
    respx.get("https://jobs.ea.com/careers/SearchJobs").mock(return_value=httpx.Response(404, text="nope"))
    with pytest.raises(httpx.HTTPStatusError):
        _adapter().fetch(_co())


def test_needs_host():
    with pytest.raises(ValueError):
        _adapter().fetch(CompanyConfig(name="X", ats="avature"))
