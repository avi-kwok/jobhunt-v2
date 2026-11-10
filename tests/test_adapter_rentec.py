from pathlib import Path

import httpx
import pytest
import respx

import jobhunt.adapters.rentec as rt
from jobhunt.models import CompanyConfig

FIX = Path(__file__).parent / "fixtures" / "rentec"
URL = "https://www.rentec.com/Careers.action"
CO = CompanyConfig(name="Renaissance Technologies", ats="rentec", host="www.rentec.com")


def run():
    return rt.RentecAdapter(httpx.Client()).fetch(CO)


def _list_route(**kw):
    return respx.get(URL, params={"jobs": "true"}).mock(**kw)


def _is_list(request):
    return "selectedPosition" not in request.url.params


@respx.mock
def test_parse_dedupe_and_intern_detail():
    board = (FIX / "board.html").read_text()
    detail = (FIX / "detail.html").read_text()
    seen = []

    def handler(request):
        seen.append(request.url.params.get("selectedPosition"))
        return httpx.Response(200, text=board if _is_list(request) else detail)

    respx.get(url__startswith=URL).mock(side_effect=handler)
    jobs = run()
    assert [j.external_id for j in jobs] == [
        "dataProgrammer", "financialInfrastructureProgrammer", "researchInternSummer"
    ]
    j = jobs[0]
    assert j.title == "Data Programmer" and j.location == "East Setauket, NY"
    assert j.department == "Programming" and j.source == "rentec"
    assert j.company == "Renaissance Technologies" and j.description is None
    assert j.url == "https://www.rentec.com/Careers.action?jobs=true&selectedPosition=dataProgrammer"
    assert jobs[1].location == "New York, NY / East Setauket, NY"
    intern = jobs[2]
    assert intern.department == "Research" and intern.title == "Research Software Intern"
    assert intern.description.startswith("Responsibilities")
    assert "C/C++" in intern.description and "Apply Now" not in intern.description
    # one list request + exactly one detail fetch (only the intern-like title)
    assert seen == [None, "researchInternSummer"]


@respx.mock
def test_detail_fetch_capped(monkeypatch):
    monkeypatch.setattr(rt, "MAX_DETAILS", 0)
    route = respx.get(url__startswith=URL).mock(
        return_value=httpx.Response(200, text=(FIX / "board.html").read_text())
    )
    jobs = run()
    assert len(jobs) == 3 and jobs[2].description is None and route.call_count == 1


@respx.mock
def test_detail_error_keeps_job():
    board = (FIX / "board.html").read_text()
    respx.get(url__startswith=URL).mock(
        side_effect=lambda r: httpx.Response(200, text=board) if _is_list(r) else httpx.Response(404)
    )
    jobs = run()
    assert jobs[2].external_id == "researchInternSummer" and jobs[2].description is None


@respx.mock
def test_empty_board():
    _list_route(return_value=httpx.Response(200, text=(FIX / "empty.html").read_text()))
    assert run() == []


@respx.mock
def test_challenge_page_raises():
    _list_route(return_value=httpx.Response(
        200, text="<html><head><title>Just a moment...</title></head><body>Checking your browser</body></html>"
    ))
    with pytest.raises(ValueError, match="not the jobs page"):
        run()


@respx.mock
def test_http_error_propagates():
    _list_route(return_value=httpx.Response(404))
    with pytest.raises(httpx.HTTPStatusError):
        run()
