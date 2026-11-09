"""End-to-end v2 pipeline: fake adapter -> ingest -> classify -> match -> Discord (respx)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import respx
from conftest import hook_url, make_sub

from jobhunt import crypto, db, pipeline
from jobhunt.adapters.base import Adapter, register
from jobhunt.config import load_config
from jobhunt.models import CompanyConfig, Job, PolitenessConfig

URL_A = hook_url(1)
URL_B = hook_url(2)

T0 = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


def _job(i, title=None, company="Acme", location="Toronto, ON", **kw):
    return Job(source="fake", company=company, external_id=str(i),
               title=title or f"Software Engineer Intern {i}",
               url=f"https://jobs.example/{i}", location=location, **kw)


@register
class FakeAdapter(Adapter):
    ats = "fake"
    JOBS: list[Job] = []

    def fetch(self, company):
        return list(FakeAdapter.JOBS)


@pytest.fixture
def cfg():
    c = load_config(Path("config.yaml"), resolved_path=Path("/nonexistent.yaml"))
    c.companies = [CompanyConfig(name="Acme", ats="fake")]
    c.politeness = PolitenessConfig(enabled=False)
    return c


@pytest.fixture(autouse=True)
def _reset_fake():
    FakeAdapter.JOBS = []
    yield
    FakeAdapter.JOBS = []


def run_cycle(cfg, conn, now, dry_run=False, cap=30, log=lambda m: None):
    errors: list[str] = []
    new = pipeline.ingest(cfg, conn, errors, cfg.companies, now=now)
    pipeline.classify_new(cfg, conn, new)
    subs = db.list_active_subscribers(conn)
    plan = pipeline.match_subscribers(cfg, conn, subs)
    results = {s.id: pipeline.deliver(conn, s, plan[s.id], dry_run=dry_run, cap=cap, log=log)
               for s in subs}
    assert errors == []
    return results


def add_sub(conn, n, activated_at, **kw):
    return make_sub(conn, n, activated_at=activated_at, **kw)


def posted_titles(route):
    out = []
    for call in route.calls:
        out += [e["title"] for e in json.loads(call.request.content)["embeds"]]
    return out


@respx.mock
def test_two_subscribers_batching_and_idempotent_rerun(cfg, conn):
    ra = respx.post(URL_A).mock(return_value=httpx.Response(204))
    rb = respx.post(URL_B).mock(return_value=httpx.Response(204))
    a = add_sub(conn, 1, T0 - timedelta(days=1))
    b = add_sub(conn, 2, T0 - timedelta(days=1), levels=("coop",))
    FakeAdapter.JOBS = [_job(i) for i in range(12)] + [
        _job(100, title="Co-op, Software Developer"),
        _job(101, title="Senior Software Engineer Intern"),
        _job(102, title="Software Engineer, Internal Tools"),
        _job(103, title="Software Engineer Intern", location="Berlin, Germany"),
    ]
    res = run_cycle(cfg, conn, T0)
    # A: 13 eligible -> 10 + 3 embeds in two messages; B (coop only): 1 message
    assert res[a].sent == 13 and len(ra.calls) == 2
    assert res[b].sent == 1 and len(rb.calls) == 1
    assert len(json.loads(ra.calls[0].request.content)["embeds"]) == 10
    assert posted_titles(rb) == ["Co-op, Software Developer"]

    # idempotent: same postings re-fetched, nothing new, nothing re-sent
    res = run_cycle(cfg, conn, T0 + timedelta(minutes=30))
    assert all(r.sent == 0 for r in res.values())
    assert len(ra.calls) == 2 and len(rb.calls) == 1
    # even a fresh candidate pass over the same DB sends nothing
    subs = db.list_active_subscribers(conn)
    plan = pipeline.match_subscribers(cfg, conn, subs)
    assert plan[a] == [] and plan[b] == []


@respx.mock
def test_404_disables_subscriber_and_others_unaffected(cfg, conn):
    ra = respx.post(URL_A).mock(return_value=httpx.Response(204))
    rb = respx.post(URL_B).mock(return_value=httpx.Response(404, json={"message": "Unknown"}))
    a = add_sub(conn, 1, T0 - timedelta(days=1))
    b = add_sub(conn, 2, T0 - timedelta(days=1))
    FakeAdapter.JOBS = [_job(1)]
    logs: list[str] = []
    res = run_cycle(cfg, conn, T0, log=logs.append)
    assert res[a].sent == 1 and not res[a].disabled
    assert res[b].disabled and res[b].sent == 0
    row = conn.execute("SELECT status, disabled_reason FROM subscribers WHERE id=?", (b,)).fetchone()
    assert row["status"] == "disabled" and "404" in row["disabled_reason"]
    assert "B" * 64 not in " ".join(logs + res[b].notes)
    # disabled user is skipped on the next cycle
    FakeAdapter.JOBS = [_job(1), _job(2)]
    run_cycle(cfg, conn, T0 + timedelta(minutes=30))
    assert len(rb.calls) == 1 and len(ra.calls) == 2


@respx.mock
def test_new_subscriber_only_gets_postings_after_activation(cfg, conn):
    route = respx.post(URL_A).mock(return_value=httpx.Response(204))
    FakeAdapter.JOBS = [_job(1, title="Software Engineer Intern OLD")]
    run_cycle(cfg, conn, T0)  # no users yet; posting stored + classified
    assert len(route.calls) == 0
    sid = add_sub(conn, 1, T0 + timedelta(hours=1))
    FakeAdapter.JOBS = [_job(1, title="Software Engineer Intern OLD"),
                        _job(2, title="Software Engineer Intern NEW")]
    res = run_cycle(cfg, conn, T0 + timedelta(hours=2))
    assert res[sid].sent == 1
    assert posted_titles(route) == ["Software Engineer Intern NEW"]


@respx.mock
def test_dry_run_reserves_nothing(cfg, conn):
    route = respx.post(URL_A).mock(return_value=httpx.Response(204))
    sid = add_sub(conn, 1, T0 - timedelta(days=1))
    FakeAdapter.JOBS = [_job(1)]
    logs: list[str] = []
    res = run_cycle(cfg, conn, T0, dry_run=True, log=logs.append)
    assert res[sid].dry_run and res[sid].sent == 0
    assert conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] == 0
    assert len(route.calls) == 0 and any("not sent" in m for m in logs)
    assert URL_A not in " ".join(logs)
    # the same posting is delivered by the next real run
    res = run_cycle(cfg, conn, T0 + timedelta(minutes=30))
    assert res[sid].sent == 1 and len(route.calls) == 1


@respx.mock
def test_request_url_is_rebuilt_from_parsed_id_and_token(cfg, conn):
    canonical = hook_url(1)
    route = respx.post(canonical).mock(return_value=httpx.Response(204))
    # stored with a legacy host + trailing slash would be a different string; ours is normalized
    sid = add_sub(conn, 1, T0 - timedelta(days=1))
    FakeAdapter.JOBS = [_job(1)]
    assert run_cycle(cfg, conn, T0)[sid].sent == 1
    assert str(route.calls[0].request.url) == canonical


@respx.mock
def test_pending_older_than_an_hour_is_resent(cfg, conn):
    route = respx.post(URL_A).mock(return_value=httpx.Response(204))
    sid = add_sub(conn, 1, T0 - timedelta(days=1))
    FakeAdapter.JOBS = [_job(1)]
    run_cycle(cfg, conn, T0)
    # simulate a crash after reserve, before mark_sent
    conn.execute("UPDATE deliveries SET status='pending', sent_at=NULL, updated_at=?",
                 (db.to_iso(datetime.now(timezone.utc) - timedelta(hours=2)),))
    conn.commit()
    assert run_cycle(cfg, conn, T0 + timedelta(hours=2))[sid].sent == 1
    assert len(route.calls) == 2
    # a fresh pending row is NOT re-sent
    conn.execute("UPDATE deliveries SET status='pending', sent_at=NULL, updated_at=?",
                 (db.utcnow_iso(),))
    conn.commit()
    assert run_cycle(cfg, conn, T0 + timedelta(hours=3))[sid].sent == 0
    assert len(route.calls) == 2


@respx.mock
def test_401_disables_immediately(cfg, conn):
    respx.post(URL_A).mock(return_value=httpx.Response(401))
    sid = add_sub(conn, 1, T0 - timedelta(days=1))
    FakeAdapter.JOBS = [_job(1)]
    assert run_cycle(cfg, conn, T0)[sid].disabled
    assert db.get_subscriber(conn, sid).status == "disabled"


@respx.mock
def test_five_failed_cycles_disable_and_success_resets(cfg, conn):
    route = respx.post(URL_A).mock(return_value=httpx.Response(500))
    sid = add_sub(conn, 1, T0 - timedelta(days=1))
    FakeAdapter.JOBS = [_job(1)]
    for i in range(4):
        res = run_cycle(cfg, conn, T0 + timedelta(minutes=30 * i))
        assert not res[sid].disabled
    assert db.get_subscriber(conn, sid).fail_count == 4
    route.mock(return_value=httpx.Response(204))
    assert run_cycle(cfg, conn, T0 + timedelta(hours=3))[sid].sent == 1
    s = db.get_subscriber(conn, sid)
    assert s.fail_count == 0 and s.last_ok_at and s.status == "active"
    # five in a row now disables
    route.mock(return_value=httpx.Response(500))
    for i in range(5):
        FakeAdapter.JOBS = [_job(10 + i)]
        res = run_cycle(cfg, conn, T0 + timedelta(hours=4, minutes=30 * i))
    assert res[sid].disabled
    assert db.get_subscriber(conn, sid).status == "disabled"


def test_wrong_key_fails_fast(cfg, conn, monkeypatch):
    sid = add_sub(conn, 1, T0 - timedelta(days=1))
    FakeAdapter.JOBS = [_job(1)]
    monkeypatch.setenv("JOBHUNT_SECRET_KEY", crypto.generate_secret_key())
    with pytest.raises(crypto.CryptoError):
        run_cycle(cfg, conn, T0)
    assert db.get_subscriber(conn, sid).status == "active"
    assert conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] == 0


def test_country_and_level_matching_per_subscriber(cfg, conn):
    us = add_sub(conn, 1, T0 - timedelta(days=1), countries=("US",))
    ca = add_sub(conn, 2, T0 - timedelta(days=1), countries=("CA",), levels=("coop",))
    FakeAdapter.JOBS = [
        _job(1, title="Software Engineer Intern", location="Seattle, WA"),
        _job(2, title="Software Engineer Co-op", location="Toronto, ON"),
        _job(3, title="Software Engineer Intern", location="Multiple Locations"),
    ]
    errors: list[str] = []
    new = pipeline.ingest(cfg, conn, errors, cfg.companies, now=T0)
    pipeline.classify_new(cfg, conn, new)
    plan = pipeline.match_subscribers(cfg, conn)
    assert sorted(c.job.external_id for c in plan[us]) == ["1", "3"]
    assert sorted(c.job.external_id for c in plan[ca]) == ["2"]


def test_retention_runs_via_prune(cfg, conn):
    FakeAdapter.JOBS = [_job(1)]
    run_cycle(cfg, conn, T0 - timedelta(days=100))
    assert db.prune_postings(conn, now=T0) == 1
    assert conn.execute("SELECT COUNT(*) FROM posting_tags").fetchone()[0] == 0


@respx.mock
def test_cap_per_cycle_then_drains(cfg, conn):
    route = respx.post(URL_A).mock(return_value=httpx.Response(204))
    uid = add_sub(conn, 1, T0 - timedelta(days=1))
    FakeAdapter.JOBS = [_job(i) for i in range(35)]
    res = run_cycle(cfg, conn, T0)
    assert res[uid].sent == 30 and res[uid].skipped_over_cap == 5
    assert len(posted_titles(route)) == 30
    res = run_cycle(cfg, conn, T0 + timedelta(minutes=30))
    assert res[uid].sent == 5


@respx.mock
def test_server_error_marks_failed_then_retries(cfg, conn):
    route = respx.post(URL_A).mock(return_value=httpx.Response(500))
    uid = add_sub(conn, 1, T0 - timedelta(days=1))
    FakeAdapter.JOBS = [_job(1)]
    res = run_cycle(cfg, conn, T0)
    assert res[uid].failed == 1 and not res[uid].disabled
    assert conn.execute("SELECT status FROM deliveries").fetchone()[0] == "failed"
    assert conn.execute("SELECT fail_count FROM subscribers").fetchone()[0] == 1
    route.mock(return_value=httpx.Response(204))
    res = run_cycle(cfg, conn, T0 + timedelta(minutes=30))
    assert res[uid].sent == 1
    assert conn.execute("SELECT status FROM deliveries").fetchone()[0] == "sent"
    assert conn.execute("SELECT fail_count FROM subscribers").fetchone()[0] == 0


@respx.mock
def test_stale_postings_stored_but_never_delivered(cfg, conn):
    route = respx.post(URL_A).mock(return_value=httpx.Response(204))
    add_sub(conn, 1, T0 - timedelta(days=1))
    FakeAdapter.JOBS = [
        _job(1, title="Software Engineer Intern STALE", posted_at=T0 - timedelta(days=3)),
        _job(2, title="Software Engineer Intern FRESH", posted_at=T0 - timedelta(minutes=10)),
    ]
    run_cycle(cfg, conn, T0)
    assert posted_titles(route) == ["Software Engineer Intern FRESH"]
    assert conn.execute("SELECT COUNT(*) FROM postings").fetchone()[0] == 2
    row = conn.execute("SELECT eligible, reject_reason FROM posting_tags WHERE uid='fake:Acme:1'").fetchone()
    assert row["eligible"] == 0 and "stale" in row["reject_reason"]


def test_ingest_via_registry_records_adapter_errors(cfg, conn):
    class Boom(Adapter):
        ats = "boom"

        def fetch(self, company):
            raise RuntimeError("down")

    register(Boom)
    errors: list[str] = []
    out = pipeline.ingest(cfg, conn, errors, [CompanyConfig(name="B", ats="boom")], now=T0)
    assert out == [] and errors and "down" in errors[0]


def test_ingest_dedupes_within_fetch(cfg, conn):
    FakeAdapter.JOBS = [_job(1), _job(1)]
    new = pipeline.ingest(cfg, conn, [], cfg.companies, now=T0)
    assert len(new) == 1
