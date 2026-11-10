"""iCIMS adapter against mocked responses (no network)."""

import json
from pathlib import Path

import httpx
import pytest
import respx

import jobhunt.adapters.icims as icims
from jobhunt.models import CompanyConfig

FIX = Path(__file__).parent / "fixtures" / "icims"


def _jibe(name="DocuSign", host="careers.docusign.com"):
    return CompanyConfig(name=name, ats="icims", host=host)


def _adapter():
    return icims.IcimsAdapter(client=httpx.Client())


@pytest.fixture(autouse=True)
def _no_delay(monkeypatch):
    monkeypatch.setattr(icims, "DEFAULT_DELAY", 0.0)
    monkeypatch.setattr(icims, "DELAYS", {})


def _page(name):
    return (FIX / name).read_text()


def test_registered():
    from jobhunt.adapters.base import get_adapter

    assert type(get_adapter("icims")).__name__ == "IcimsAdapter"


@respx.mock
def test_jibe_parses_and_dedupes_across_terms():
    route = respx.get("https://careers.docusign.com/api/jobs").mock(
        return_value=httpx.Response(200, json=json.loads(_page("jibe_page.json")))
    )
    jobs = _adapter().fetch(_jibe())
    assert route.call_count == len(icims.JIBE_TERMS)  # same rows each term -> deduped
    assert [j.external_id for j in jobs] == ["101", "102"]
    j = jobs[0]
    assert j.uid == "icims:DocuSign:101"
    assert j.url == "https://careers.docusign.com/jobs/101"
    assert j.location == "Seattle, Washington"
    assert jobs[1].location == "US-Seattle"
    assert j.department == "University" and j.employment_type == "INTERN"
    assert "Build things" in j.description
    assert j.posted_at.year == 2026 and j.posted_at.tzinfo is not None


@respx.mock
def test_jibe_paginates_and_caps(monkeypatch):
    monkeypatch.setattr(icims, "JIBE_LIMIT", 2)
    monkeypatch.setattr(icims, "JIBE_MAX_PAGES", 3)
    monkeypatch.setattr(icims, "JIBE_TERMS", ("intern",))
    pages = []

    def handler(request):
        p = int(request.url.params["page"])
        pages.append(p)
        rows = [{"data": {"slug": f"{p}-{i}", "title": "Intern"}} for i in range(2)]
        return httpx.Response(200, json={"jobs": rows, "totalCount": 99})

    respx.get("https://careers.x.com/api/jobs").mock(side_effect=handler)
    jobs = _adapter().fetch(_jibe("X", "careers.x.com"))
    assert pages == [1, 2, 3]  # hard page cap
    assert len(jobs) == 6


@respx.mock
def test_jibe_stops_at_total(monkeypatch):
    monkeypatch.setattr(icims, "JIBE_LIMIT", 2)
    monkeypatch.setattr(icims, "JIBE_TERMS", ("intern",))
    route = respx.get("https://careers.x.com/api/jobs").mock(
        return_value=httpx.Response(200, json={"jobs": [{"data": {"slug": "a", "title": "I"}}] * 2, "totalCount": 2})
    )
    _adapter().fetch(_jibe("X", "careers.x.com"))
    assert route.call_count == 1


@respx.mock
def test_jibe_empty_board():
    respx.get("https://www.github.careers/api/jobs").mock(
        return_value=httpx.Response(200, json={"jobs": [], "totalCount": 0})
    )
    assert _adapter().fetch(_jibe("GitHub", "www.github.careers")) == []


@respx.mock
def test_jibe_http_error_propagates():
    respx.get("https://careers.docusign.com/api/jobs").mock(return_value=httpx.Response(404))
    with pytest.raises(httpx.HTTPStatusError):
        _adapter().fetch(_jibe())


@respx.mock
def test_jibe_blocked_403_propagates():
    respx.get("https://careers.docusign.com/api/jobs").mock(return_value=httpx.Response(403))
    with pytest.raises(httpx.HTTPStatusError):
        _adapter().fetch(_jibe())


def test_host_required():
    with pytest.raises(ValueError):
        _adapter().fetch(CompanyConfig(name="X", ats="icims"))


@respx.mock
def test_portal_parses_and_dedupes_across_portals():
    html = _page("portal_page.html").replace("p.icims.com", "a.icims.com")
    ra = respx.get("https://a.icims.com/jobs/search").mock(return_value=httpx.Response(200, text=html))
    rb = respx.get("https://b.icims.com/jobs/search").mock(return_value=httpx.Response(200, text=html))
    co = CompanyConfig(name="MSCI", ats="icims", host="a.icims.com", slug="b.icims.com")
    jobs = _adapter().fetch(co)
    assert ra.call_count == rb.call_count == len(icims.PORTAL_TERMS)
    assert ra.calls[0].request.url.params["searchKeyword"] == "intern"
    assert [j.external_id for j in jobs] == ["6071", "6072"]
    j = jobs[0]
    assert j.title == "Intern – Data Transformation"
    assert j.url == "https://a.icims.com/jobs/6071/slug/job"  # no ?in_iframe
    assert j.location == "US-NY-New York"
    assert j.department == "Data and Operations" and j.employment_type == "Part-Time"
    assert (j.posted_at.month, j.posted_at.day, j.posted_at.hour) == (11, 7, 15)


@respx.mock
def test_portal_paginates_with_cap(monkeypatch):
    monkeypatch.setattr(icims, "PORTAL_PAGE", 2)
    monkeypatch.setattr(icims, "PORTAL_MAX_PAGES", 2)
    monkeypatch.setattr(icims, "PORTAL_TERMS", ("intern",))
    html = _page("portal_page.html").replace("Page 1 of 1", "Page 1 of 9")
    route = respx.get("https://a.icims.com/jobs/search").mock(return_value=httpx.Response(200, text=html))
    _adapter().fetch(CompanyConfig(name="K", ats="icims", host="a.icims.com"))
    assert [c.request.url.params["pr"] for c in route.calls] == ["0", "1"]


@respx.mock
def test_portal_empty_and_error():
    respx.get("https://a.icims.com/jobs/search").mock(
        return_value=httpx.Response(200, text=_page("portal_empty.html"))
    )
    co = CompanyConfig(name="K", ats="icims", host="a.icims.com")
    assert _adapter().fetch(co) == []
    respx.get("https://a.icims.com/jobs/search").mock(return_value=httpx.Response(403))
    with pytest.raises(httpx.HTTPStatusError):
        _adapter().fetch(co)


@respx.mock
def test_atlassian_filters_and_dedupes():
    respx.get(icims.ATLASSIAN_URL).mock(
        return_value=httpx.Response(200, json=json.loads(_page("atlassian.json")))
    )
    jobs = _adapter().fetch(CompanyConfig(name="Atlassian", ats="icims", host="www.atlassian.com"))
    assert [j.external_id for j in jobs] == ["1"]
    j = jobs[0]
    assert j.url == "https://www.atlassian.com/company/careers/details/1"
    assert j.location == "Seattle - United States - Seattle, Washington"
    assert "Do stuff" in j.description and j.department == "Interns"


@respx.mock
def test_atlassian_error_propagates():
    respx.get(icims.ATLASSIAN_URL).mock(return_value=httpx.Response(404))
    with pytest.raises(httpx.HTTPStatusError):
        _adapter().fetch(CompanyConfig(name="Atlassian", ats="icims", host="www.atlassian.com"))


@respx.mock
def test_jibe_payload_without_jobs_key_raises():
    respx.get("https://www.github.careers/api/jobs").mock(
        return_value=httpx.Response(200, json={"message": "blocked"})
    )
    with pytest.raises(ValueError, match="Jibe"):
        _adapter().fetch(_jibe("GitHub", "www.github.careers"))


@respx.mock
def test_portal_challenge_page_raises():
    respx.get("https://a.icims.com/jobs/search").mock(
        return_value=httpx.Response(200, text="<html><body>Access denied</body></html>")
    )
    with pytest.raises(ValueError, match="portal"):
        _adapter().fetch(CompanyConfig(name="K", ats="icims", host="a.icims.com"))
