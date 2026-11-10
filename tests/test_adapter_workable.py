import httpx
import pytest
import respx

from jobhunt.adapters.workable import WorkableAdapter
from jobhunt.models import CompanyConfig

URL = "https://apply.workable.com/api/v1/widget/accounts/acme"
CO = CompanyConfig(name="Acme", ats="workable", slug="acme")


def job(sc, title="Software Engineer Intern"):
    return {
        "title": title, "shortcode": sc, "employment_type": "Full-time",
        "telecommuting": False, "department": "Eng",
        "url": f"https://apply.workable.com/j/{sc}", "published_on": "2026-11-05",
        "country": "Canada", "city": "Toronto", "state": "Ontario",
        "description": "<p>Build <b>things</b></p>",
    }


def run():
    return WorkableAdapter(client=httpx.Client()).fetch(CO)


@respx.mock
def test_parse_single_call():
    route = respx.get(URL).mock(return_value=httpx.Response(
        200, json={"name": "Acme", "jobs": [job("A1"), job("A1"), job("B2")]}))
    jobs = run()
    assert route.call_count == 1
    assert route.calls[0].request.url.params["details"] == "true"
    assert [j.external_id for j in jobs] == ["A1", "B2"]  # deduped
    j = jobs[0]
    assert j.url == "https://apply.workable.com/j/A1"
    assert j.location == "Toronto, Ontario, Canada"
    assert j.description == "Build things"
    assert j.posted_at.year == 2026 and j.uid == "workable:Acme:A1"


@respx.mock
def test_remote_flag():
    d = job("R1"); d["telecommuting"] = True; d["city"] = d["state"] = ""
    respx.get(URL).mock(return_value=httpx.Response(200, json={"jobs": [d]}))
    assert run()[0].location == "Canada (Remote)"


@respx.mock
def test_empty_board():
    respx.get(URL).mock(return_value=httpx.Response(200, json={"name": "Acme", "jobs": []}))
    assert run() == []


@respx.mock
def test_http_error_propagates():
    respx.get(URL).mock(return_value=httpx.Response(404))
    with pytest.raises(httpx.HTTPStatusError):
        run()


def test_needs_slug():
    with pytest.raises(ValueError):
        WorkableAdapter(client=httpx.Client()).fetch(CompanyConfig(name="X"))
