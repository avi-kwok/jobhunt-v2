import json
from pathlib import Path

import httpx
import pytest
import respx

import jobhunt.adapters.aptiv as ap
from jobhunt.models import CompanyConfig

FIX = Path(__file__).parent / "fixtures" / "aptiv"
URL = "https://aptivcareers.searchapi-na.hawksearch.com/api/v2/search/"
CO = CompanyConfig(
    name="Aptiv",
    ats="aptiv",
    host="aptivcareers.searchapi-na.hawksearch.com",
    slug="28fad22cfe584b879917858203dd97ce",
)
BASE = "https://www.aptiv.com/en/jobs/search/open-positions/"


def fx(n):
    return json.loads((FIX / n).read_text())


def search(request):
    body = json.loads(request.content)
    assert body["ClientGuid"] == CO.slug and body["MaxPerPage"] == ap.MAX_PER_PAGE
    if body["Keyword"] == "intern":
        return httpx.Response(200, json=fx("search_intern_p1.json" if body["Page"] == 1 else "search_intern_p2.json"))
    if body["Keyword"] == "student":
        return httpx.Response(200, json=fx("search_student.json"))
    return httpx.Response(200, json=fx("search_empty.json"))


def run():
    return ap.AptivAdapter(httpx.Client()).fetch(CO)


@respx.mock
def test_parse_fields_union_and_dedupe_across_terms():
    route = respx.post(URL).mock(side_effect=search)
    jobs = {j.external_id: j for j in run()}
    # intern pages give A, B, C; student repeats A and adds E; co-op is empty
    assert sorted(jobs) == ["J000702905", "J000704361", "J000704404", "J000705140"]
    j = jobs["J000705140"]
    assert j.source == "aptiv" and j.company == "Aptiv"
    assert j.title == "Student Engineer"
    assert j.url == BASE + "J000705140"
    assert j.location == "Gothenburg, Vastmanland County, Sweden"
    assert j.department == "Administration Support & Service"
    assert j.posted_at is not None and j.posted_at.year == 2026
    assert j.description and "Student Engineer" in j.description and "<" not in j.description
    assert jobs["J000704361"].location == "Ottawa, Ontario, Canada"
    assert route.call_count == 4  # intern p1, intern p2, student, co-op (stops on empty)


@respx.mock
def test_paging_stops_at_nofpages_and_on_repeat_page():
    respx.post(URL).mock(side_effect=search)
    run()
    bodies = [json.loads(c.request.content) for c in respx.calls]
    assert [(b["Keyword"], b["Page"]) for b in bodies] == [
        ("intern", 1),
        ("intern", 2),
        ("co-op", 1),
        ("student", 1),
    ]


@respx.mock
def test_repeated_page_stops_paging(monkeypatch):
    monkeypatch.setattr(ap, "SEARCH_TERMS", ("intern",))
    body = fx("search_intern_p1.json")
    body["Pagination"]["NofPages"] = 99  # widget that ignores Page keeps answering page 1
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=body))
    jobs = run()
    assert len(jobs) == 2
    assert route.call_count == 2  # page 1 adds 2 new, page 2 repeats both and stops


@respx.mock
def test_page_cap(monkeypatch):
    monkeypatch.setattr(ap, "SEARCH_TERMS", ("intern",))
    monkeypatch.setattr(ap, "MAX_PAGES", 3)
    body = fx("search_intern_p1.json")
    body["Pagination"]["NofPages"] = 99
    route = respx.post(URL).mock(side_effect=lambda r: _page(r, body))
    run()
    assert route.call_count == 3


def _page(request, body):
    # each page answers with fresh posting ids so the stop-on-repeat guard does not end paging
    n = json.loads(request.content)["Page"]
    hit = {"DocId": n, "Score": 1, "Document": {"title": [f"Intern {n}"], "link": [f"{BASE}P{n:06d}"]}}
    return httpx.Response(200, json={"Pagination": body["Pagination"], "Results": [hit]})


@respx.mock
def test_empty_search():
    respx.post(URL).mock(return_value=httpx.Response(200, json=fx("search_empty.json")))
    assert run() == []


@respx.mock
def test_hits_without_title_or_link_are_skipped():
    body = fx("search_intern_p1.json")
    body["Results"][0]["Document"].pop("title")
    body["Results"][1]["Document"].pop("link")
    body["Pagination"]["NofPages"] = 1
    respx.post(URL).mock(return_value=httpx.Response(200, json=body))
    assert run() == []


@respx.mock
def test_http_error_raises(monkeypatch):
    monkeypatch.setattr(ap, "SEARCH_TERMS", ("intern",))
    respx.post(URL).mock(return_value=httpx.Response(404))
    with pytest.raises(httpx.HTTPStatusError):
        run()


def test_missing_host_or_slug_raises():
    adapter = ap.AptivAdapter(httpx.Client())
    with pytest.raises(ValueError, match="needs a host and a slug"):
        adapter.fetch(CompanyConfig(name="Aptiv", ats="aptiv", slug="x"))
    with pytest.raises(ValueError, match="needs a host and a slug"):
        adapter.fetch(CompanyConfig(name="Aptiv", ats="aptiv", host="h.example"))
