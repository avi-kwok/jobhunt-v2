"""Eightfold adapter against mocked responses (no network)."""

import json
from pathlib import Path

import httpx
import pytest
import respx

from jobhunt.adapters import eightfold
from jobhunt.adapters.eightfold import MAX_PAGES, QUERIES, EightfoldAdapter
from jobhunt.models import CompanyConfig

FIX = Path(__file__).parent / "fixtures" / "eightfold"
HOST = "ex.eightfold.ai"
PCSX = f"https://{HOST}/api/pcsx/search"
LEGACY = f"https://{HOST}/api/apply/v2/jobs"
CO = CompanyConfig(name="Ex", ats="eightfold", host=HOST, slug="ex.com")


def fx(name):
    return json.loads((FIX / name).read_text())


def pcsx_body(ids, count):
    return {"data": {"positions": [{"id": i, "name": f"Intern {i}"} for i in ids], "count": count}}


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("jobhunt.adapters.base.time.sleep", lambda s: None)


@respx.mock
def test_pcsx_parses_fields():
    respx.get(PCSX).mock(return_value=httpx.Response(200, json=fx("pcsx_page.json")))
    jobs = EightfoldAdapter().fetch(CO)
    assert [j.external_id for j in jobs] == ["1", "2"]  # same ids across 3 queries -> deduped
    j = jobs[0]
    assert j.source == "eightfold" and j.company == "Ex"
    assert j.url == f"https://{HOST}/careers/job/1"
    assert j.location == "Plano,Texas,United States" and j.department == "Eng"
    assert j.posted_at.year == 2026
    assert len(respx.calls) == len(QUERIES)


@respx.mock
def test_search_union_across_queries():
    def handler(request):
        q = request.url.params["query"]
        ids = {"intern": [1, 2], "co-op": [2, 3], "student": [4]}[q]
        return httpx.Response(200, json=pcsx_body(ids, len(ids)))

    respx.get(PCSX).mock(side_effect=handler)
    jobs = EightfoldAdapter().fetch(CO)
    assert sorted(j.external_id for j in jobs) == ["1", "2", "3", "4"]


@respx.mock
def test_pagination_advances_start():
    def handler(request):
        if request.url.params["query"] != "intern":
            return httpx.Response(200, json=pcsx_body([], 0))
        start = int(request.url.params["start"])
        ids = list(range(start, min(start + 10, 25)))
        return httpx.Response(200, json=pcsx_body(ids, 25))

    respx.get(PCSX).mock(side_effect=handler)
    jobs = EightfoldAdapter().fetch(CO)
    assert len(jobs) == 25


@respx.mock
def test_page_cap():
    def handler(request):
        start = int(request.url.params["start"])
        return httpx.Response(200, json=pcsx_body(range(start, start + 10), 100000))

    respx.get(PCSX).mock(side_effect=handler)
    eightfold_calls = len(QUERIES) * MAX_PAGES
    EightfoldAdapter().fetch(CO)
    assert len(respx.calls) == eightfold_calls


@respx.mock
def test_falls_back_to_legacy_and_remembers():
    respx.get(PCSX).mock(return_value=httpx.Response(403, json={"message": "no"}))
    legacy = respx.get(LEGACY).mock(return_value=httpx.Response(200, json=fx("legacy_page.json")))
    ad = EightfoldAdapter()
    jobs = ad.fetch(CO)
    assert [j.external_id for j in jobs] == ["7"]
    assert jobs[0].url == "https://x.example.com/careers/job/7"
    assert jobs[0].location == "Los Gatos,California,United States"
    assert legacy.call_count == len(QUERIES)
    pcsx_calls = len([c for c in respx.calls if c.request.url.path.endswith("/search")])
    ad.fetch(CO)  # mode pinned: no more pcsx probing
    assert len([c for c in respx.calls if c.request.url.path.endswith("/search")]) == pcsx_calls


@respx.mock
def test_html_response_falls_back():
    respx.get(PCSX).mock(return_value=httpx.Response(200, text="<html>nope</html>"))
    respx.get(LEGACY).mock(return_value=httpx.Response(200, json=fx("legacy_page.json")))
    assert len(EightfoldAdapter().fetch(CO)) == 1


@respx.mock
def test_empty_board():
    respx.get(PCSX).mock(return_value=httpx.Response(200, json=pcsx_body([], 0)))
    assert EightfoldAdapter().fetch(CO) == []


@respx.mock
def test_http_error_propagates():
    respx.get(PCSX).mock(return_value=httpx.Response(403))
    respx.get(LEGACY).mock(return_value=httpx.Response(403))
    with pytest.raises(httpx.HTTPStatusError):
        EightfoldAdapter().fetch(CO)


def test_needs_host_and_domain():
    with pytest.raises(ValueError):
        EightfoldAdapter().fetch(CompanyConfig(name="X", ats="eightfold"))


@respx.mock
def test_pinned_mode_skips_pcsx_probe():
    legacy = respx.get(LEGACY).mock(return_value=httpx.Response(200, json=fx("legacy_page.json")))
    pcsx = respx.get(PCSX).mock(return_value=httpx.Response(403))
    co = CompanyConfig(name="Ex", ats="eightfold", host=HOST, slug="ex.com", eightfold_mode="legacy")
    EightfoldAdapter().fetch(co)
    assert pcsx.call_count == 0 and legacy.call_count == len(QUERIES)
