from pathlib import Path

import httpx
import pytest
import respx

import jobhunt.adapters.successfactors_sitemap as sfs
from jobhunt.models import CompanyConfig

FIX = Path(__file__).parent / "fixtures" / "successfactors_sitemap"
HOST = "jobs.example.com"
SITEMAP = f"https://{HOST}/sitemap.xml"
CO = CompanyConfig(name="Ex", ats="successfactors_sitemap", host=HOST)


def fx(name):
    return (FIX / name).read_bytes()


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(sfs, "DETAIL_DELAY", 0)


def run():
    return sfs.SuccessFactorsSitemapAdapter(httpx.Client()).fetch(CO)


def mock_site(detail=lambda req: httpx.Response(200, content=fx("detail.html")), sitemap="urlset.xml"):
    def handler(request):
        if request.url.path == "/sitemap.xml":
            return httpx.Response(200, content=fx(sitemap))
        return detail(request)

    return respx.route(host=HOST).mock(side_effect=handler)


def detail_calls(route):
    return [c for c in route.calls if c.request.url.path != "/sitemap.xml"]


@respx.mock
def test_urlset_filters_dedupes_orders_and_parses_details():
    def detail(req):
        if req.url.path.endswith("/1447752633/"):
            return httpx.Response(200, content=fx("detail.html"))
        return httpx.Response(200, text="<html><title>X</title></html>")  # not a posting page

    route = mock_site(detail)
    sfs.MAX_DETAILS, saved = 20, sfs.MAX_DETAILS
    try:
        jobs = run()
    finally:
        sfs.MAX_DETAILS = saved
    # English terms first, newest id first; French stagiaire last; "Interne"/SkillBridge dropped;
    # the duplicate Wichita URL is deduped.
    assert [j.external_id for j in jobs] == ["1447752633", "1446732233", "1440000001", "1449999999"]
    a = jobs[0]
    assert a.title == "Intern, Data Analyst (Winter 2027)"
    assert a.location == "St Laurent, Québ H4R 1K2, CA"
    assert a.posted_at.year == 2026 and a.posted_at.month == 10 and a.posted_at.day == 14
    assert "Build data pipelines in Python & SQL." in a.description and "Dashboards" in a.description
    assert a.url.startswith(f"https://{HOST}/job/St-Laurent-Intern%2C-Data-Analyst")
    assert a.source == "successfactors_sitemap" and a.company == "Ex"
    # second detail was not a posting page -> details stop, slug fallback for the rest
    assert len(detail_calls(route)) == 2
    b = jobs[1]
    assert b.title == "Wichita Project Integrator Intern" and b.location == "KS 67209"
    assert b.description is None and b.posted_at is None
    assert jobs[3].title == "Dorval Stagiaire, Qualité Développeur Web (Hiver 2027)"
    assert jobs[3].location == "Québ H4S 1Y9, CA"


@respx.mock
def test_detail_cap():
    route = mock_site()
    sfs.MAX_DETAILS, saved = 2, sfs.MAX_DETAILS
    try:
        jobs = run()
    finally:
        sfs.MAX_DETAILS = saved
    assert len(jobs) == 4
    assert len(detail_calls(route)) == 2
    assert sum(1 for j in jobs if j.description) == 2


@respx.mock
def test_detail_block_stops_details_without_failing():
    route = mock_site(lambda req: httpx.Response(403))
    jobs = run()
    assert len(jobs) == 4 and all(j.description is None for j in jobs)
    assert len(detail_calls(route)) == 1


@respx.mock
def test_rss_filters_strips_location_and_dedupes():
    route = mock_site(sitemap="rss.xml")
    jobs = run()
    assert len(route.calls) == 1  # everything comes from the feed
    assert [j.external_id for j in jobs] == ["1417207300", "1417207500"]
    a, b = jobs
    assert a.title == "Software Engineering Co-op Student (Winter 2027)"
    assert a.location == "Toronto, ON, CA, M5H 0A9"
    assert a.url == "https://careers.example.com/job/Toronto%2C-ON-Software-Engineering-Co-op-Student-ON-M5H-0A9/1417207300/"
    assert "Build internal tools in Python & Go." in a.description
    assert b.title == "Stagiaire, Développeur logiciel" and b.location == "Montréal, QC, CA, H3B 4T9"


@pytest.mark.parametrize("name", ["empty_rss.xml", "empty_urlset.xml"])
@respx.mock
def test_empty_board(name):
    mock_site(sitemap=name)
    assert run() == []


@respx.mock
def test_challenge_page_raises():
    respx.get(SITEMAP).mock(return_value=httpx.Response(200, text="<html><body>Access denied</body></html>"))
    with pytest.raises(ValueError):
        run()


@respx.mock
def test_unexpected_root_raises():
    respx.get(SITEMAP).mock(return_value=httpx.Response(200, text="<sitemapindex/>"))
    with pytest.raises(ValueError, match="unexpected sitemap root"):
        run()


@respx.mock
def test_urlset_without_job_urls_raises():
    xml = '<urlset xmlns="http://www.google.com/schemas/sitemap/0.9"><url><loc>https://jobs.example.com/about</loc></url></urlset>'
    respx.get(SITEMAP).mock(return_value=httpx.Response(200, text=xml))
    with pytest.raises(ValueError, match="no /job/ postings"):
        run()


@respx.mock
def test_http_error_propagates():
    respx.get(SITEMAP).mock(return_value=httpx.Response(403))
    with pytest.raises(httpx.HTTPStatusError):
        run()


def test_needs_host():
    with pytest.raises(ValueError):
        sfs.SuccessFactorsSitemapAdapter(httpx.Client()).fetch(CompanyConfig(name="x"))


@pytest.mark.parametrize("title,hit", [
    ("Intern, Data Analyst", True), ("InternCo-op (Data Analyst)", True),
    ("Co-op Student", True), ("Stagiaire, TI", True), ("Étudiante en génie", True),
    ("Internal Audit", False), ("Logistique Interne", False), ("Cooperative Lead", False),
    ("International Tax", False),
])
def test_intern_re(title, hit):
    assert bool(sfs.INTERN_RE.search(title)) is hit


@respx.mock
def test_urlset_slug_fallback_strips_shared_city_and_bare_region(monkeypatch):
    """Seagate-style slugs: city prefix (>=3 postings), optional bare state, no postal."""
    monkeypatch.setattr(sfs, "MAX_DETAILS", 0)
    respx.get("https://seagatecareers.com/sitemap.xml").mock(
        return_value=httpx.Response(200, content=fx("seagate_urlset.xml")))
    co = CompanyConfig(name="Seagate", ats="successfactors_sitemap", host="seagatecareers.com")
    by = {j.external_id: j for j in sfs.SuccessFactorsSitemapAdapter(httpx.Client()).fetch(co)}
    assert sorted(by) == ["1426836700", "1436202100", "1442617900", "1443137100",
                          "1444409000", "1444957300", "1448606700"]  # interns only, deduped
    j = by["1448606700"]
    assert j.title == "Recording Head Characterization & Data Analytics Intern Summer 2027"
    assert j.location == "Bloomington, MN"
    assert by["1443137100"].title == "Lithography Summer Internship (PhD)"
    assert (by["1444409000"].title, by["1444409000"].location) == ("Data Scientist Intern", "Nakhon Ratchasima")
    assert (by["1436202100"].title, by["1436202100"].location) == ("IT Communications Intern Summer 2027", "Fremont, CA 94536")
    # cities seen on <3 postings stay in the title rather than risk eating a title word
    assert by["1444957300"].title == "Taipei Edge IoT Engineering Intern, Electronics Design Engineering"
    assert by["1426836700"].location is None


def test_split_slug_detail_title_drops_stray_punctuation_tail():
    assert sfs.split_slug("Pune-Intern-GenAI-(Customer-Success)-)", "Intern - GenAI (Customer Success)") == (
        "Intern - GenAI (Customer Success)", "Pune")
    assert sfs.split_slug("Bloomington-R&D-Intern-)-MN", "R&D Intern")[1] == "Bloomington, MN"
