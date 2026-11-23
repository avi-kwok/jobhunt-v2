from pathlib import Path

import httpx
import pytest
import respx

import jobhunt.adapters.symbotic as sy
from jobhunt.models import CompanyConfig

FIX = Path(__file__).parent / "fixtures" / "symbotic"
URL = "https://www.symbotic.com/careers/jobs/"
CO = CompanyConfig(name="Symbotic", ats="symbotic", host="www.symbotic.com")


def run():
    return sy.SymboticAdapter(httpx.Client()).fetch(CO)


@respx.mock
def test_parse_visible_and_hidden_rows_deduped():
    respx.get(URL).mock(return_value=httpx.Response(200, text=(FIX / "listings.html").read_text()))
    jobs = run()
    assert [j.external_id for j in jobs] == ["R8316", "R8544", "R8228", "R8571"]
    j = jobs[0]
    assert j.title == "Staff Mechanical Engineer – Mobile Bot"
    assert j.url == "https://www.symbotic.com/careers/jobs/R8316/"
    assert j.location == "Milpitas, CA, USA"
    assert j.department == "Hardware & Industrial Controls Engineering"
    assert j.source == "symbotic" and j.company == "Symbotic"
    assert jobs[1].location == "Wilmer, TX, USA" and jobs[1].department == "Customer Site Operations"
    assert jobs[2].location == "Remote & Travel - Ontario, CAN"
    assert jobs[3].location == "Wilmington, MA - ITC, USA"


@respx.mock
def test_empty_listing_returns_empty():
    respx.get(URL).mock(return_value=httpx.Response(200, text=(FIX / "empty.html").read_text()))
    assert run() == []


@respx.mock
def test_http_error_propagates():
    respx.get(URL).mock(return_value=httpx.Response(404))
    with pytest.raises(httpx.HTTPStatusError):
        run()


@respx.mock
def test_missing_container_raises():
    respx.get(URL).mock(return_value=httpx.Response(200, text="<html><body>Access denied</body></html>"))
    with pytest.raises(ValueError, match="container"):
        run()


def test_needs_host():
    with pytest.raises(ValueError):
        sy.SymboticAdapter(httpx.Client()).fetch(CompanyConfig(name="x", ats="symbotic"))
