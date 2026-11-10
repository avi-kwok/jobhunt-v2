import json
from pathlib import Path

import httpx
import pytest
import respx

from jobhunt.adapters import phenom
from jobhunt.adapters.phenom import PhenomAdapter
from jobhunt.models import CompanyConfig

FIX = json.loads((Path(__file__).parent / "fixtures" / "phenom" / "page.json").read_text())
URL = "https://careers.example.com/widgets"
CO = CompanyConfig(name="Ex", ats="phenom", host="careers.example.com", slug="EXUS", site="us/en")


def resp(jobs):
    return httpx.Response(200, json={"refineSearch": {"status": 200, "data": {"jobs": jobs}}})


def mkjob(i, title="Software Intern"):
    return {"jobId": str(i), "jobSeqNo": f"SEQ{i}", "title": title}


@respx.mock
def test_parses_fields():
    route = respx.post(URL).mock(return_value=resp(FIX["refineSearch"]["data"]["jobs"]))
    jobs = PhenomAdapter(client=httpx.Client()).fetch(CO)
    assert len(jobs) == 3  # same 3 jobs from every term, deduped
    j = jobs[0]
    assert j.source == "phenom" and j.company == "Ex"
    assert j.url.startswith("https://careers.example.com/us/en/job/")
    assert j.posted_at is not None and j.title and j.description
    body = json.loads(route.calls[0].request.content)
    assert body["refNum"] == "EXUS" and body["lang"] == "en_us" and body["country"] == "us"


@respx.mock
def test_union_and_dedupe():
    def side(request):
        kw = json.loads(request.content)["keywords"]
        return resp({"intern": [mkjob(1), mkjob(2)], "co-op": [mkjob(2), mkjob(3)]}.get(kw, [mkjob(1)]))

    respx.post(URL).mock(side_effect=side)
    ids = sorted(j.external_id for j in PhenomAdapter(client=httpx.Client()).fetch(CO))
    assert ids == ["1", "2", "3"]


@respx.mock
def test_pagination_and_cap(monkeypatch):
    monkeypatch.setattr(phenom, "PAGE_SIZE", 2)
    monkeypatch.setattr(phenom, "MAX_PAGES", 3)
    monkeypatch.setattr(phenom, "SEARCH_TERMS", ("intern",))
    n = {"i": 0}

    def side(request):
        n["i"] += 1
        return resp([mkjob(n["i"] * 10), mkjob(n["i"] * 10 + 1)])  # always full page

    route = respx.post(URL).mock(side_effect=side)
    jobs = PhenomAdapter(client=httpx.Client()).fetch(CO)
    assert route.call_count == 3 and len(jobs) == 6
    assert [json.loads(c.request.content)["from"] for c in route.calls] == [0, 2, 4]


@respx.mock
def test_empty_board():
    respx.post(URL).mock(return_value=resp([]))
    assert PhenomAdapter(client=httpx.Client()).fetch(CO) == []


@respx.mock
def test_http_error_propagates():
    respx.post(URL).mock(return_value=httpx.Response(404))
    with pytest.raises(httpx.HTTPStatusError):
        PhenomAdapter(client=httpx.Client()).fetch(CO)


def test_missing_config():
    with pytest.raises(ValueError):
        PhenomAdapter(client=httpx.Client()).fetch(CompanyConfig(name="X", ats="phenom"))
