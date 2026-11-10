from pathlib import Path

import httpx
import pytest
import respx

import jobhunt.adapters.jazzhr as jz
from jobhunt.models import CompanyConfig

FIX = Path(__file__).parent / "fixtures" / "jazzhr"
URL = "https://ex.applytojob.com/apply"
CO = CompanyConfig(name="Ex", ats="jazzhr", slug="ex")


def run():
    return jz.JazzHRAdapter(httpx.Client()).fetch(CO)


@respx.mock
def test_parse_and_dedupe():
    respx.get(URL).mock(return_value=httpx.Response(200, text=(FIX / "board.html").read_text()))
    jobs = run()
    assert [j.external_id for j in jobs] == ["AAA111", "BBB222"]
    j = jobs[0]
    assert j.title == "Data Intern" and j.location == "Remote" and j.department == "Analytics"
    assert j.url == "https://ex.applytojob.com/apply/AAA111/Data-Intern" and j.source == "jazzhr"
    assert jobs[1].location == "Toronto, ON" and jobs[1].department is None


@respx.mock
def test_empty_board():
    respx.get(URL).mock(return_value=httpx.Response(200, text=(FIX / "empty.html").read_text()))
    assert run() == []


@respx.mock
def test_http_error_propagates():
    respx.get(URL).mock(return_value=httpx.Response(404))
    with pytest.raises(httpx.HTTPStatusError):
        run()


def test_needs_slug():
    with pytest.raises(ValueError):
        jz.JazzHRAdapter(httpx.Client()).fetch(CompanyConfig(name="x", ats="jazzhr"))


@respx.mock
def test_challenge_page_raises():
    respx.get(URL).mock(return_value=httpx.Response(200, text="<html><body>Checking your browser</body></html>"))
    with pytest.raises(ValueError, match="container"):
        run()
