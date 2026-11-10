import json
from pathlib import Path

import httpx
import pytest
import respx

import jobhunt.adapters.gem as gm
from jobhunt.models import CompanyConfig

FIX = Path(__file__).parent / "fixtures" / "gem"
URL = "https://api.gem.com/job_board/v0/ex/job_posts/"
CO = CompanyConfig(name="Ex", ats="gem", slug="ex")


def run():
    return gm.GemAdapter(httpx.Client()).fetch(CO)


@respx.mock
def test_parse_dedupe():
    respx.get(URL).mock(return_value=httpx.Response(200, json=json.loads((FIX / "posts.json").read_text())))
    jobs = run()
    assert [j.external_id for j in jobs] == ["g1", "g2"]
    j = jobs[0]
    assert j.title == "Intern, Engineering" and j.location == "Waterloo, Canada"
    assert j.department == "Eng" and j.employment_type == "intern" and j.description == "Hi there"
    assert j.url == "https://jobs.gem.com/ex/g1" and j.posted_at.year == 2026
    assert jobs[1].description == "x" and jobs[1].location is None and jobs[1].department is None


@respx.mock
def test_empty_board():
    respx.get(URL).mock(return_value=httpx.Response(200, json=[]))
    assert run() == []


@respx.mock
def test_http_error_propagates():
    respx.get(URL).mock(return_value=httpx.Response(404))
    with pytest.raises(httpx.HTTPStatusError):
        run()
