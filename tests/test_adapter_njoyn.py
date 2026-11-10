from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from jobhunt.adapters import njoyn
from jobhunt.adapters.njoyn import NjoynAdapter
from jobhunt.models import CompanyConfig

FIX = Path(__file__).parent / "fixtures" / "njoyn"
URL = "https://cgi.njoyn.com/CORP/xweb/xweb.asp?NTKN=c&clid=21001&Page=JobListing&lang=1"
CODES = "APP,CCFT,CCPT,INT,TBD,TRD,TRN,VIE,WS,DSPB,DSPM,TSBM,CDAP,CDPR,"
CO = CompanyConfig(name="CGI", ats="njoyn", host="cgi.njoyn.com", slug="CORP",
                   site="21001", tenant=CODES)


def page(name):
    return (FIX / name).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def no_delay(monkeypatch):
    monkeypatch.setattr(njoyn, "REQUEST_DELAY", 0)


def form(request):
    return {k: v[0] for k, v in parse_qs(request.content.decode(), keep_blank_values=True).items()}


def router(jobtype_pages, keyword_page="empty.html"):
    """Serve fixture pages: job-type search by `pn`, keyword searches one page each."""
    calls = []

    def handler(request):
        f = form(request)
        calls.append(f)
        if f["Inp_Jobtype"]:
            return httpx.Response(200, text=page(jobtype_pages[int(f["pn"]) - 1]))
        return httpx.Response(200, text=page(keyword_page))

    respx.post(URL).mock(side_effect=handler)
    return calls


def run(co=CO):
    return NjoynAdapter(client=httpx.Client()).fetch(co)


@respx.mock
def test_parse_paginate_union_and_dedupe():
    calls = router(["listing_p1.html", "listing_p2.html"], "keyword.html")
    jobs = run()
    # 2 job-type pages + one request per keyword
    assert len(calls) == 2 + len(njoyn.KEYWORDS)
    assert [c["pn"] for c in calls[:2]] == ["1", "2"]
    assert calls[0]["Inp_Jobtype"] == CODES and calls[0]["searchFilled"] == "1"
    assert {c["Inp_Keywords"] for c in calls[2:]} == set(njoyn.KEYWORDS)
    ids = [j.external_id for j in jobs]
    assert len(ids) == len(set(ids)) == 4 + 2 + 2  # J1026-2812 appears in both searches
    assert "J0926-2370" in ids  # keyword-only intern
    j = next(j for j in jobs if j.external_id == "J1026-2812")
    assert j.title == "Winter 2027 : AI Developer Co-op (8 months)"
    assert j.location == "Toronto, Canada"
    assert j.department == "Software Development / Engineering"
    assert j.url == ("https://cgi.njoyn.com/CORP/xweb/xweb.asp?NTKN=c&clid=21001"
                     "&Page=JobDetails&Jobid=J1026-2812&BRID=1345069&lang=1")
    assert j.uid == "njoyn:CGI:J1026-2812" and j.posted_at is None
    multi = next(j for j in jobs if j.external_id == "J1026-1263")
    assert multi.location == "Lyon, Grenoble, Rennes, France"


@respx.mock
def test_no_tenant_skips_jobtype_search():
    calls = router([], "keyword.html")
    jobs = run(CO.model_copy(update={"tenant": None}))
    assert len(calls) == len(njoyn.KEYWORDS)
    assert all(c["Inp_Jobtype"] == "" for c in calls)
    assert len(jobs) == 3


@respx.mock
def test_pagination_cap(monkeypatch):
    monkeypatch.setattr(njoyn, "KEYWORDS", ())
    big = page("listing_p1.html").replace("Page 1 of 2", "Page 1 of 99")
    route = respx.post(URL).mock(return_value=httpx.Response(200, text=big))
    run()
    assert route.call_count == njoyn.MAX_PAGES


@respx.mock
def test_empty_board():
    calls = router(["empty.html"])
    assert run() == []
    assert len(calls) == 1 + len(njoyn.KEYWORDS)


@respx.mock
def test_redesign_or_challenge_raises():
    respx.post(URL).mock(return_value=httpx.Response(200, text="<html>Just a moment...</html>"))
    with pytest.raises(ValueError):
        run()
    # form still there but rows no longer parse
    broken = page("listing_p1.html").replace("Page=JobDetails", "Page=Other")
    respx.post(URL).mock(return_value=httpx.Response(200, text=broken))
    with pytest.raises(ValueError):
        run()


def test_missing_config_raises():
    with pytest.raises(ValueError):
        run(CO.model_copy(update={"site": None}))


@respx.mock
def test_http_error_propagates():
    respx.post(URL).mock(return_value=httpx.Response(404))
    with pytest.raises(httpx.HTTPStatusError):
        run()
