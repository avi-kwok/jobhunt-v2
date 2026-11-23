from pathlib import Path

import httpx
import pytest
import respx

import jobhunt.adapters.descartes as ds
from jobhunt.models import CompanyConfig

FIX = Path(__file__).parent / "fixtures" / "descartes"
BASE = "https://careers.descartes.com/join-our-team/"
CO = CompanyConfig(name="Descartes Systems", ats="descartes", host="careers.descartes.com")


def fx(n):
    return (FIX / n).read_text(encoding="utf-8")


def run():
    return ds.DescartesAdapter(httpx.Client()).fetch(CO)


@pytest.fixture(autouse=True)
def no_delay(monkeypatch):
    monkeypatch.setattr(ds, "DELAY", 0)


@respx.mock
def test_parse_pages_and_fields():
    respx.get(BASE).mock(return_value=httpx.Response(200, text=fx("page1.html")))
    respx.get(f"{BASE}page/2/").mock(return_value=httpx.Response(200, text=fx("page2.html")))
    jobs = {j.external_id: j for j in run()}
    assert sorted(jobs) == ["director-corporate-development", "sales-partnership-representative", "software-qa-analyst"]
    j = jobs["software-qa-analyst"]
    assert j.title == "Software QA Analyst" and j.source == "descartes"
    assert j.url == "https://careers.descartes.com/job/software-qa-analyst/"
    assert j.location == "Zilina, SK, 010 01"
    assert j.posted_at.year == 2026 and j.posted_at.month == 11 and j.posted_at.day == 22
    assert jobs["sales-partnership-representative"].title == "Sales & Partnership Representative"
    assert jobs["sales-partnership-representative"].location == "US"
    assert jobs["director-corporate-development"].location == "Waterloo, ON, CA, N2V 1C6"


@respx.mock
def test_single_page_stops_without_next_link():
    route = respx.get(BASE).mock(return_value=httpx.Response(200, text=fx("page2.html")))
    jobs = run()
    assert route.call_count == 1 and len(jobs) == 1


@respx.mock
def test_crawl_delay_between_pages(monkeypatch):
    sleeps = []
    monkeypatch.setattr(ds, "DELAY", 10.0)
    monkeypatch.setattr(ds.time, "sleep", lambda s: sleeps.append(s))
    respx.get(BASE).mock(return_value=httpx.Response(200, text=fx("page1.html")))
    respx.get(f"{BASE}page/2/").mock(return_value=httpx.Response(200, text=fx("page2.html")))
    run()
    assert sleeps == [10.0]


@respx.mock
def test_page_without_grid_raises():
    respx.get(BASE).mock(return_value=httpx.Response(200, text=fx("no_grid.html")))
    with pytest.raises(ValueError, match="no job grid"):
        run()


@respx.mock
def test_page_cap(monkeypatch):
    monkeypatch.setattr(ds, "MAX_PAGES", 2)
    route = respx.get(BASE).mock(return_value=httpx.Response(200, text=fx("page1.html")))
    respx.get(f"{BASE}page/2/").mock(return_value=httpx.Response(200, text=fx("page1.html")))
    run()
    assert route.call_count == 1
    assert respx.calls.call_count == 2


@respx.mock
def test_http_error_propagates():
    respx.get(BASE).mock(return_value=httpx.Response(403))
    with pytest.raises(httpx.HTTPStatusError):
        run()


def test_needs_host():
    with pytest.raises(ValueError):
        ds.DescartesAdapter(httpx.Client()).fetch(CompanyConfig(name="Descartes Systems", ats="descartes"))
