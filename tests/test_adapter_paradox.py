from pathlib import Path

import httpx
import pytest
import respx

import jobhunt.adapters.paradox as pdx
from jobhunt.models import CompanyConfig

FIX = Path(__file__).parent / "fixtures" / "paradox"
HOST = "careers.loblaw.ca"
INDEX = f"https://{HOST}/sitemap.xml"
JOBS = f"https://{HOST}/sitemap-31957fc0-en.xml"
CO = CompanyConfig(name="Ex", ats="paradox", host=HOST)


def fx(name):
    return (FIX / name).read_bytes()


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(pdx, "DETAIL_DELAY", 0)


def run():
    return pdx.ParadoxAdapter(httpx.Client()).fetch(CO)


def mock_site(detail):
    def handler(request):
        if request.url.path == "/sitemap.xml":
            return httpx.Response(200, content=fx("sitemap.xml"))
        if request.url.path == "/sitemap-31957fc0-en.xml":
            return httpx.Response(200, content=fx("jobs-sitemap.xml"))
        return detail(request)

    return respx.route(host=HOST).mock(side_effect=handler)


def detail_calls(route):
    return [c for c in route.calls if "/job/" in c.request.url.path]


def posting(req):
    return httpx.Response(200, content=fx("posting-pharmacy-intern.html"))


def not_found(req):
    return httpx.Response(200, content=fx("posting-no-jsonld.html"))


@respx.mock
def test_intern_postings_parsed_with_title_url_location_and_date():
    route = mock_site(posting)
    jobs = run()
    # "Interim ..." and "HMR Clerk" slugs are not internships; the three pharmacy-intern URLs are.
    assert [j.external_id for j in jobs] == ["R2000646938", "R2000735652", "R2000718832"]
    assert len(detail_calls(route)) == 3
    a = jobs[1]
    assert a.title == "Pharmacy Intern"
    assert a.location == "Barrie, ON, CA"
    assert a.url == f"https://{HOST}/pharmacy-intern/job/R2000735652"
    assert a.posted_at.year == 2026 and a.posted_at.month == 11 and a.posted_at.day == 20
    assert "Come make your difference" in a.description
    assert a.source == "paradox" and a.company == "Ex"


@respx.mock
def test_detail_without_jsonld_stops_details_and_falls_back_to_slug():
    route = mock_site(not_found)
    jobs = run()
    assert len(jobs) == 3
    assert len(detail_calls(route)) == 1  # first non-posting page ends the detail requests
    for j in jobs:
        assert j.title == "pharmacy intern"
        assert j.location is None and j.description is None and j.posted_at is None


@respx.mock
def test_detail_cap():
    route = mock_site(posting)
    pdx.MAX_DETAILS, saved = 1, pdx.MAX_DETAILS
    try:
        jobs = run()
    finally:
        pdx.MAX_DETAILS = saved
    assert len(detail_calls(route)) == 1
    assert jobs[0].location == "Barrie, ON, CA"
    assert jobs[1].location is None and jobs[2].location is None


@respx.mock
def test_missing_job_sitemap_raises():
    respx.get(INDEX).mock(return_value=httpx.Response(200, content=b"<sitemapindex xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'></sitemapindex>"))
    with pytest.raises(ValueError, match="no -en.xml job sitemap"):
        run()


@respx.mock
def test_sitemap_http_error_raises():
    respx.get(INDEX).mock(return_value=httpx.Response(500))
    with pytest.raises(httpx.HTTPStatusError):
        run()


def test_missing_host_raises():
    with pytest.raises(ValueError, match="paradox adapter needs host"):
        pdx.ParadoxAdapter(httpx.Client()).fetch(CompanyConfig(name="Ex", ats="paradox"))


def test_location_joins_multiple_places():
    data = {"jobLocation": [
        {"address": {"addressLocality": "Barrie", "addressRegion": "ON", "addressCountry": "CA"}},
        {"address": {"addressLocality": "Kingston", "addressRegion": "ON", "addressCountry": "CA"}},
        {"address": {}},
    ]}
    assert pdx._location(data) == "Barrie, ON, CA | Kingston, ON, CA"
    assert pdx._location({"jobLocation": []}) is None
