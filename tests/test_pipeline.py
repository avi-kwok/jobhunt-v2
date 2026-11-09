"""End-to-end v2 pipeline: fake adapter -> ingest -> classify -> match -> Discord (respx)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import respx

from jobhunt import db, pipeline
from jobhunt.adapters.base import Adapter, register
from jobhunt.config import load_config
from jobhunt.models import CompanyConfig, Job, PolitenessConfig

URL_A = "https://discord.com/api/webhooks/111/TOKENAAAA"
URL_B = "https://discord.com/api/webhooks/222/TOKENBBBB"

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


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "p.db")
    yield c
    c.close()


@pytest.fixture(autouse=True)
def _reset_fake():
    FakeAdapter.JOBS = []
    yield
    FakeAdapter.JOBS = []


def run_cycle(cfg, conn, env, now, dry_run=False, cap=30, log=lambda m: None):
    errors: list[str] = []
    new = pipeline.ingest(cfg, conn, errors, cfg.companies, now=now)
    pipeline.classify_new(cfg, conn, new)
    users = db.list_active_users(conn)
    plan = pipeline.match_users(cfg, conn, users)
    results = {u.id: pipeline.deliver(conn, u, plan[u.id], env=env, dry_run=dry_run,
                                      cap=cap, log=log) for u in users}
    assert errors == []
    return results


def add_user(conn, env_name, activated_at, **prefs):
    return db.create_user(conn, db.Prefs(**prefs), "webhook_env", env_name,
                          activated_at=db.to_iso(activated_at))


def posted_titles(route):
    out = []
    for call in route.calls:
        out += [e["title"] for e in json.loads(call.request.content)["embeds"]]
    return out


@respx.mock
def test_two_users_batching_and_idempotent_rerun(cfg, conn):
    ra = respx.post(URL_A).mock(return_value=httpx.Response(204))
    rb = respx.post(URL_B).mock(return_value=httpx.Response(204))
    add_user(conn, "A", T0 - timedelta(days=1))
    add_user(conn, "B", T0 - timedelta(days=1), levels=["coop"])
    FakeAdapter.JOBS = [_job(i) for i in range(12)] + [
        _job(100, title="Co-op, Software Developer"),
        _job(101, title="Senior Software Engineer Intern"),
        _job(102, title="Software Engineer, Internal Tools"),
        _job(103, title="Software Engineer Intern", location="Berlin, Germany"),
    ]
    env = {"A": URL_A, "B": URL_B}
    res = run_cycle(cfg, conn, env, T0)
    # A: 13 eligible -> 10 + 3 embeds in two messages; B (coop only): 1 message
    assert res[1].sent == 13 and len(ra.calls) == 2
    assert res[2].sent == 1 and len(rb.calls) == 1
    assert len(json.loads(ra.calls[0].request.content)["embeds"]) == 10
    assert posted_titles(rb) == ["Co-op, Software Developer"]

    # idempotent: same postings re-fetched, nothing new, nothing re-sent
    res = run_cycle(cfg, conn, env, T0 + timedelta(minutes=30))
    assert all(r.sent == 0 for r in res.values())
    assert len(ra.calls) == 2 and len(rb.calls) == 1
    # even a fresh candidate pass over the same DB sends nothing
    users = db.list_active_users(conn)
    plan = pipeline.match_users(cfg, conn, users)
    assert plan[1] == [] and plan[2] == []


@respx.mock
def test_404_disables_user_and_others_unaffected(cfg, conn):
    ra = respx.post(URL_A).mock(return_value=httpx.Response(204))
    rb = respx.post(URL_B).mock(return_value=httpx.Response(404, json={"message": "Unknown"}))
    a = add_user(conn, "A", T0 - timedelta(days=1))
    b = add_user(conn, "B", T0 - timedelta(days=1))
    FakeAdapter.JOBS = [_job(1)]
    logs: list[str] = []
    res = run_cycle(cfg, conn, {"A": URL_A, "B": URL_B}, T0, log=logs.append)
    assert res[a].sent == 1 and not res[a].disabled
    assert res[b].disabled and res[b].sent == 0
    row = conn.execute("SELECT status, disabled_reason FROM users WHERE id=?", (b,)).fetchone()
    assert row["status"] == "disabled" and "404" in row["disabled_reason"]
    assert "TOKENBBBB" not in " ".join(logs + res[b].notes)
    # disabled user is skipped on the next cycle
    FakeAdapter.JOBS = [_job(1), _job(2)]
    run_cycle(cfg, conn, {"A": URL_A, "B": URL_B}, T0 + timedelta(minutes=30))
    assert len(rb.calls) == 1 and len(ra.calls) == 2


@respx.mock
def test_new_user_only_gets_postings_after_activation(cfg, conn):
    route = respx.post(URL_A).mock(return_value=httpx.Response(204))
    FakeAdapter.JOBS = [_job(1, title="Software Engineer Intern OLD")]
    run_cycle(cfg, conn, {"A": URL_A}, T0)  # no users yet; posting stored + classified
    assert len(route.calls) == 0
    add_user(conn, "A", T0 + timedelta(hours=1))
    FakeAdapter.JOBS = [_job(1, title="Software Engineer Intern OLD"),
                        _job(2, title="Software Engineer Intern NEW")]
    res = run_cycle(cfg, conn, {"A": URL_A}, T0 + timedelta(hours=2))
    assert res[1].sent == 1
    assert posted_titles(route) == ["Software Engineer Intern NEW"]


@respx.mock
def test_dry_run_when_env_missing_reserves_nothing(cfg, conn):
    route = respx.post(URL_A).mock(return_value=httpx.Response(204))
    uid = add_user(conn, "A", T0 - timedelta(days=1))
    FakeAdapter.JOBS = [_job(1)]
    logs: list[str] = []
    res = run_cycle(cfg, conn, {}, T0, log=logs.append)
    assert res[uid].dry_run and res[uid].sent == 0
    assert conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] == 0
    assert len(route.calls) == 0 and any("not sent" in m for m in logs)
    # once the variable is set the same posting is delivered
    res = run_cycle(cfg, conn, {"A": URL_A}, T0 + timedelta(minutes=30))
    assert res[uid].sent == 1 and len(route.calls) == 1
    # explicit dry_run flag (--no-notify) behaves the same way
    FakeAdapter.JOBS = [_job(1), _job(2)]
    res = run_cycle(cfg, conn, {"A": URL_A}, T0 + timedelta(minutes=60), dry_run=True)
    assert res[uid].dry_run and len(route.calls) == 1
    assert conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] == 1


@respx.mock
def test_cap_per_cycle_then_drains(cfg, conn):
    route = respx.post(URL_A).mock(return_value=httpx.Response(204))
    uid = add_user(conn, "A", T0 - timedelta(days=1))
    FakeAdapter.JOBS = [_job(i) for i in range(35)]
    res = run_cycle(cfg, conn, {"A": URL_A}, T0)
    assert res[uid].sent == 30 and res[uid].skipped_over_cap == 5
    assert len(posted_titles(route)) == 30
    res = run_cycle(cfg, conn, {"A": URL_A}, T0 + timedelta(minutes=30))
    assert res[uid].sent == 5


@respx.mock
def test_server_error_marks_failed_then_retries(cfg, conn):
    route = respx.post(URL_A).mock(return_value=httpx.Response(500))
    uid = add_user(conn, "A", T0 - timedelta(days=1))
    FakeAdapter.JOBS = [_job(1)]
    res = run_cycle(cfg, conn, {"A": URL_A}, T0)
    assert res[uid].failed == 1 and not res[uid].disabled
    assert conn.execute("SELECT status FROM deliveries").fetchone()[0] == "failed"
    assert conn.execute("SELECT fail_count FROM destinations").fetchone()[0] == 1
    route.mock(return_value=httpx.Response(204))
    res = run_cycle(cfg, conn, {"A": URL_A}, T0 + timedelta(minutes=30))
    assert res[uid].sent == 1
    assert conn.execute("SELECT status FROM deliveries").fetchone()[0] == "sent"
    assert conn.execute("SELECT fail_count FROM destinations").fetchone()[0] == 0


@respx.mock
def test_stale_postings_stored_but_never_delivered(cfg, conn):
    route = respx.post(URL_A).mock(return_value=httpx.Response(204))
    add_user(conn, "A", T0 - timedelta(days=1))
    FakeAdapter.JOBS = [
        _job(1, title="Software Engineer Intern STALE", posted_at=T0 - timedelta(days=3)),
        _job(2, title="Software Engineer Intern FRESH", posted_at=T0 - timedelta(minutes=10)),
    ]
    run_cycle(cfg, conn, {"A": URL_A}, T0)
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
