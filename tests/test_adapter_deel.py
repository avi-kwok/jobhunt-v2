from pathlib import Path

import httpx
import pytest
import respx

from jobhunt.adapters.deel import DeelAdapter
from jobhunt.models import CompanyConfig

FIX = Path(__file__).parent / "fixtures" / "deel"
URL = "https://jobs.deel.com/acme"
CO = CompanyConfig(name="Acme", ats="deel", slug="acme", host="jobs.deel.com")


def run():
    return DeelAdapter(client=httpx.Client()).fetch(CO)


@respx.mock
def test_parse_and_dedupe():
    route = respx.get(URL).mock(
        return_value=httpx.Response(200, text=(FIX / "klarna_board.html").read_text())
    )
    jobs = run()
    assert route.call_count == 1
    assert [j.external_id for j in jobs] == ["p1", "p2"]  # p1 duplicated in payload
    j = jobs[0]
    assert j.title == "Software Engineer Intern"
    assert j.location == "Stockholm; Berlin"
    assert j.department == "Engineering" and j.employment_type == "Internship"
    assert j.url == "https://jobs.deel.com/acme/job-details/p1/overview"
    assert j.description == "Build stuff"  # resolved from RSC text chunk
    assert j.posted_at.year == 2026 and j.uid == "deel:Acme:p1"


@respx.mock
def test_empty_board():
    respx.get(URL).mock(return_value=httpx.Response(200, text=(FIX / "empty_board.html").read_text()))
    assert run() == []


@respx.mock
def test_http_error_propagates():
    respx.get(URL).mock(return_value=httpx.Response(404))
    with pytest.raises(httpx.HTTPStatusError):
        run()


def test_requires_slug():
    with pytest.raises(ValueError):
        DeelAdapter(client=httpx.Client()).fetch(CompanyConfig(name="X", ats="deel"))


@respx.mock
def test_missing_flight_key_raises():
    respx.get(URL).mock(return_value=httpx.Response(200, text="<html><body>Just a moment...</body></html>"))
    with pytest.raises(ValueError, match="jobPostings"):
        run()
