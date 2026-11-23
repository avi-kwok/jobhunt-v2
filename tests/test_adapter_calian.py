import json
from pathlib import Path

import httpx
import pytest
import respx

import jobhunt.adapters.calian as ca
from jobhunt.models import CompanyConfig

FIX = Path(__file__).parent / "fixtures" / "calian"
URL = "https://careers.calian.com/wp-json/wp/v2/career"
CO = CompanyConfig(name="Calian", ats="calian", host="careers.calian.com")


def fx(n):
    return json.loads((FIX / n).read_text())


def run():
    return ca.CalianAdapter(httpx.Client()).fetch(CO)


@respx.mock
def test_parse_fields_and_location():
    respx.get(URL).mock(
        side_effect=lambda req: httpx.Response(
            200,
            json=fx("page1.json") if req.url.params["page"] == "1" else [],
            headers={"x-wp-totalpages": "2"},
        )
    )
    jobs = {j.external_id: j for j in run()}
    assert sorted(jobs) == ["54802", "56140", "57538"]
    j = jobs["54802"]
    assert j.title == "General Dentist" and j.source == "calian" and j.company == "Calian"
    assert j.url == "https://careers.calian.com/careers/df-general-dentist-55688/"
    assert j.location == "Barrie, ON"
    assert j.description.startswith("Location: Barrie, ON")
    assert j.posted_at is not None and j.posted_at.year == 2026
    assert jobs["56140"].location == "Victoria, BC"
    assert jobs["57538"].location is None
    assert jobs["57538"].title == "Telecom installer"


def test_location_ignores_other_location_headings():
    assert ca._location("<p>Location and Privacy: data is stored in Canada.</p>") is None
    assert ca._location("<p>Location 1: Ottawa, ON</p>\n<p>Location 2: Halifax, NS</p>") == (
        "Ottawa, ON | Halifax, NS"
    )


@respx.mock
def test_page_cap_and_dedupe(monkeypatch):
    monkeypatch.setattr(ca, "MAX_PAGES", 2)
    route = respx.get(URL).mock(
        return_value=httpx.Response(200, json=fx("page1.json"), headers={"x-wp-totalpages": "99"})
    )
    jobs = run()
    assert route.call_count == 2
    assert len(jobs) == 3


@respx.mock
def test_empty_board():
    respx.get(URL).mock(
        return_value=httpx.Response(200, json=fx("empty.json"), headers={"x-wp-totalpages": "0"})
    )
    assert run() == []


@respx.mock
def test_http_error_propagates():
    respx.get(URL).mock(return_value=httpx.Response(404, json={"code": "rest_no_route"}))
    with pytest.raises(httpx.HTTPStatusError):
        run()


def test_needs_host():
    with pytest.raises(ValueError):
        ca.CalianAdapter(httpx.Client()).fetch(CompanyConfig(name="Calian", ats="calian"))
