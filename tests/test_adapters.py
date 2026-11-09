"""Adapter parsing against mocked ATS responses (no network)."""

import httpx
import pytest
import respx

from jobhunt.adapters.base import html_to_text
from jobhunt.adapters.greenhouse import GreenhouseAdapter
from jobhunt.adapters.lever import LeverAdapter
from jobhunt.adapters.ashby import AshbyAdapter
from jobhunt.adapters.workday import SEARCH_TERMS, WorkdayAdapter
from jobhunt.adapters.amazon import BASE_QUERIES, AmazonAdapter
from jobhunt.models import CompanyConfig, Job


def test_html_to_text_strips_and_truncates():
    out = html_to_text("<p>Hello <b>world</b></p><ul><li>a</li><li>b</li></ul>")
    assert "Hello world" in out
    assert "<" not in out
    long = html_to_text("<p>" + "x" * 5000 + "</p>", max_chars=100)
    assert len(long) <= 101  # 100 + ellipsis


@respx.mock
def test_greenhouse_parses():
    respx.get("https://boards-api.greenhouse.io/v1/boards/stripe/jobs?content=true").mock(
        return_value=httpx.Response(
            200,
            json={
                "jobs": [
                    {
                        "id": 42,
                        "title": "Software Engineer Intern",
                        "updated_at": "2026-06-01T00:00:00Z",
                        "absolute_url": "https://stripe.com/jobs/42",
                        "location": {"name": "Vancouver, BC"},
                        "content": "<p>Build things</p>",
                    }
                ]
            },
        )
    )
    jobs = GreenhouseAdapter(client=httpx.Client()).fetch(
        CompanyConfig(name="Stripe", ats="greenhouse", slug="stripe")
    )
    assert len(jobs) == 1
    j = jobs[0]
    assert j.external_id == "42"
    assert j.location == "Vancouver, BC"
    assert j.uid == "greenhouse:Stripe:42"
    assert "Build things" in j.description


@respx.mock
def test_lever_parses():
    respx.get("https://api.lever.co/v0/postings/brex?mode=json").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": "abc",
                    "text": "Backend Engineering Intern",
                    "hostedUrl": "https://jobs.lever.co/brex/abc",
                    "categories": {"location": "Toronto", "team": "Infra", "commitment": "Intern"},
                    "descriptionPlain": "Do backend work",
                    "createdAt": 1717200000000,
                }
            ],
        )
    )
    jobs = LeverAdapter(client=httpx.Client()).fetch(
        CompanyConfig(name="Brex", ats="lever", slug="brex")
    )
    assert jobs[0].department == "Infra"
    assert jobs[0].employment_type == "Intern"
    assert jobs[0].posted_at is not None


@respx.mock
def test_ashby_parses():
    respx.get(
        "https://api.ashbyhq.com/posting-api/job-board/ramp?includeCompensation=true"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "jobs": [
                    {
                        "id": "x1",
                        "title": "SWE Intern",
                        "location": "Remote",
                        "jobUrl": "https://jobs.ashbyhq.com/ramp/x1",
                        "departmentName": "Eng",
                        "employmentType": "Intern",
                        "publishedAt": "2026-05-01T00:00:00Z",
                        "descriptionPlain": "Ship code",
                    }
                ]
            },
        )
    )
    jobs = AshbyAdapter(client=httpx.Client()).fetch(
        CompanyConfig(name="Ramp", ats="ashby", slug="ramp")
    )
    assert jobs[0].location == "Remote"
    assert jobs[0].url.endswith("/ramp/x1")


@respx.mock
def test_workday_paginates():
    url = "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/careers/jobs"

    # total=25: a full page of 20, then a partial page of 5 -> pagination triggers.
    def responder(request):
        import json

        offset = json.loads(request.content)["offset"]
        count = 20 if offset == 0 else 5
        postings = [
            {
                "title": "SWE Intern",
                "externalPath": f"/job/SWE-Intern_R{offset + i}",
                "locationsText": "Seattle",
                "bulletFields": [f"R{offset + i}"],
            }
            for i in range(count)
        ]
        return httpx.Response(200, json={"total": 25, "jobPostings": postings})

    respx.post(url).mock(side_effect=responder)
    jobs = WorkdayAdapter(client=httpx.Client()).fetch(
        CompanyConfig(name="Acme", ats="workday", tenant="acme", wd="wd5", site="careers")
    )
    assert len(jobs) == 25
    assert jobs[0].url == "https://acme.wd5.myworkdayjobs.com/careers/job/SWE-Intern_R0"


@respx.mock
def test_workday_searches_internship_terms_and_unions():
    """Adapter must query each internship term and dedupe overlapping results.

    Each searchText returns the SAME posting (a huge board would return it under
    several terms); the union must yield it exactly once. Also asserts pagination
    stops on a short page even though `total` is an unreliable 0 (as real Workday
    multi-word queries return), which the old `offset >= total` logic couldn't do.
    """
    url = "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/careers/jobs"
    queries = []

    def responder(request):
        import json

        body = json.loads(request.content)
        queries.append(body["searchText"])
        # One posting, total=0 (unreliable) -> must still stop after the short page.
        postings = [
            {
                "title": "Software Engineer Intern",
                "externalPath": "/job/SWE-Intern_R1",
                "locationsText": "Seattle",
                "bulletFields": ["R1"],
            }
        ]
        return httpx.Response(200, json={"total": 0, "jobPostings": postings})

    respx.post(url).mock(side_effect=responder)
    jobs = WorkdayAdapter(client=httpx.Client()).fetch(
        CompanyConfig(name="Acme", ats="workday", tenant="acme", wd="wd5", site="careers")
    )
    # Same posting under every term -> deduped to one.
    assert len(jobs) == 1
    assert jobs[0].external_id == "R1"
    # No facets in the discovery response -> fallback: one empty-search discovery
    # request, then every configured internship term issued as a query. Under
    # concurrency their arrival order is nondeterministic, so compare as sets.
    assert queries[0] == ""
    assert sorted(queries[1:]) == sorted(SEARCH_TERMS)


@respx.mock
def test_amazon_unions_internship_queries():
    """Amazon adapter narrows with internship base_query terms and unions them.

    No category filter is sent: amazon.jobs ignores a plain ``category=`` and
    files many SWE/PM intern postings under unrelated categories, so filtering
    by category would drop eligible roles (SWE *and* PM/TPM interns).
    """
    seen_queries = []
    seen_params = []

    def responder(request):
        seen_params.append(list(request.url.params.keys()))
        seen_queries.append(dict(request.url.params).get("base_query"))
        return httpx.Response(
            200,
            json={
                "hits": 1,
                "jobs": [
                    {
                        "id_icims": "999",
                        "title": "Software Dev Engineer Intern",
                        "location": "Seattle",
                        "job_path": "/en/jobs/999/x",
                    }
                ],
            },
        )

    respx.get("https://www.amazon.jobs/en/search.json").mock(side_effect=responder)
    jobs = AmazonAdapter(client=httpx.Client()).fetch(
        CompanyConfig(name="Amazon", ats="amazon")
    )
    assert len(jobs) == 1  # same posting across queries -> deduped
    assert seen_queries == list(BASE_QUERIES)
    assert not any(k.startswith("category") for keys in seen_params for k in keys)


@respx.mock
def test_amazon_returns_pm_interns_from_any_category():
    """PM/TPM and SWE interns come back whatever category Amazon files them under."""
    postings = [
        {"id_icims": "1", "title": "Program Manager Intern – Summer 2027",
         "job_category": "Project/Program/Product Management--Non-Tech", "job_path": "/1"},
        {"id_icims": "2", "title": "Product Manager Technical (PMT) Intern",
         "job_category": "Project/Program/Product Management--Technical", "job_path": "/2"},
        {"id_icims": "3", "title": "Software Development Engineer Intern - Summer 2027 (CAN)",
         "job_category": "Administrative Support", "job_path": "/3"},
    ]

    def responder(request):
        if dict(request.url.params).get("base_query") == "intern":
            return httpx.Response(200, json={"hits": 3, "jobs": postings})
        return httpx.Response(200, json={"hits": 0, "jobs": []})

    respx.get("https://www.amazon.jobs/en/search.json").mock(side_effect=responder)
    jobs = AmazonAdapter(client=httpx.Client()).fetch(
        CompanyConfig(name="Amazon", ats="amazon")
    )
    assert [j.external_id for j in jobs] == ["1", "2", "3"]


def test_workday_falls_back_to_sequential_on_429(monkeypatch):
    """A persistent 429 on the concurrent attempt triggers a sequential retry.

    request_with_retry raises HTTPStatusError(429) after exhausting retries; the
    adapter must cool off (no real sleep here) and re-run every term sequentially,
    returning the jobs the sequential pass finds.
    """
    from jobhunt.adapters import workday as workday_mod

    monkeypatch.setattr(workday_mod.time, "sleep", lambda *a, **k: None)

    company = CompanyConfig(
        name="Acme", ats="workday", tenant="acme", wd="wd5", site="careers",
        workday_facets=False,  # phrase-search path: no discovery request
    )
    job = Job(
        source="workday",
        company="Acme",
        external_id="R1",
        title="Software Engineer Intern",
        url="https://acme.wd5.myworkdayjobs.com/careers/job/SWE-Intern_R1",
    )

    calls = {"n": 0}

    def flaky_search(self, url, job_base, company, term, facets=None):
        calls["n"] += 1
        # Fail the very first (concurrent) search with a persistent-429 error,
        # exactly as request_with_retry surfaces it; sequential retry succeeds.
        if calls["n"] == 1:
            raise httpx.HTTPStatusError(
                "429", request=httpx.Request("POST", url),
                response=httpx.Response(429),
            )
        return [job]

    monkeypatch.setattr(WorkdayAdapter, "_search", flaky_search)

    jobs = WorkdayAdapter(client=httpx.Client()).fetch(company)
    assert [j.external_id for j in jobs] == ["R1"]  # deduped across all terms


def test_missing_slug_raises():
    with pytest.raises(ValueError):
        GreenhouseAdapter(client=httpx.Client()).fetch(CompanyConfig(name="X", ats="greenhouse"))


# ---- Workday internship-facet path -----------------------------------------

_WD_URL = "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/careers/jobs"

# Shaped like real CXS facets: flat facets, a nested location group whose values
# contain "Campus"/"University" (must be ignored), and "Internal Audit" (must not
# match "intern").
_FACETS = [
    {
        "facetParameter": "workerSubType",
        "descriptor": "Job Type",
        "values": [
            {"descriptor": "Regular", "id": "reg", "count": 900},
            {"descriptor": "Intern (Fixed Term)", "id": "intern-ft", "count": 20},
            {"descriptor": "Student/Coop (Fixed Term)", "id": "coop-ft", "count": 5},
            {"descriptor": "New College Graduate", "id": "ncg", "count": 9},
        ],
    },
    {
        "facetParameter": "jobFamily",
        "descriptor": "Job Family",
        "values": [
            {"descriptor": "Internal Audit", "id": "audit", "count": 50},
            {"descriptor": "Software Engineering", "id": "swe", "count": 300},
        ],
    },
    {
        "facetParameter": "locationMainGroup",
        "descriptor": None,
        "values": [
            {
                "facetParameter": "locations",
                "descriptor": "Locations",
                "values": [
                    {"descriptor": "Toronto - University Ave", "id": "loc1", "count": 3},
                    {"descriptor": "O'Fallon (Main Campus)", "id": "loc2", "count": 4},
                ],
            }
        ],
    },
]


def _posting(i, title="Product Manager Intern"):
    return {
        "title": title,
        "externalPath": f"/job/X_R{i}",
        "locationsText": "Toronto",
        "bulletFields": [f"R{i}"],
    }


def test_find_intern_facets_flat_skips_location_and_internal():
    from jobhunt.adapters.workday import find_intern_facets

    assert find_intern_facets(_FACETS) == {"workerSubType": ["intern-ft", "coop-ft"]}


def test_find_intern_facets_nested_group():
    from jobhunt.adapters.workday import find_intern_facets

    nested = [
        {
            "facetParameter": "jobFamilyGroupMain",
            "descriptor": "Category",
            "values": [
                {
                    "facetParameter": "jobFamilyGroup",
                    "descriptor": "Job Category",
                    "values": [
                        {"descriptor": "Campus", "id": "campus", "count": 17},
                        {"descriptor": "Finance", "id": "fin", "count": 80},
                    ],
                },
                {
                    "facetParameter": "Sub_Category",
                    "descriptor": "Sub Category",
                    "values": [{"descriptor": "Internship Programs", "id": "ip", "count": 9}],
                },
            ],
        }
    ]
    assert find_intern_facets(nested) == {"jobFamilyGroup": ["campus"], "Sub_Category": ["ip"]}


def test_find_intern_facets_none():
    from jobhunt.adapters.workday import find_intern_facets

    assert find_intern_facets([]) == {}
    assert find_intern_facets(None) == {}
    assert find_intern_facets([_FACETS[1]]) == {}  # only "Internal Audit"


@respx.mock
def test_workday_uses_intern_facet_with_empty_search():
    """Discovery finds the intern facet -> one facet-filtered, empty-text search.

    Discovery postings (unfiltered board) are discarded; the facet search paginates
    (20 + 3) and no SEARCH_TERMS phrase search is issued.
    """
    import json

    bodies = []

    def responder(request):
        body = json.loads(request.content)
        bodies.append(body)
        if not body["appliedFacets"]:  # discovery
            return httpx.Response(
                200,
                json={"total": 900, "facets": _FACETS,
                      "jobPostings": [_posting(999, "Senior Accountant")]},
            )
        offset = body["offset"]
        n = 20 if offset == 0 else 3
        return httpx.Response(
            200, json={"total": 23, "jobPostings": [_posting(offset + i) for i in range(n)]}
        )

    respx.post(_WD_URL).mock(side_effect=responder)
    jobs = WorkdayAdapter(client=httpx.Client()).fetch(
        CompanyConfig(name="Acme", ats="workday", tenant="acme", wd="wd5", site="careers")
    )
    assert len(jobs) == 23
    assert "R999" not in {j.external_id for j in jobs}
    assert all(b["searchText"] == "" for b in bodies)
    facet_bodies = [b for b in bodies if b["appliedFacets"]]
    assert [b["offset"] for b in facet_bodies] == [0, 20]
    assert facet_bodies[0]["appliedFacets"] == {"workerSubType": ["intern-ft", "coop-ft"]}


@respx.mock
def test_workday_unions_multiple_intern_facet_params():
    """Separate facet params AND together, so each gets its own search; union them."""
    import json

    facets = [
        {"facetParameter": "workerSubType", "descriptor": "Job Type",
         "values": [{"descriptor": "Intern", "id": "i", "count": 2}]},
        {"facetParameter": "jobFamilyGroup", "descriptor": "Job Category",
         "values": [{"descriptor": "University", "id": "u", "count": 2}]},
    ]
    applied = []

    def responder(request):
        body = json.loads(request.content)
        if not body["appliedFacets"]:
            return httpx.Response(200, json={"facets": facets, "jobPostings": []})
        applied.append(body["appliedFacets"])
        if "workerSubType" in body["appliedFacets"]:
            return httpx.Response(200, json={"jobPostings": [_posting(1), _posting(2)]})
        return httpx.Response(200, json={"jobPostings": [_posting(2), _posting(3)]})

    respx.post(_WD_URL).mock(side_effect=responder)
    jobs = WorkdayAdapter(client=httpx.Client()).fetch(
        CompanyConfig(name="Acme", ats="workday", tenant="acme", wd="wd5", site="careers")
    )
    assert [j.external_id for j in jobs] == ["R1", "R2", "R3"]
    assert sorted(applied, key=str) == sorted(
        [{"workerSubType": ["i"]}, {"jobFamilyGroup": ["u"]}], key=str
    )


@respx.mock
def test_workday_falls_back_to_search_terms_without_intern_facet():
    import json

    texts = []

    def responder(request):
        body = json.loads(request.content)
        texts.append(body["searchText"])
        assert body["appliedFacets"] == {}
        if body["searchText"] == "":
            return httpx.Response(200, json={"facets": [_FACETS[1]], "jobPostings": []})
        return httpx.Response(200, json={"jobPostings": [_posting(1)]})

    respx.post(_WD_URL).mock(side_effect=responder)
    jobs = WorkdayAdapter(client=httpx.Client()).fetch(
        CompanyConfig(name="Acme", ats="workday", tenant="acme", wd="wd5", site="careers")
    )
    assert [j.external_id for j in jobs] == ["R1"]
    assert texts[0] == ""
    assert sorted(texts[1:]) == sorted(SEARCH_TERMS)
    assert {"product intern", "product co-op", "program intern"} <= set(SEARCH_TERMS)


@respx.mock
def test_workday_facets_false_forces_search_terms():
    """workday_facets: false skips discovery entirely, even if a facet exists."""
    import json

    bodies = []

    def responder(request):
        body = json.loads(request.content)
        bodies.append(body)
        return httpx.Response(200, json={"facets": _FACETS, "jobPostings": [_posting(1)]})

    respx.post(_WD_URL).mock(side_effect=responder)
    jobs = WorkdayAdapter(client=httpx.Client()).fetch(
        CompanyConfig(name="Acme", ats="workday", tenant="acme", wd="wd5",
                      site="careers", workday_facets=False)
    )
    assert [j.external_id for j in jobs] == ["R1"]
    assert sorted(b["searchText"] for b in bodies) == sorted(SEARCH_TERMS)
    assert all(b["appliedFacets"] == {} for b in bodies)


def test_workday_facets_flag_loads_from_resolved_yaml(tmp_path):
    from jobhunt.config import load_config

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "criteria:\n  internship_terms: [intern]\n  role_terms: [software]\n"
        "location_tiers:\n  local: [toronto]\n"
        "companies:\n  - name: TD\n  - name: Adobe\n"
    )
    resolved = tmp_path / "companies.resolved.yaml"
    resolved.write_text(
        "companies:\n"
        "- {name: TD, ats: workday, tenant: td, wd: wd3, site: S, workday_facets: false}\n"
        "- {name: Adobe, ats: workday, tenant: adobe, wd: wd5, site: S}\n"
        "- {name: Extra, ats: workday, tenant: x, wd: wd1, site: S, workday_facets: false}\n"
    )
    cfg = load_config(cfg_path, resolved)
    by = {c.name: c for c in cfg.companies}
    assert by["TD"].workday_facets is False
    assert by["Adobe"].workday_facets is None
    assert by["Extra"].workday_facets is False  # resolved-only entry


def test_workday_429_during_facet_discovery_retries_sequentially(monkeypatch):
    from jobhunt.adapters import workday as workday_mod

    monkeypatch.setattr(workday_mod.time, "sleep", lambda *a, **k: None)
    calls = {"discover": 0, "workers": []}

    def flaky_discover(self, url):
        calls["discover"] += 1
        if calls["discover"] == 1:
            raise httpx.HTTPStatusError(
                "429", request=httpx.Request("POST", url), response=httpx.Response(429)
            )
        return _FACETS

    def fake_run(self, url, job_base, company, queries, workers):
        calls["workers"].append(workers)
        assert queries == [("", {"workerSubType": ["intern-ft", "coop-ft"]})]
        return []

    monkeypatch.setattr(WorkdayAdapter, "_discover_facets", flaky_discover)
    monkeypatch.setattr(WorkdayAdapter, "_run_queries", fake_run)
    WorkdayAdapter(client=httpx.Client()).fetch(
        CompanyConfig(name="Acme", ats="workday", tenant="acme", wd="wd5", site="careers")
    )
    assert calls == {"discover": 2, "workers": [1]}
