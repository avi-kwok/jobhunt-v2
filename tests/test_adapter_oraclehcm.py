import json
from pathlib import Path

import httpx
import pytest
import respx

from jobhunt.adapters import oraclehcm
from jobhunt.adapters.oraclehcm import OracleHcmAdapter
from jobhunt.models import CompanyConfig

FIX = json.loads((Path(__file__).parent / "fixtures/oraclehcm/page.json").read_text())
URL = "https://jpmc.fa.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
CO = CompanyConfig(name="JPM", ats="oraclehcm", host="jpmc.fa.oraclecloud.com", site="CX_1001")


def _reqs(n, start=0, title="Software Intern"):
    return {"items": [{"requisitionList": [
        {"Id": str(start + i), "Title": f"{title} {i}"} for i in range(n)]}]}


def _empty():
    return httpx.Response(200, json={"items": [{"requisitionList": []}]})


@respx.mock
def test_parse_fields_and_union_dedupe():
    def handler(request):
        term = request.url.params["finder"]
        if 'keyword="intern"' in term:
            return httpx.Response(200, json=FIX)
        if 'keyword="student"' in term:  # overlaps on 210690282
            return httpx.Response(200, json={"items": [{"requisitionList": [
                FIX["items"][0]["requisitionList"][0], {"Id": "9", "Title": "Student Dev"}]}]})
        return _empty()

    respx.get(URL).mock(side_effect=handler)
    jobs = OracleHcmAdapter(client=httpx.Client()).fetch(CO)
    assert [j.external_id for j in jobs] == ["210690282", "210804704", "9"]
    j = jobs[0]
    assert j.source == "oraclehcm" and j.company == "JPM"
    assert j.url == "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/210690282"
    assert j.location == "New York, NY, United States"
    assert j.department == "Analysts"
    assert j.posted_at.year == 2026 and j.posted_at.month == 11
    assert "<" not in j.description and "Join us" in j.description
    assert jobs[1].employment_type == "Full time"
    assert 'siteNumber=CX_1001' in respx.calls[0].request.url.params["finder"]


@respx.mock
def test_pagination_cap(monkeypatch):
    monkeypatch.setattr(oraclehcm, "SEARCH_TERMS", ("intern",))
    route = respx.get(URL).mock(side_effect=lambda r: httpx.Response(
        200, json=_reqs(25, start=int(r.url.params["finder"].split("offset=")[1]) )))
    jobs = OracleHcmAdapter(client=httpx.Client()).fetch(CO)
    assert route.call_count == oraclehcm.MAX_PAGES
    assert len(jobs) == 25 * oraclehcm.MAX_PAGES


@respx.mock
def test_stops_when_page_has_no_internish_title(monkeypatch):
    monkeypatch.setattr(oraclehcm, "SEARCH_TERMS", ("intern",))
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=_reqs(25, title="Vice President")))
    OracleHcmAdapter(client=httpx.Client()).fetch(CO)
    assert route.call_count == 1


@respx.mock
def test_empty_board():
    respx.get(URL).mock(return_value=_empty())
    assert OracleHcmAdapter(client=httpx.Client()).fetch(CO) == []


@respx.mock
def test_http_error_propagates():
    respx.get(URL).mock(return_value=httpx.Response(403))
    with pytest.raises(httpx.HTTPStatusError):
        OracleHcmAdapter(client=httpx.Client()).fetch(CO)


def test_missing_config():
    with pytest.raises(ValueError):
        OracleHcmAdapter(client=httpx.Client()).fetch(CompanyConfig(name="x"))
