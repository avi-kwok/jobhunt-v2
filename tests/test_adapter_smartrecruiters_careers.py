from pathlib import Path

import httpx
import pytest
import respx

import jobhunt.adapters.smartrecruiters_careers as sc
from jobhunt.models import CompanyConfig

FIX = Path(__file__).parent / "fixtures" / "smartrecruiters_careers"
BASE = "https://careers.smartrecruiters.com/IFS1"
GROUPS = f"{BASE}/api/groups"
CO = CompanyConfig(name="IFS", ats="smartrecruiters_careers", host="careers.smartrecruiters.com", slug="IFS1")


@pytest.fixture(autouse=True)
def no_delay(monkeypatch):
    monkeypatch.setattr(sc, "DELAY", 0)


def run():
    return sc.SmartRecruitersCareersAdapter(httpx.Client()).fetch(CO)


def fixture(name):
    return (FIX / name).read_text(encoding="utf-8")


@respx.mock
def test_parse_careers_page_and_groups(monkeypatch):
    monkeypatch.setattr(sc, "MAX_PAGES", 3)
    respx.get(BASE).mock(return_value=httpx.Response(200, text=fixture("careers.html")))
    pages = {"1": fixture("groups_page1.html"), "2": fixture("groups_page2.html")}
    respx.get(GROUPS).mock(side_effect=lambda req: httpx.Response(200, text=pages[req.url.params["page"]]))
    jobs = run()
    assert [j.external_id for j in jobs] == [
        "744000159584100", "744000154429449", "744000162367509", "744000162068789",
        "744000157726809", "744000162072594", "744000160183869",
        "744000111908045",
    ]
    copperleaf = jobs[0]
    assert copperleaf.title == "Senior Solutions Consultant ( Pre Sales) | Copperleaf | Dutch Speaker"
    assert copperleaf.location == "'s-Hertogenbosch, Netherlands"
    assert copperleaf.department == "Pre-Sales"
    assert copperleaf.url == "https://jobs.smartrecruiters.com/IFS1/744000159584100-senior-solutions-consultant-pre-sales-copperleaf-dutch-speaker"
    assert copperleaf.source == "smartrecruiters_careers" and copperleaf.company == "IFS"
    uae = jobs[4]
    assert uae.title == "Senior Project Manager (Aerospace & Defense) - UAE"
    assert uae.location == "Dubai, United Arab Emirates" and uae.department == "Consulting"
    assert jobs[-1].location == "Itasca, IL"


@respx.mock
def test_page_cap_stops_paging(monkeypatch):
    monkeypatch.setattr(sc, "MAX_PAGES", 2)
    respx.get(BASE).mock(return_value=httpx.Response(200, text=fixture("careers.html")))
    route = respx.get(GROUPS).mock(return_value=httpx.Response(200, text=fixture("groups_page2.html")))
    jobs = run()
    assert route.call_count == 1
    assert len(jobs) == 5


@respx.mock
def test_repeated_postings_are_deduplicated(monkeypatch):
    monkeypatch.setattr(sc, "MAX_PAGES", 3)
    respx.get(BASE).mock(return_value=httpx.Response(200, text=fixture("careers.html")))
    respx.get(GROUPS).mock(return_value=httpx.Response(200, text=fixture("groups_page1.html")))
    jobs = run()
    ids = [j.external_id for j in jobs]
    assert len(ids) == len(set(ids)) == 7


@respx.mock
def test_empty_board():
    respx.get(BASE).mock(return_value=httpx.Response(200, text='<section id="st-openings" class="site-section openings"></section>'))
    assert run() == []


@respx.mock
def test_missing_container_raises():
    respx.get(BASE).mock(return_value=httpx.Response(200, text="<html><body>Checking your browser</body></html>"))
    with pytest.raises(ValueError, match="openings container"):
        run()


def test_missing_host_or_slug_raises():
    bad = CompanyConfig(name="IFS", ats="smartrecruiters_careers", host=None, slug="IFS1")
    with pytest.raises(ValueError, match="needs a host and a slug"):
        sc.SmartRecruitersCareersAdapter(httpx.Client()).fetch(bad)


@respx.mock
def test_board_http_error_propagates():
    respx.get(BASE).mock(return_value=httpx.Response(404))
    with pytest.raises(httpx.HTTPStatusError):
        run()


@respx.mock
def test_groups_http_error_propagates(monkeypatch):
    monkeypatch.setattr(sc, "MAX_PAGES", 2)
    respx.get(BASE).mock(return_value=httpx.Response(200, text=fixture("careers.html")))
    respx.get(GROUPS).mock(return_value=httpx.Response(404))
    with pytest.raises(httpx.HTTPStatusError):
        run()


MORE = f"{BASE}/api/more"
EMPTY_PAGE = "<html><body>We couldn't load the data now.</body></html>"


@respx.mock
def test_show_more_pages_a_location_group_until_no_new_ids():
    respx.get(BASE).mock(return_value=httpx.Response(200, text=fixture("careers_more.html")))
    pages = {"1": fixture("more_page1.html"), "2": fixture("more_page2.html"), "3": EMPTY_PAGE}
    route = respx.get(MORE).mock(side_effect=lambda req: httpx.Response(200, text=pages[req.url.params["page"]]))
    jobs = run()
    assert route.call_count == 3
    first = route.calls[0].request.url
    assert first.params["type"] == "location" and first.params["value"] == "Colombo, LK"
    assert [j.external_id for j in jobs] == [
        "744000162221919", "744000161998961", "744000159061950", "744000158199089", "744000149575149",
    ]
    assert {j.location for j in jobs} == {"Colombo, Sri Lanka"}
    assert jobs[2].title == "Service Delivery Manager" and jobs[2].department == "Consulting"


@respx.mock
def test_show_more_stops_at_advertised_count():
    text = fixture("careers_more.html").replace('data-qty="23"', 'data-qty="5"')
    respx.get(BASE).mock(return_value=httpx.Response(200, text=text))
    pages = {"1": fixture("more_page1.html"), "2": fixture("more_page2.html")}
    route = respx.get(MORE).mock(side_effect=lambda req: httpx.Response(200, text=pages[req.url.params["page"]]))
    jobs = run()
    assert route.call_count == 2
    assert len(jobs) == 5


@respx.mock
def test_show_more_stops_when_page_repeats_rendered_cards():
    respx.get(BASE).mock(return_value=httpx.Response(200, text=fixture("careers_more.html")))
    route = respx.get(MORE).mock(return_value=httpx.Response(200, text=fixture("careers_more.html")))
    jobs = run()
    assert route.call_count == 1
    assert len(jobs) == 2


@respx.mock
def test_show_more_request_cap(monkeypatch):
    monkeypatch.setattr(sc, "MAX_MORE", 1)
    respx.get(BASE).mock(return_value=httpx.Response(200, text=fixture("careers_more.html")))
    route = respx.get(MORE).mock(side_effect=lambda req: httpx.Response(200, text=fixture("more_page1.html")))
    jobs = run()
    assert route.call_count == 1
    assert len(jobs) == 4
