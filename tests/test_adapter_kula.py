from pathlib import Path

import httpx
import pytest
import respx

import jobhunt.adapters.kula as kula
from jobhunt.models import CompanyConfig

FIX = Path(__file__).parent / "fixtures" / "kula"
URL = "https://careers.kula.ai/example"
CO = CompanyConfig(name="Example", ats="kula", host="careers.kula.ai", slug="example")


def run(co=CO):
    return kula.KulaAdapter(httpx.Client()).fetch(co)


@respx.mock
def test_parse_board_fields_and_dedupe():
    respx.get(URL).mock(return_value=httpx.Response(200, text=(FIX / "board.html").read_text()))
    jobs = run()
    assert [j.external_id for j in jobs] == ["66607", "67080", "64382"]
    intern = jobs[0]
    assert intern.title == "Data Analytics Intern (Summer)"
    assert intern.url == "https://careers.kula.ai/example/66607-data-analytics-intern-summer"
    assert intern.location == "Toronto, Ontario, Canada"
    assert intern.department == "Co-op"
    assert intern.employment_type == "Internship • Hybrid"
    assert intern.source == "kula" and intern.company == "Example"
    assert jobs[1].department == "Electrical" and jobs[1].employment_type == "Full time • On-Site"


@respx.mock
def test_placeholder_bare_meta_line_has_no_department():
    respx.get(URL).mock(return_value=httpx.Response(200, text=(FIX / "placeholder.html").read_text()))
    (job,) = run()
    assert job.external_id == "34567"
    assert job.title == "Future Opportunities: Enterprise Account Manager"
    assert job.location == "Canada"
    assert job.department is None
    assert job.employment_type == "Full time • Remote"
    assert job.url == "https://careers.kula.ai/example/34567-future-opportunities-enterprise-account-manager"


@respx.mock
def test_query_suffix_is_dropped_from_url():
    html = (FIX / "placeholder.html").read_text().replace("34567-future-opportunities-enterprise-account-manager", "34567-future-opportunities-enterprise-account-manager?jobs=true")
    respx.get(URL).mock(return_value=httpx.Response(200, text=html))
    (job,) = run()
    assert job.url == "https://careers.kula.ai/example/34567-future-opportunities-enterprise-account-manager"


@respx.mock
def test_empty_board_with_container_is_empty_list():
    respx.get(URL).mock(return_value=httpx.Response(200, text=(FIX / "empty.html").read_text()))
    assert run() == []


@respx.mock
def test_missing_container_raises():
    respx.get(URL).mock(return_value=httpx.Response(200, text=(FIX / "challenge.html").read_text()))
    with pytest.raises(ValueError, match="container"):
        run()


@respx.mock
def test_http_error_propagates():
    respx.get(URL).mock(return_value=httpx.Response(404))
    with pytest.raises(httpx.HTTPStatusError):
        run()


@respx.mock
def test_one_get_per_fetch():
    route = respx.get(URL).mock(return_value=httpx.Response(200, text=(FIX / "empty.html").read_text()))
    run()
    assert route.call_count == 1


def test_needs_host_and_slug():
    with pytest.raises(ValueError):
        run(CompanyConfig(name="x", ats="kula", slug="example"))
    with pytest.raises(ValueError):
        run(CompanyConfig(name="x", ats="kula", host="careers.kula.ai"))
