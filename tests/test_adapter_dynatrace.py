import json
from pathlib import Path

import httpx
import pytest
import respx

import jobhunt.adapters.dynatrace as dt
from jobhunt.models import CompanyConfig

FIX = Path(__file__).parent / "fixtures" / "dynatrace"
URL = "https://www.dynatrace.com/api/coveo/search/"
CO = CompanyConfig(name="Dynatrace", ats="dynatrace", host="www.dynatrace.com")


def fx(n):
    return json.loads((FIX / n).read_text())


def run():
    return dt.DynatraceAdapter(httpx.Client()).fetch(CO)


def by_first(request):
    first = json.loads(request.content)["firstResult"]
    return httpx.Response(200, json=fx("page0.json" if first == 0 else "page1.json"))


@respx.mock
def test_parse_fields():
    respx.post(URL).mock(return_value=httpx.Response(200, json=fx("page0.json")))
    jobs = {j.external_id: j for j in run()}
    assert sorted(jobs) == ["1330847600", "1429647600", "1500000001"]
    j = jobs["1330847600"]
    assert j.title == "Strategic Product Lead (m/w/x), Office of CTO"
    assert j.url == "https://www.dynatrace.com/careers/jobs/1330847600/"
    assert j.location == "Vienna, Austria" and j.department == "R&D"
    assert j.source == "dynatrace" and j.company == "Dynatrace"
    assert j.description == "Short neutral posting text."
    assert j.posted_at.year == 2026 and j.posted_at.tzinfo is not None
    assert jobs["1429647600"].location == "Provo, United States"
    assert jobs["1500000001"].title.startswith("Software Engineering - Summer Internships 2027")


@respx.mock
def test_paging_and_dedupe_across_pages():
    route = respx.post(URL).mock(side_effect=by_first)
    jobs = run()
    assert len(jobs) == 3  # page1 repeats the first posting; dedupe keeps one
    sent = [json.loads(c.request.content)["firstResult"] for c in route.calls]
    assert sent == [0, 100]


@respx.mock
def test_stops_at_total_count():
    body = fx("page0.json")
    body["totalCount"] = 3
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=body))
    run()
    assert route.call_count == 1


@respx.mock
def test_stops_on_empty_page():
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=fx("empty.json")))
    assert run() == []
    assert route.call_count == 1


@respx.mock
def test_page_cap(monkeypatch):
    monkeypatch.setattr(dt, "MAX_PAGES", 1)
    route = respx.post(URL).mock(side_effect=by_first)
    assert len(run()) == 3
    assert route.call_count == 1


@respx.mock
def test_empty_board():
    respx.post(URL).mock(return_value=httpx.Response(200, json=fx("empty.json")))
    assert run() == []


@respx.mock
def test_no_location_and_multi_office():
    body = fx("page0.json")
    body["results"][0]["raw"]["office_locations"] = []
    body["results"][0]["raw"]["country"] = []
    body["results"][1]["raw"]["office_locations"] = ["Boston", "Denver"]
    body["results"][1]["raw"]["country"] = ["United States"]
    respx.post(URL).mock(return_value=httpx.Response(200, json=body))
    jobs = {j.external_id: j for j in run()}
    assert jobs["1330847600"].location is None
    assert jobs["1429647600"].location == "Boston, United States | Denver"


@respx.mock
def test_http_error_propagates():
    respx.post(URL).mock(return_value=httpx.Response(404))
    with pytest.raises(httpx.HTTPStatusError):
        run()
