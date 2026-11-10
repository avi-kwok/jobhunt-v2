from pathlib import Path

import httpx
import pytest
import respx

from jobhunt.adapters import radancy
from jobhunt.adapters.radancy import RadancyAdapter
from jobhunt.models import CompanyConfig

FIX = Path(__file__).parent / "fixtures" / "radancy"
BASE = "https://jobs.example.com"
CO = CompanyConfig(name="Acme", ats="radancy", host="jobs.example.com")
SITEMAP = (FIX / "sitemap.xml").read_text()
JOB_HTML = (FIX / "job.html").read_text()
J1003 = BASE + "/job/mountain-view/summer-2027-software-engineer-intern/111/1003"
J1002 = BASE + "/job/toronto/software-engineer-co-op/111/1002"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(radancy, "DETAIL_DELAY", 0)


def run():
    return RadancyAdapter(client=httpx.Client()).fetch(CO)


@respx.mock
def test_filters_interns_dedupes_and_parses_details():
    respx.get(BASE + "/sitemap.xml").mock(return_value=httpx.Response(200, text=SITEMAP))
    respx.get(J1003).mock(return_value=httpx.Response(200, text=JOB_HTML))
    respx.get(J1002).mock(return_value=httpx.Response(200, text="<html></html>"))  # no JSON-LD
    jobs = run()
    # internal-audit (not intern) and staff-engineer dropped; 1002 deduped; newest id first
    assert [j.external_id for j in jobs] == ["1003", "1002"]
    a, b = jobs
    assert a.title == "Summer 2027: Software Engineer Intern"
    assert a.location == "Mountain View, California, United States"
    assert a.description == "Build things" and a.posted_at.year == 2026
    assert a.url == J1003 and a.uid == "radancy:Acme:1003"
    # slug fallback when the page has no JSON-LD
    assert b.title == "Software Engineer Co Op" and b.location == "Toronto"


@respx.mock
def test_detail_cap(monkeypatch):
    monkeypatch.setattr(radancy, "MAX_DETAILS", 1)
    respx.get(BASE + "/sitemap.xml").mock(return_value=httpx.Response(200, text=SITEMAP))
    d = respx.get(J1003).mock(return_value=httpx.Response(200, text=JOB_HTML))
    other = respx.get(J1002).mock(return_value=httpx.Response(200, text=JOB_HTML))
    jobs = run()
    assert d.call_count == 1 and other.call_count == 0
    assert len(jobs) == 2 and jobs[1].title == "Software Engineer Co Op"


@respx.mock
def test_challenge_stops_detail_fetches():
    respx.get(BASE + "/sitemap.xml").mock(return_value=httpx.Response(200, text=SITEMAP))
    d = respx.get(J1003).mock(return_value=httpx.Response(202, text=""))
    other = respx.get(J1002).mock(return_value=httpx.Response(200, text=JOB_HTML))
    jobs = run()
    assert d.call_count == 1 and other.call_count == 0
    assert [j.title for j in jobs] == ["Summer 2027 Software Engineer Intern", "Software Engineer Co Op"]


@respx.mock
def test_empty_board():
    respx.get(BASE + "/sitemap.xml").mock(return_value=httpx.Response(
        200, text='<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"></urlset>'))
    assert run() == []


@respx.mock
def test_http_error_propagates():
    respx.get(BASE + "/sitemap.xml").mock(return_value=httpx.Response(404))
    with pytest.raises(httpx.HTTPStatusError):
        run()


def test_requires_host():
    with pytest.raises(ValueError):
        RadancyAdapter(client=httpx.Client()).fetch(CompanyConfig(name="X", ats="radancy"))


@respx.mock
def test_sitemap_without_job_urls_raises():
    respx.get(BASE + "/sitemap.xml").mock(return_value=httpx.Response(
        200, text='<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://jobs.example.com/x</loc></url></urlset>'))
    with pytest.raises(ValueError, match="no /job/"):
        run()
