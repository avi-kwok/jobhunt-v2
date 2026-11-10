import copy
import json
from pathlib import Path

import httpx
import pytest
import respx

from jobhunt.adapters.digitalrecruiters import (
    API, LIMIT, MAX_DETAILS, MAX_LIST_PAGES, SEARCH_PAGES, SEARCH_TERMS,
    DigitalRecruitersAdapter, api_locale,
)
from jobhunt.models import CompanyConfig

FIX = Path(__file__).parent / "fixtures" / "digitalrecruiters"
LIST = json.loads((FIX / "job_ads.json").read_text())
DETAIL = json.loads((FIX / "job_ad_detail.json").read_text())
CO = CompanyConfig(name="Moment Factory", ats="digitalrecruiters", slug="jobs.momentfactory.com",
                   site="en", host="api.digitalrecruiters.com")


def item(i, title="Producer"):
    it = copy.deepcopy(LIST["items"][0])
    it.update(id=f"{i}-97077009", job_ad_id=i, title=title, url=f"{i}-slug")
    return it


def page(items, count=None):
    return {"count": len(items) if count is None else count, "items": items, "filters": {}}


def run():
    return DigitalRecruitersAdapter(client=httpx.Client()).fetch(CO)


def body(req):
    return json.loads(req.content or b"{}")


def test_locale_mapping():
    assert api_locale("en") == "en_GB"
    assert api_locale("fr") == "fr_FR"
    assert api_locale("fr_CA") == "fr_CA"
    assert api_locale("fr-CA") == "fr_CA"


@respx.mock
def test_parse_real_shape():
    route = respx.post(API).mock(return_value=httpx.Response(200, json=LIST))
    jobs = run()
    assert route.call_count == 1
    q = route.calls[0].request.url.params
    assert q["domainName"] == "jobs.momentfactory.com" and q["locale"] == "en_GB"
    assert q["page"] == "1" and q["limit"] == str(LIMIT)
    assert len(jobs) == 3
    j = jobs[0]
    assert j.external_id == "4686577-97077009"
    assert j.title == "Architectural project manager (freelance)"
    assert j.url == ("https://jobs.momentfactory.com/en/annonce/"
                     "4686577-architectural-project-manager-freelance-h2v-4h8-montreal")
    assert j.location == "Montréal" and j.employment_type == "Permanent contract"
    assert j.description is None and j.posted_at is None  # no detail for non-intern titles
    assert j.uid == "digitalrecruiters:Moment Factory:4686577-97077009"


@respx.mock
def test_intern_title_gets_detail_and_cap():
    items = [item(i, "Software Developer Intern") for i in range(1, MAX_DETAILS + 3)]
    respx.post(API).mock(return_value=httpx.Response(200, json=page(items)))
    det = respx.get(url__startswith=API + "/").mock(return_value=httpx.Response(200, json=DETAIL))
    jobs = run()
    assert det.call_count == MAX_DETAILS
    first = det.calls[0].request.url
    assert first.path.endswith("/job-ads/1") and first.params["addressId"] == "97077009"
    j = jobs[0]
    assert j.description and "QUELQUES MOTS" in j.description
    assert j.posted_at.isoformat() == "2026-11-09T19:11:59+00:00"
    assert j.location == DETAIL["formatted_address"]
    assert jobs[-1].description is None


@respx.mock
def test_detail_404_keeps_list_data():
    respx.post(API).mock(return_value=httpx.Response(200, json=page([item(1, "Stagiaire - Développement")])))
    respx.get(url__startswith=API + "/").mock(return_value=httpx.Response(404, json={"error": {}}))
    j = run()[0]
    assert j.title == "Stagiaire - Développement" and j.description is None


@respx.mock
def test_full_list_pagination_and_cap():
    full = [item(i) for i in range(LIMIT)]
    route = respx.post(API).mock(return_value=httpx.Response(
        200, json=page(full, count=LIMIT * MAX_LIST_PAGES)))
    jobs = run()
    assert route.call_count == MAX_LIST_PAGES  # never asks past the cap
    assert [c.request.url.params["page"] for c in route.calls] == ["1", "2", "3", "4"]
    assert len(jobs) == LIMIT  # same ids every page -> deduped


@respx.mock
def test_short_page_stops():
    route = respx.post(API).mock(side_effect=[
        httpx.Response(200, json=page([item(i) for i in range(LIMIT)], count=LIMIT + 1)),
        httpx.Response(200, json=page([item(999)], count=LIMIT + 1)),
    ])
    assert len(run()) == LIMIT + 1
    assert route.call_count == 2


@respx.mock
def test_big_board_uses_search_union_and_dedupes():
    big = LIMIT * MAX_LIST_PAGES + 1
    first = page([item(1), item(2)], count=big)

    def handler(request):
        q = body(request).get("q")
        if q is None:
            return httpx.Response(200, json=first)
        if q == "intern":
            return httpx.Response(200, json=page([item(2), item(3, "Intern, Software")]))
        if q == "stage":
            return httpx.Response(200, json=page([item(4, "Stage - Développeur"), item(3)]))
        return httpx.Response(200, json=page([]))

    route = respx.post(API).mock(side_effect=handler)
    respx.get(url__startswith=API + "/").mock(return_value=httpx.Response(200, json=DETAIL))
    jobs = run()
    assert sorted(j.external_id for j in jobs) == [f"{i}-97077009" for i in (1, 2, 3, 4)]
    assert [body(c.request).get("q") for c in route.calls] == [None, *SEARCH_TERMS]


@respx.mock
def test_search_page_cap():
    full = [item(i) for i in range(LIMIT)]
    route = respx.post(API).mock(return_value=httpx.Response(
        200, json=page(full, count=LIMIT * MAX_LIST_PAGES + 1)))
    run()
    assert route.call_count == 1 + len(SEARCH_TERMS) * SEARCH_PAGES


@respx.mock
def test_empty_board():
    respx.post(API).mock(return_value=httpx.Response(200, json=page([])))
    assert run() == []


@respx.mock
def test_redesign_or_challenge_raises():
    respx.post(API).mock(return_value=httpx.Response(200, text="<html>challenge</html>"))
    with pytest.raises(ValueError):
        run()
    respx.post(API).mock(return_value=httpx.Response(200, json={"count": 0, "results": []}))
    with pytest.raises(ValueError):
        run()


@respx.mock
def test_http_error_propagates():
    respx.post(API).mock(return_value=httpx.Response(
        400, json={"error": {"code": 400, "message": "This locale isn't supported"}}))
    with pytest.raises(httpx.HTTPStatusError):
        run()


def test_missing_slug_raises():
    with pytest.raises(ValueError):
        DigitalRecruitersAdapter(client=httpx.Client()).fetch(
            CompanyConfig(name="X", ats="digitalrecruiters"))


@respx.mock
def test_detail_transient_failure_keeps_list_data_and_stops_details(monkeypatch):
    monkeypatch.setattr("jobhunt.adapters.base.time.sleep", lambda s: None)
    items = [item(i, "Software Developer Intern") for i in range(1, 4)]
    respx.post(API).mock(return_value=httpx.Response(200, json=page(items)))
    det = respx.get(url__startswith=API + "/").mock(return_value=httpx.Response(503))
    jobs = run()
    assert len(jobs) == 3 and all(j.description is None for j in jobs)
    assert det.call_count == 3  # one detail's retries, then no more detail calls
