import json
import re
from pathlib import Path

import httpx
import pytest
import respx

import jobhunt.adapters.deshaw as ds
from jobhunt.models import CompanyConfig

FIX = Path(__file__).parent / "fixtures" / "deshaw"
URL = "https://www.deshaw.com/careers"
CO = CompanyConfig(name="D. E. Shaw", ats="deshaw", host="www.deshaw.com")


def run():
    return ds.DEShawAdapter(httpx.Client()).fetch(CO)


@respx.mock
def test_parse_regular_and_internships_skip_internal():
    respx.get(URL).mock(return_value=httpx.Response(200, text=(FIX / "careers.html").read_text()))
    jobs = {j.external_id: j for j in run()}
    assert sorted(jobs) == ["5902", "6012", "6055", "6212", "6283"]
    assert "1" not in jobs
    j = jobs["6283"]
    assert j.title == "Cove MBA Associate- Summer 2027"
    assert j.url == "https://www.deshaw.com/careers/Cove-MBA-Associate-Summer-2027-6283"
    assert j.location == "New York" and j.department == "Investment Analysis"
    assert j.source == "deshaw" and j.company == "D. E. Shaw"
    assert jobs["6012"].location == "Singapore" and jobs["6012"].department == "Administration"


@respx.mock
def test_multiple_locations_joined():
    text = (FIX / "careers.html").read_text()
    lists = json.loads(re.search(r'__NEXT_DATA__"[^>]*>(.*?)</script>', text, re.S).group(1))
    lists["props"]["pageProps"]["regularJobs"][0]["data"]["jobMetadata"]["jobLocations"].append(
        {"name": "London", "abbreviation": "LDN"}
    )
    body = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(lists)}</script>'
    respx.get(URL).mock(return_value=httpx.Response(200, text=body))
    assert {j.external_id: j for j in run()}["6012"].location == "Singapore | London"


@respx.mock
def test_items_without_id_or_job_url_skipped():
    text = (FIX / "careers.html").read_text()
    lists = json.loads(re.search(r'__NEXT_DATA__"[^>]*>(.*?)</script>', text, re.S).group(1))
    regular = lists["props"]["pageProps"]["regularJobs"]
    del regular[0]["data"]["jobUrl"]
    del regular[1]["data"]["id"]
    body = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(lists)}</script>'
    respx.get(URL).mock(return_value=httpx.Response(200, text=body))
    jobs = {j.external_id: j for j in run()}
    assert sorted(jobs) == ["5902", "6055", "6283"]
    assert all(not j.url.endswith("/careers/None") for j in jobs.values())


@respx.mock
def test_only_careers_page_is_requested():
    route = respx.get(URL).mock(return_value=httpx.Response(200, text=(FIX / "careers.html").read_text()))
    run()
    assert route.call_count == 1


@respx.mock
def test_empty_lists():
    body = '<script id="__NEXT_DATA__" type="application/json">{"props":{"pageProps":{"regularJobs":[],"internships":[]}}}</script>'
    respx.get(URL).mock(return_value=httpx.Response(200, text=body))
    assert run() == []


@respx.mock
def test_missing_next_data_raises():
    respx.get(URL).mock(return_value=httpx.Response(200, text="<html>maintenance</html>"))
    with pytest.raises(ValueError, match="__NEXT_DATA__"):
        run()


@respx.mock
def test_missing_lists_raises():
    body = '<script id="__NEXT_DATA__" type="application/json">{"props":{"pageProps":{"other":1}}}</script>'
    respx.get(URL).mock(return_value=httpx.Response(200, text=body))
    with pytest.raises(ValueError, match="job lists"):
        run()


@respx.mock
def test_http_error_propagates():
    respx.get(URL).mock(return_value=httpx.Response(404))
    with pytest.raises(httpx.HTTPStatusError):
        run()


def test_needs_host():
    with pytest.raises(ValueError):
        ds.DEShawAdapter(httpx.Client()).fetch(CompanyConfig(name="x", ats="deshaw"))
