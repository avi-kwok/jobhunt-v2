from pathlib import Path

import httpx
import pytest
import respx

import jobhunt.adapters.jobvite as jv
from jobhunt.models import CompanyConfig

FIX = Path(__file__).parent / "fixtures" / "jobvite"
URL = "https://jobs.jobvite.com/ex/jobs"
CO = CompanyConfig(name="Ex", ats="jobvite", slug="ex")


def run():
    return jv.JobviteAdapter(httpx.Client()).fetch(CO)


@respx.mock
def test_parse_departments_and_dedupe():
    respx.get(URL).mock(return_value=httpx.Response(200, text=(FIX / "board.html").read_text()))
    jobs = run()
    assert [j.external_id for j in jobs] == ["o111", "o222"]
    a, b = jobs
    assert a.title == "Marketing Intern" and a.department == "Marketing"
    assert a.location == "Vancouver, British Columbia"
    assert a.url == "https://jobs.jobvite.com/ex/job/o111" and a.source == "jobvite"
    assert b.title == "Engineer & Co-op" and b.department == "Engineering" and b.location == "Remote, Canada"


@respx.mock
def test_empty_board():
    respx.get(URL).mock(return_value=httpx.Response(200, text=(FIX / "empty.html").read_text()))
    assert run() == []


@respx.mock
def test_http_error_propagates():
    respx.get(URL).mock(return_value=httpx.Response(404))
    with pytest.raises(httpx.HTTPStatusError):
        run()


@respx.mock
def test_challenge_page_raises():
    respx.get(URL).mock(return_value=httpx.Response(200, text="<html><body>Checking your browser</body></html>"))
    with pytest.raises(ValueError, match="markup"):
        run()
