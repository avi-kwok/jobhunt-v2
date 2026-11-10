import json
from pathlib import Path

import httpx
import pytest
import respx

from jobhunt.adapters import apple
from jobhunt.adapters.apple import AppleAdapter
from jobhunt.models import CompanyConfig

CO = CompanyConfig(name="Apple", ats="apple")
FIX = json.loads((Path(__file__).parent / "fixtures/apple/search_page.json").read_text())


def result(pid, title):
    return {"positionId": str(pid), "postingTitle": title, "transformedPostingTitle": "t",
            "locations": [], "jobSummary": ""}


def run():
    return AppleAdapter(client=httpx.Client()).fetch(CO)


def csrf():
    return respx.get(apple.CSRF_URL).mock(
        return_value=httpx.Response(200, headers={"x-apple-csrf-token": "tok"}))


@respx.mock
def test_parse_and_title_filter():
    csrf()
    route = respx.post(apple.SEARCH_URL).mock(return_value=httpx.Response(200, json=FIX))
    jobs = run()
    assert [j.external_id for j in jobs] == ["200676091"]  # "Senior Engineer" dropped, deduped over queries
    j = jobs[0]
    assert j.location == "Cupertino, California, United States of America"
    assert j.department == "Software and Services"
    assert j.description == "Build great things."
    assert j.url == "https://jobs.apple.com/en-us/details/200676091/software-engineering-internship"
    assert j.posted_at.year == 2026 and j.uid == "apple:Apple:200676091"
    assert route.call_count == len(apple.INTERN_QUERIES)  # short page ends each query
    assert route.calls[0].request.headers["x-apple-csrf-token"] == "tok"


@respx.mock
def test_pagination_and_cap(monkeypatch):
    monkeypatch.setattr(apple, "MAX_PAGES", 3)
    monkeypatch.setattr(apple, "INTERN_QUERIES", ("intern",))
    csrf()
    pages = iter(range(1000))
    def handler(req):
        n = next(pages)
        return httpx.Response(200, json={"res": {"searchResults": [
            result(n * 100 + i, "X Intern") for i in range(20)]}})
    route = respx.post(apple.SEARCH_URL).mock(side_effect=handler)
    assert len(run()) == 60 and route.call_count == 3


@respx.mock
def test_stops_when_page_has_no_intern_titles(monkeypatch):
    monkeypatch.setattr(apple, "INTERN_QUERIES", ("intern",))
    csrf()
    pages = iter([[result(i, "Intern") for i in range(20)],
                  [result(100 + i, "Engineer") for i in range(20)]])
    route = respx.post(apple.SEARCH_URL).mock(side_effect=lambda r: httpx.Response(
        200, json={"res": {"searchResults": next(pages)}}))
    assert len(run()) == 20 and route.call_count == 2


@respx.mock
def test_empty_board():
    csrf()
    respx.post(apple.SEARCH_URL).mock(return_value=httpx.Response(200, json={"res": {"searchResults": []}}))
    assert run() == []


@respx.mock
def test_http_error_propagates():
    csrf()
    respx.post(apple.SEARCH_URL).mock(return_value=httpx.Response(404))
    with pytest.raises(httpx.HTTPStatusError):
        run()
