import httpx
import pytest
import respx

from jobhunt.adapters.recruitee import RecruiteeAdapter
from jobhunt.models import CompanyConfig

URL = "https://acme.recruitee.com/api/offers/"
CO = CompanyConfig(name="Acme", ats="recruitee", slug="acme", host="acme.recruitee.com")


def offer(i, title="Intern Researcher", **kw):
    d = {
        "id": i, "guid": f"g{i}", "title": title, "status": "published",
        "location": "Markham, Ontario, Canada", "department": "Lab",
        "employment_type_code": "internship", "remote": False,
        "careers_url": f"https://acme.recruitee.com/o/job-{i}",
        "description": "<p>Build <b>things</b></p>",
        "published_at": "2026-11-03 16:41:09 UTC",
    }
    d.update(kw)
    return d


def run():
    return RecruiteeAdapter(client=httpx.Client()).fetch(CO)


@respx.mock
def test_parse_dedupe_and_single_call():
    route = respx.get(URL).mock(return_value=httpx.Response(
        200, json={"offers": [offer(1), offer(1), offer(2, status="draft"), offer(3)]}))
    jobs = run()
    assert route.call_count == 1
    assert [j.external_id for j in jobs] == ["1", "3"]
    j = jobs[0]
    assert j.url == "https://acme.recruitee.com/o/job-1"
    assert j.description == "Build things"
    assert j.employment_type == "internship" and j.department == "Lab"
    assert j.posted_at.year == 2026 and j.posted_at.tzinfo is not None
    assert j.uid == "recruitee:Acme:1"


@respx.mock
def test_remote_and_url_fallback():
    o = offer(5, remote=True, slug="s5"); del o["careers_url"]
    respx.get(URL).mock(return_value=httpx.Response(200, json={"offers": [o]}))
    j = run()[0]
    assert j.location == "Markham, Ontario, Canada (Remote)"
    assert j.url == "https://acme.recruitee.com/o/s5"


@respx.mock
def test_empty_board():
    respx.get(URL).mock(return_value=httpx.Response(200, json={"offers": []}))
    assert run() == []


@respx.mock
def test_redesign_or_challenge_raises():
    respx.get(URL).mock(return_value=httpx.Response(200, text="<html>challenge</html>"))
    with pytest.raises(ValueError):
        run()
    respx.get(URL).mock(return_value=httpx.Response(200, json={"error": "x"}))
    with pytest.raises(ValueError):
        run()


@respx.mock
def test_http_error_propagates():
    respx.get(URL).mock(return_value=httpx.Response(404))
    with pytest.raises(httpx.HTTPStatusError):
        run()
