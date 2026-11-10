import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from jobhunt.adapters import balyasny
from jobhunt.adapters.balyasny import BalyasnyAdapter
from jobhunt.models import CompanyConfig

FIX = json.loads((Path(__file__).parent / "fixtures" / "balyasny" / "search.json").read_text())
URL = "https://bambusdev.my.site.com/s/sfsites/aura"
CO = CompanyConfig(name="Balyasny", ats="balyasny", host="bambusdev.my.site.com",
                   site="BAM Website")


@pytest.fixture(autouse=True)
def _no_delay(monkeypatch):
    monkeypatch.setattr(balyasny, "REQUEST_DELAY", 0)


def payload(records):
    d = json.loads(json.dumps(FIX))
    d["actions"][0]["returnValue"]["returnValue"] = records
    return d


def apex_params(request):
    form = parse_qs(request.content.decode())
    msg = json.loads(form["message"][0])
    return msg["actions"][0]["params"]


def run():
    return BalyasnyAdapter(client=httpx.Client()).fetch(CO)


@respx.mock
def test_parse_fixture():
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=FIX))
    jobs = run()
    assert route.call_count == 2
    p = apex_params(route.calls[0].request)
    assert p["classname"] == "BamJobRequisitionInfoDataService"
    assert p["method"] == "searchJobRequisitions"
    assert p["params"]["site"] == "BAM Website"
    assert p["params"]["experienceLevelFilter"] == ["Internships"]
    assert apex_params(route.calls[1].request)["params"]["searchKey"] == "intern"
    assert len(jobs) == 4  # both calls return the same 4 records -> deduped
    j = next(j for j in jobs if j.title == "Software Engineering (Summer Internship)")
    assert j.external_id == "a0DUN00000FaQhh2AF"
    assert j.url == ("https://bambusdev.my.site.com/s/details?"
                     "jobReq=Software-Engineering--Summer-Internship-_REQ8446")
    assert j.location == "Hong Kong; Singapore"
    assert j.department == "Technology"
    assert j.employment_type == "Internships"
    assert j.posted_note.startswith("Posted")
    assert j.posted_at is None and j.description is None
    assert j.uid == "balyasny:Balyasny:a0DUN00000FaQhh2AF"


@respx.mock
def test_search_union_and_dedupe():
    recs = FIX["actions"][0]["returnValue"]["returnValue"]
    route = respx.post(URL).mock(side_effect=[
        httpx.Response(200, json=payload([recs[0], recs[1], recs[0]])),
        httpx.Response(200, json=payload([recs[1], recs[2]])),
    ])
    jobs = run()
    assert route.call_count == 2
    assert [j.external_id for j in jobs] == [recs[0]["Id"], recs[1]["Id"], recs[2]["Id"]]


@respx.mock
def test_result_cap(monkeypatch):
    monkeypatch.setattr(balyasny, "MAX_RESULTS", 2)
    respx.post(URL).mock(return_value=httpx.Response(200, json=FIX))
    assert len(run()) == 2


@respx.mock
def test_missing_location_and_title_fallback():
    rec = dict(FIX["actions"][0]["returnValue"]["returnValue"][0])
    rec.update(Publish_Title__c=None, Job_Requisition_Positions__r=None)
    respx.post(URL).mock(return_value=httpx.Response(200, json=payload([rec])))
    j = run()[0]
    assert j.title == rec["Name"] and j.location is None


@respx.mock
def test_empty_board():
    respx.post(URL).mock(return_value=httpx.Response(200, json=payload([])))
    assert run() == []


@respx.mock
def test_redesign_or_challenge_raises():
    respx.post(URL).mock(return_value=httpx.Response(200, text="<html>challenge</html>"))
    with pytest.raises(ValueError):
        run()
    respx.post(URL).mock(return_value=httpx.Response(200, json={"actions": []}))
    with pytest.raises(ValueError):
        run()
    err = {"actions": [{"id": "1;a", "state": "ERROR", "returnValue": None,
                        "error": [{"message": "No such Apex class"}]}]}
    respx.post(URL).mock(return_value=httpx.Response(200, json=err))
    with pytest.raises(ValueError, match="ERROR"):
        run()
    respx.post(URL).mock(return_value=httpx.Response(200, json=payload(None)))
    with pytest.raises(ValueError):
        run()


@respx.mock
def test_http_error_propagates():
    respx.post(URL).mock(return_value=httpx.Response(404))
    with pytest.raises(httpx.HTTPStatusError):
        run()
