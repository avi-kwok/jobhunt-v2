import httpx
import pytest
import respx

from jobhunt.adapters import smartrecruiters as sr
from jobhunt.adapters.smartrecruiters import SmartRecruitersAdapter
from jobhunt.models import CompanyConfig

URL = "https://api.smartrecruiters.com/v1/companies/Acme/postings"
CO = CompanyConfig(name="Acme", ats="smartrecruiters", slug="Acme")


def posting(i, name="Software Engineer Intern"):
    return {
        "id": str(i), "name": name, "releasedDate": "2026-11-09T12:31:58.551Z",
        "location": {"city": "Toronto", "country": "ca", "remote": True,
                     "fullLocation": "Toronto, ON, Canada"},
        "department": {"label": "Eng"}, "typeOfEmployment": {"label": "Intern"},
    }


def run():
    return SmartRecruitersAdapter(client=httpx.Client()).fetch(CO)


@respx.mock
def test_parse_and_union_dedupe():
    def handler(request):
        q = request.url.params["q"]
        data = {"intern": [posting(1), posting(2)], "co-op": [posting(2), posting(3)], "student": []}
        return httpx.Response(200, json={"content": data[q]})
    route = respx.get(URL).mock(side_effect=handler)
    jobs = run()
    assert route.call_count == 3
    assert sorted(j.external_id for j in jobs) == ["1", "2", "3"]
    j = jobs[0]
    assert j.url == "https://jobs.smartrecruiters.com/Acme/1"
    assert j.location == "Toronto, ON, Canada (Remote)"
    assert j.department == "Eng" and j.employment_type == "Intern"
    assert j.posted_at.year == 2026 and j.uid == "smartrecruiters:Acme:1"


@respx.mock
def test_pagination_and_cap(monkeypatch):
    monkeypatch.setattr(sr, "PAGE_LIMIT", 2)
    monkeypatch.setattr(sr, "MAX_PAGES", 2)
    counter = iter(range(1000))
    route = respx.get(URL).mock(side_effect=lambda r: httpx.Response(
        200, json={"content": [posting(next(counter)), posting(next(counter))]}))
    jobs = run()
    assert route.call_count == 6  # 3 terms x 2-page cap
    assert len(jobs) == 12


@respx.mock
def test_empty_board():
    respx.get(URL).mock(return_value=httpx.Response(200, json={"content": []}))
    assert run() == []


@respx.mock
def test_http_error_propagates():
    respx.get(URL).mock(return_value=httpx.Response(404))
    with pytest.raises(httpx.HTTPStatusError):
        run()


def test_needs_slug():
    with pytest.raises(ValueError):
        SmartRecruitersAdapter(client=httpx.Client()).fetch(CompanyConfig(name="X"))
