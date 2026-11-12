"""Simplify fallback adapter + allowlist (no network)."""

import time

import httpx
import respx

from jobhunt.adapters.simplify import LISTINGS_URL, SimplifyAdapter
from jobhunt.config import load_config, simplify_allowlist
from jobhunt.models import CompanyConfig

DAY = 86400


def row(id_, company, age_days=0, **kw):
    r = {
        "id": id_, "company_name": company, "title": "SWE Intern", "active": True,
        "is_visible": True, "locations": ["Toronto, ON", "Remote"],
        "url": f"https://x.example/{id_}", "date_posted": int(time.time()) - age_days * DAY,
    }
    r.update(kw)
    return r


def pseudo(names):
    return CompanyConfig(name="SimplifyJobs", ats="simplify", simplify_companies=names)


def run(rows, tmp_path, names=("Google", "Jane Street")):
    with respx.mock:
        respx.get(LISTINGS_URL).mock(return_value=httpx.Response(200, json=rows))
        return SimplifyAdapter(client=httpx.Client(), cache_dir=tmp_path).fetch(pseudo(list(names)))


def test_only_allowlisted_canonical_names(tmp_path):
    jobs = run([row("a", "GOOGLE"), row("b", "Jane  Street."), row("c", "Acme")], tmp_path)
    assert sorted(j.company for j in jobs) == ["Google", "Jane Street"]
    j = next(j for j in jobs if j.company == "Google")
    assert j.location == "Toronto, ON; Remote" and j.posted_at is None
    assert j.posted_note.startswith("Posted ") and j.posted_note.endswith("via SimplifyJobs")


def test_filters_inactive_invisible_old(tmp_path):
    rows = [
        row("ok", "Google"),
        row("inactive", "Google", active=False),
        row("hidden", "Google", is_visible=False),
        row("old", "Google", age_days=5),
    ]
    assert [j.external_id for j in run(rows, tmp_path)] == ["ok"]


def test_uid_format(tmp_path):
    assert run([row("42", "Google")], tmp_path)[0].uid == "simplify:Google:42"


def test_304_reuses_cache(tmp_path):
    with respx.mock:
        route = respx.get(LISTINGS_URL).mock(
            return_value=httpx.Response(200, json=[row("a", "Google")], headers={"ETag": '"e1"'})
        )
        ad = SimplifyAdapter(client=httpx.Client(), cache_dir=tmp_path)
        assert len(ad.fetch(pseudo(["Google"]))) == 1
        route.mock(return_value=httpx.Response(304))
        assert len(ad.fetch(pseudo(["Google"]))) == 1
        assert route.calls.last.request.headers["if-none-match"] == '"e1"'


def test_failure_with_cache_raises(tmp_path):
    import pytest

    with respx.mock:
        route = respx.get(LISTINGS_URL).mock(
            return_value=httpx.Response(200, json=[row("a", "Google")], headers={"ETag": '"e1"'})
        )
        ad = SimplifyAdapter(client=httpx.Client(), cache_dir=tmp_path)
        ad.fetch(pseudo(["Google"]))
        route.mock(return_value=httpx.Response(404))
        with pytest.raises(httpx.HTTPStatusError):
            ad.fetch(pseudo(["Google"]))


def test_allowlist_excludes_ats_and_covered():
    cos = [
        CompanyConfig(name="Stripe", ats="greenhouse"),
        CompanyConfig(name="Slack", covered_by="Salesforce"),
        CompanyConfig(name="Google"),
        pseudo(None),
    ]
    assert simplify_allowlist(cos) == ["Google"]


def test_load_config_allowlist_never_has_ats_company():
    cfg = load_config()
    sj = next(c for c in cfg.companies if c.ats == "simplify")
    with_ats = {c.name for c in cfg.companies if c.ats}
    covered = {c.name for c in cfg.companies if c.covered_by}
    assert sj.simplify_companies
    assert not set(sj.simplify_companies) & (with_ats | covered)


def test_name_alias_maps_to_allowlisted_canonical(tmp_path):
    jobs = run([row("w", "Walmart")], tmp_path, names=("Walmart Global Tech",))
    assert [j.company for j in jobs] == ["Walmart Global Tech"]


def test_name_alias_ignored_when_canonical_not_allowlisted(tmp_path):
    assert run([row("w", "Walmart")], tmp_path, names=("Google",)) == []


def test_grad_only_rows_skipped_and_terms_kept(tmp_path):
    rows = [
        row("ug", "Google", degrees=["Bachelor's", "Master's"], terms=["Summer 2027"]),
        row("assoc", "Google", degrees=["Associate's"]),
        row("grad", "Google", degrees=["Master's", "PhD"]),
        row("mba", "Google", degrees=["MBA"]),
        row("none", "Google"),
        row("empty", "Google", degrees=[]),
    ]
    jobs = run(rows, tmp_path)
    assert sorted(j.external_id for j in jobs) == ["assoc", "empty", "none", "ug"]
    assert next(j for j in jobs if j.external_id == "ug").terms == ["Summer 2027"]
    assert next(j for j in jobs if j.external_id == "none").terms is None
