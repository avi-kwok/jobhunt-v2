import json
from pathlib import Path

import httpx
import pytest
import respx

from jobhunt.adapters import ibm
from jobhunt.adapters.ibm import IBMAdapter
from jobhunt.models import CompanyConfig

CO = CompanyConfig(name="IBM", ats="ibm")
FIX = json.loads((Path(__file__).parent / "fixtures/ibm/search_hits.json").read_text())


def hit(n, title="Data Intern"):
    return {"_id": f"id{n}", "_source": {
        "url": f"https://careers.ibm.com/careers/JobDetail?jobId={n}", "title": title}}


def run():
    return IBMAdapter(client=httpx.Client()).fetch(CO)


@respx.mock
def test_parse_and_union_dedupe():
    route = respx.post(ibm.SEARCH_URL).mock(return_value=httpx.Response(200, json=FIX))
    jobs = run()
    assert route.call_count == 2  # "intern" + "co-op", same fixture each
    assert [j.external_id for j in jobs] == ["140053"]  # non-intern dropped, deduped
    j = jobs[0]
    assert j.location == "Singapore, SG" and j.department == "Software Engineering"
    assert j.employment_type == "Internship" and j.description == "A career in IBM..."
    assert j.url.endswith("jobId=140053") and j.posted_at.year == 2026
    assert j.uid == "ibm:IBM:140053"


@respx.mock
def test_pagination_and_cap(monkeypatch):
    monkeypatch.setattr(ibm, "PAGE_SIZE", 2)
    monkeypatch.setattr(ibm, "MAX_PAGES", 3)
    monkeypatch.setattr(ibm, "QUERIES", ("intern",))
    def handler(req):
        frm = json.loads(req.content)["from"]
        return httpx.Response(200, json={"hits": {"hits": [hit(frm), hit(frm + 1)]}})
    route = respx.post(ibm.SEARCH_URL).mock(side_effect=handler)
    assert len(run()) == 6 and route.call_count == 3


@respx.mock
def test_empty_board():
    respx.post(ibm.SEARCH_URL).mock(return_value=httpx.Response(200, json={"hits": {"hits": []}}))
    assert run() == []


@respx.mock
def test_http_error_propagates():
    respx.post(ibm.SEARCH_URL).mock(return_value=httpx.Response(404))
    with pytest.raises(httpx.HTTPStatusError):
        run()
