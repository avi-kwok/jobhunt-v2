import json
from pathlib import Path

import httpx
import pytest
import respx

import jobhunt.adapters.pinpoint as pp
from jobhunt.models import CompanyConfig

FIX = Path(__file__).parent / "fixtures" / "pinpoint"
URL = "https://ex.pinpointhq.com/postings.json"
CO = CompanyConfig(name="Ex", ats="pinpoint", slug="ex")


def run():
    return pp.PinpointAdapter(httpx.Client()).fetch(CO)


@respx.mock
def test_parse_dedupe():
    respx.get(URL).mock(return_value=httpx.Response(200, json=json.loads((FIX / "postings.json").read_text())))
    jobs = run()
    assert [j.external_id for j in jobs] == ["10", "11"]
    j = jobs[0]
    assert j.title == "Cloud Co-op" and j.location == "Toronto" and j.department == "Eng"
    assert j.url == "https://careers.ex.com/en/postings/x" and j.employment_type == "Full Time"
    assert j.description.startswith("Desc") and "Do" in j.description
    assert jobs[1].description is None and jobs[1].location is None


@respx.mock
def test_empty_board():
    respx.get(URL).mock(return_value=httpx.Response(200, json={"data": []}))
    assert run() == []


@respx.mock
def test_http_error_propagates():
    respx.get(URL).mock(return_value=httpx.Response(404))
    with pytest.raises(httpx.HTTPStatusError):
        run()
