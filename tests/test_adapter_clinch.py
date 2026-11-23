from pathlib import Path

import httpx
import pytest
import respx

import jobhunt.adapters.clinch as cl
from jobhunt.models import CompanyConfig

FIX = Path(__file__).parent / "fixtures" / "clinch"
HOST = "careers.example.com"
URL = f"https://{HOST}/jobs/search"
CO = CompanyConfig(name="Ex", ats="clinch", host=HOST)


@pytest.fixture(autouse=True)
def no_delay(monkeypatch):
    monkeypatch.setattr(cl, "DELAY", 0)


def page(name):
    return httpx.Response(200, text=(FIX / name).read_text(encoding="utf-8"))


def run():
    return cl.ClinchAdapter(httpx.Client()).fetch(CO)


@respx.mock
def test_card_layout_fields_and_multi_location():
    respx.get(URL, params={"page": "1"}).mock(return_value=page("page1.html"))
    respx.get(URL, params={"page": "2"}).mock(return_value=page("empty.html"))
    jobs = {j.external_id: j for j in run()}
    j = jobs["senior-analyst-global-mobility-immigration-brooklyn-new-york-united-states"]
    assert j.title == "Senior Analyst, Global Mobility & Immigration"
    assert j.url.startswith("https://careers.etsy.com/jobs/")
    assert j.location == "Brooklyn, New York, United States | Remote, United States (Pacific)"
    assert j.department == "HR Operations" and j.employment_type == "Full-time"
    assert j.description.startswith("Company Description") and "Etsy is the global marketplace" in j.description
    assert j.source == "clinch" and j.company == "Ex"


@respx.mock
def test_table_layout_and_pagination_stops_without_next():
    respx.get(URL, params={"page": "1"}).mock(return_value=page("page1.html"))
    respx.get(URL, params={"page": "2"}).mock(return_value=page("page2.html"))
    route3 = respx.get(URL, params={"page": "3"}).mock(return_value=page("empty.html"))
    jobs = run()
    assert not route3.called
    row = jobs[[x.external_id for x in jobs].index("senior-product-designer-austin-texas-united-states-0e5b760d-1877-48d6-a6b3-6b26d016f23a")]
    assert row.title == "Senior Product Designer" and row.location == "Austin, Texas"
    assert row.department is None and row.description is None
    assert len(jobs) == 4 and len({j.external_id for j in jobs}) == 4


@respx.mock
def test_empty_page_returns_nothing_and_sends_only_page_param():
    route = respx.get(URL).mock(return_value=page("empty.html"))
    assert run() == []
    assert route.call_count == 1 and set(route.calls.last.request.url.params) == {"page"}


@respx.mock
def test_blank_page_without_listing_container_raises():
    respx.get(URL).mock(return_value=httpx.Response(202, text=""))
    with pytest.raises(ValueError, match="no job list container"):
        run()


@respx.mock
def test_page_cap_and_dedupe(monkeypatch):
    monkeypatch.setattr(cl, "MAX_PAGES", 2)
    route = respx.get(URL).mock(return_value=page("page1.html"))
    jobs = run()
    assert route.call_count == 2
    assert len(jobs) == 2


def test_missing_host_raises():
    with pytest.raises(ValueError, match="clinch adapter needs a host"):
        cl.ClinchAdapter(httpx.Client()).fetch(CompanyConfig(name="Ex", ats="clinch"))


@respx.mock
def test_http_error_raises():
    respx.get(URL).mock(return_value=httpx.Response(404))
    with pytest.raises(httpx.HTTPStatusError):
        run()
