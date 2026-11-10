import json
from pathlib import Path

import httpx
import pytest
import respx

import jobhunt.adapters.rippling as rp
from jobhunt.models import CompanyConfig

FIX = Path(__file__).parent / "fixtures" / "rippling"
URL = "https://ats.rippling.com/api/v2/board/ex/jobs"
CO = CompanyConfig(name="Ex", ats="rippling", slug="ex")


def fx(n):
    return json.loads((FIX / n).read_text())


def run():
    return rp.RipplingAdapter(httpx.Client()).fetch(CO)


def pages(request):
    return httpx.Response(200, json=fx("page1.json" if request.url.params["page"] == "1" else "page0.json"))


@respx.mock
def test_parse_paginate_dedupe_merge_locations():
    respx.get(URL).mock(side_effect=pages)
    respx.get(f"{URL}/a1").mock(return_value=httpx.Response(200, json=fx("detail.json")))
    respx.get(f"{URL}/a3").mock(return_value=httpx.Response(404))
    jobs = {j.external_id: j for j in run()}
    assert sorted(jobs) == ["a1", "a2", "a3"]
    j = jobs["a1"]
    assert j.location == "Toronto | Remote" and j.department == "Eng"
    assert j.url == "https://ats.rippling.com/ex/jobs/a1" and j.source == "rippling"
    assert j.description == "Build things\n\nAbout us" and j.posted_at.year == 2026
    assert j.employment_type == "Intern"
    assert jobs["a2"].description is None and jobs["a2"].posted_at is None
    assert jobs["a3"].description is None  # detail failure is swallowed


@respx.mock
def test_page_cap_and_detail_cap(monkeypatch):
    monkeypatch.setattr(rp, "MAX_PAGES", 3)
    monkeypatch.setattr(rp, "MAX_DETAILS", 0)
    body = fx("page0.json")
    body["totalPages"] = 99
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=body))
    run()
    assert route.call_count == 3


@respx.mock
def test_empty_board():
    respx.get(URL).mock(return_value=httpx.Response(200, json=fx("empty.json")))
    assert run() == []


@respx.mock
def test_http_error_propagates():
    respx.get(URL).mock(return_value=httpx.Response(404, json={"error_code": "RESOURCE_NOT_FOUND"}))
    with pytest.raises(httpx.HTTPStatusError):
        run()


def test_needs_slug():
    with pytest.raises(ValueError):
        rp.RipplingAdapter(httpx.Client()).fetch(CompanyConfig(name="x", ats="rippling"))
