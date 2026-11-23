from pathlib import Path

import httpx
import pytest
import respx

import jobhunt.adapters.thrivehealth as th
from jobhunt.models import CompanyConfig

FIX = Path(__file__).parent / "fixtures" / "thrivehealth"
URL = "https://www.thrive.health/careers"
CO = CompanyConfig(name="Thrive Health", ats="thrivehealth", host="www.thrive.health")


def run():
    return th.ThriveHealthAdapter(httpx.Client()).fetch(CO)


@respx.mock
def test_parse_cards():
    respx.get(URL).mock(return_value=httpx.Response(200, text=(FIX / "careers.html").read_text()))
    jobs = run()
    assert [j.external_id for j in jobs] == ["clinical-business-development-lead", "registered-nurse-virtual-care"]
    j = jobs[0]
    assert j.title == "Clinical Business Development Lead"
    assert j.url == "https://www.thrive.health/careers/clinical-business-development-lead"
    assert j.location == "Remote — Alberta"
    assert j.department is None and j.source == "thrivehealth" and j.company == "Thrive Health"
    assert jobs[1].location == "Remote — Ontario"


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
    respx.get(URL).mock(return_value=httpx.Response(200, text="<html><body>Checking your browser</body></html>"))
    with pytest.raises(ValueError, match="container"):
        run()


def test_needs_host():
    with pytest.raises(ValueError):
        th.ThriveHealthAdapter(httpx.Client()).fetch(CompanyConfig(name="x", ats="thrivehealth"))
