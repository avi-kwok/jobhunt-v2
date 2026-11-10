from pathlib import Path

import httpx
import pytest
import respx

import jobhunt.adapters.successfactors as sf
from jobhunt.models import CompanyConfig

FIX = Path(__file__).parent / "fixtures" / "successfactors"
URL = "https://jobs.example.com/search/"
CO = CompanyConfig(name="Ex", ats="successfactors", host="jobs.example.com")


def fx(name):
    return (FIX / name).read_text()


def run():
    a = sf.SuccessFactorsAdapter(httpx.Client())
    return a.fetch(CO)


@respx.mock
def test_parse_union_dedupe():
    def handler(request):
        q = request.url.params["q"]
        return httpx.Response(200, text=fx("page1.html" if q == "intern" else "page_dupe.html" if q == "co-op" else "empty.html"))

    respx.get(URL).mock(side_effect=handler)
    jobs = run()
    assert sorted(j.external_id for j in jobs) == ["1001", "1002", "1003"]
    j = next(j for j in jobs if j.external_id == "1001")
    assert j.title == "Software Developer Intern & Co-op"
    assert j.location == "Toronto, ON, CA"
    assert j.url == "https://jobs.example.com/job/Toronto-Software-Developer-Intern-ON/1001/"
    assert j.posted_at.year == 2026 and j.posted_at.month == 11 and j.posted_at.day == 7
    assert j.source == "successfactors" and j.description is None


@respx.mock
def test_pagination_cap():
    route = respx.get(URL).mock(return_value=httpx.Response(200, text=fx("full.html")))
    run()
    # each of 3 terms stops at MAX_PAGES even though total says 10000
    assert route.call_count == len(sf.SEARCH_TERMS) * sf.MAX_PAGES
    starts = {r.request.url.params["startrow"] for r in route.calls}
    assert "25" in starts and str(sf.PAGE_SIZE * sf.MAX_PAGES) not in starts


@respx.mock
def test_stops_when_total_reached():
    route = respx.get(URL).mock(return_value=httpx.Response(200, text=fx("page1.html")))
    run()
    assert route.call_count == len(sf.SEARCH_TERMS)  # one short page per term


@respx.mock
def test_empty_board():
    respx.get(URL).mock(return_value=httpx.Response(200, text=fx("empty.html")))
    assert run() == []


@respx.mock
def test_http_error_propagates():
    respx.get(URL).mock(return_value=httpx.Response(403))
    with pytest.raises(httpx.HTTPStatusError):
        run()


def test_needs_host():
    with pytest.raises(ValueError):
        sf.SuccessFactorsAdapter(httpx.Client()).fetch(CompanyConfig(name="x"))


@respx.mock
def test_challenge_page_raises():
    respx.get(URL).mock(return_value=httpx.Response(200, text="<html><body>Access denied</body></html>"))
    with pytest.raises(ValueError, match="results container"):
        run()
