import json
from pathlib import Path

import httpx
import pytest
import respx

import jobhunt.adapters.bamboohr as bh
from jobhunt.models import CompanyConfig

FIX = Path(__file__).parent / "fixtures" / "bamboohr"
BASE = "https://ex.bamboohr.com/careers"
CO = CompanyConfig(name="Ex", ats="bamboohr", slug="ex")


def fx(n):
    return json.loads((FIX / n).read_text())


def run():
    return bh.BambooHRAdapter(httpx.Client()).fetch(CO)


@respx.mock
def test_parse_dedupe_detail_for_intern_only():
    respx.get(f"{BASE}/list").mock(return_value=httpx.Response(200, json=fx("list.json")))
    det = respx.get(f"{BASE}/1/detail").mock(return_value=httpx.Response(200, json=fx("detail.json")))
    jobs = {j.external_id: j for j in run()}
    assert sorted(jobs) == ["1", "2"]  # duplicate id 2 collapsed
    assert det.call_count == 1  # no detail for non-intern title
    j = jobs["1"]
    assert j.title == "Product Co-op" and j.location == "Vancouver, British Columbia"
    assert j.url == f"{BASE}/1" and j.department == "Product" and j.employment_type == "Full-Time"
    assert j.description == "Great role" and j.posted_at.day == 28
    assert jobs["2"].location == "Remote" and jobs["2"].description is None


@respx.mock
def test_detail_cap(monkeypatch):
    monkeypatch.setattr(bh, "MAX_DETAILS", 0)
    respx.get(f"{BASE}/list").mock(return_value=httpx.Response(200, json=fx("list.json")))
    assert len(run()) == 2  # no detail route mocked; a call would raise


@respx.mock
def test_empty_board():
    respx.get(f"{BASE}/list").mock(return_value=httpx.Response(200, json=fx("empty.json")))
    assert run() == []


@respx.mock
def test_http_error_propagates():
    respx.get(f"{BASE}/list").mock(return_value=httpx.Response(403))
    with pytest.raises(httpx.HTTPStatusError):
        run()


@respx.mock
def test_detail_error_is_swallowed():
    respx.get(f"{BASE}/list").mock(return_value=httpx.Response(200, json=fx("list.json")))
    respx.get(f"{BASE}/1/detail").mock(return_value=httpx.Response(404))
    assert next(j for j in run() if j.external_id == "1").description is None
