"""Heartbeat: health verdict, 24h DB counts, and --no-notify (no network)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from typer.testing import CliRunner

from jobhunt import cli
from jobhunt import db as v2db
from jobhunt.classify import Classification
from jobhunt.models import Job

runner = CliRunner()


def _fake_cfg(watchlist: int = 3):
    companies = [SimpleNamespace(name=f"C{i}", ats="greenhouse") for i in range(watchlist)]
    # one unresolved company should NOT count toward the watchlist
    companies.append(SimpleNamespace(name="Unresolved", ats=None))
    return SimpleNamespace(companies=companies)


def _seed_db(path: str) -> None:
    """Seed: 3 postings in 24h (1 sent, 1 failed delivery), 1 old; 1 active + 1 pending sub."""
    from conftest import make_sub

    conn = v2db.connect(path)
    now = datetime.now(timezone.utc)
    jobs = []
    for ext, seen in (("1", now), ("2", now), ("3", now), ("old", now - timedelta(days=3))):
        job = Job(source="x", company="C", external_id=ext, title="SWE Intern", url="u")
        v2db.insert_new_postings(conn, [job], seen_at=v2db.to_iso(seen))
        jobs.append(job)
    sid = make_sub(conn, 1)
    make_sub(conn, 2, status="pending")
    v2db.reserve_delivery(conn, sid, jobs[0].uid)
    v2db.mark_sent(conn, sid, [jobs[0].uid])
    v2db.reserve_delivery(conn, sid, jobs[1].uid)
    v2db.mark_failed(conn, sid, [jobs[1].uid])
    conn.close()


# ---- (b) 24h counts --------------------------------------------------------

def test_db_counts_24h_window(tmp_path):
    db = str(tmp_path / "jobhunt_v2.db")
    _seed_db(db)
    assert cli._heartbeat_db_counts(db) == {
        "active_subscribers": 1, "pending_subscribers": 1, "disabled_subscribers": 0,
        "signups_24h": 2, "activations_24h": 1,
        "postings_24h": 3, "sent_24h": 1, "failed_24h": 1,
    }


def test_no_notify_table_has_growth_fields(monkeypatch, tmp_path):
    db = str(tmp_path / "jobhunt_v2.db")
    _seed_db(db)
    monkeypatch.setattr(cli, "CRON_LOG_PATH", tmp_path / "none.log")
    monkeypatch.setattr(cli, "LAST_RUN_PATH", tmp_path / "none.json")
    monkeypatch.setattr(cli, "load_config", lambda: _fake_cfg())
    out = runner.invoke(cli.app, ["heartbeat", "--no-notify", "--db", db], env={"COLUMNS": "200"}).output
    assert "Signups / activations (24h)" in out and "2 / 1" in out
    assert "Pending / disabled" in out and "1 / 0" in out


# ---- (a) health verdict: fresh vs stale cron.log --------------------------

def _run_heartbeat_no_notify(monkeypatch, tmp_path, cron_age_min=None, watchlist=3):
    db = str(tmp_path / "jobhunt_v2.db")
    v2db.connect(db).close()  # create empty schema
    logs = tmp_path / "logs"
    logs.mkdir()
    cron_log = logs / "cron.log"
    if cron_age_min is not None:
        cron_log.write_text("fired\n")
        ts = (datetime.now(timezone.utc) - timedelta(minutes=cron_age_min)).timestamp()
        import os

        os.utime(cron_log, (ts, ts))

    monkeypatch.setattr(cli, "CRON_LOG_PATH", cron_log)
    monkeypatch.setattr(cli, "LAST_RUN_PATH", logs / "last_run.json")
    monkeypatch.setattr(cli, "load_config", lambda: _fake_cfg(watchlist))
    return runner.invoke(cli.app, ["heartbeat", "--no-notify", "--db", db])


def test_health_fresh_cron_is_healthy(monkeypatch, tmp_path):
    result = _run_heartbeat_no_notify(monkeypatch, tmp_path, cron_age_min=10)
    assert result.exit_code == 0
    assert "healthy" in result.output
    assert "no cron activity" not in result.output


def test_health_stale_cron_is_unhealthy(monkeypatch, tmp_path):
    result = _run_heartbeat_no_notify(monkeypatch, tmp_path, cron_age_min=200)
    assert result.exit_code == 0
    assert "no cron activity" in result.output


def test_health_missing_cron_log_is_unhealthy(monkeypatch, tmp_path):
    result = _run_heartbeat_no_notify(monkeypatch, tmp_path, cron_age_min=None)
    assert result.exit_code == 0
    assert "no worker.log" in result.output


def test_watchlist_counts_only_resolved(monkeypatch, tmp_path):
    result = _run_heartbeat_no_notify(monkeypatch, tmp_path, cron_age_min=10, watchlist=5)
    assert result.exit_code == 0
    # 5 resolved shown as the watchlist (the unresolved company is excluded)
    assert "5" in result.output


# ---- (c) --no-notify prints and does NOT hit the webhook ------------------

def test_no_notify_does_not_call_webhook(monkeypatch, tmp_path):
    called = {"n": 0}

    class _Boom:
        def __init__(self, *a, **k):
            called["n"] += 1

        def send_status(self, *a, **k):
            called["n"] += 1

    monkeypatch.setattr(cli, "DiscordNotifier", _Boom)
    result = _run_heartbeat_no_notify(monkeypatch, tmp_path, cron_age_min=10)
    assert result.exit_code == 0
    assert called["n"] == 0  # webhook path never touched


def test_notify_path_calls_send_status(monkeypatch, tmp_path):
    captured = {}

    class _Fake:
        def __init__(self, url=None, *a, **k):
            captured["url"] = url

        def send_status(self, title, fields, color):
            captured["title"] = title
            captured["color"] = color
            captured["fields"] = fields

    db = str(tmp_path / "jobhunt_v2.db")
    v2db.connect(db).close()
    logs = tmp_path / "logs"
    logs.mkdir()
    cron_log = logs / "cron.log"
    cron_log.write_text("fired\n")
    last_run = logs / "last_run.json"
    last_run.write_text(json.dumps({
        "ts": datetime.now(timezone.utc).isoformat(),
        "companies": 3, "new_postings": 1, "alerts_sent": 0,
    }))

    monkeypatch.setattr(cli, "CRON_LOG_PATH", cron_log)
    monkeypatch.setattr(cli, "LAST_RUN_PATH", last_run)
    monkeypatch.setattr(cli, "load_config", lambda: _fake_cfg())
    monkeypatch.setattr(cli, "DiscordNotifier", _Fake)

    from conftest import hook_url

    monkeypatch.setenv("ADMIN_WEBHOOK_URL", hook_url(9))
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", hook_url(8))
    result = runner.invoke(cli.app, ["heartbeat", "--db", db])
    assert result.exit_code == 0
    assert captured["url"] == hook_url(9)  # admin channel, never DISCORD_WEBHOOK_URL
    assert hook_url(9) not in result.output
    assert captured["title"].startswith("🟢")
    assert captured["color"] == 0x2ECC71
    names = {n for n, _ in captured["fields"]}
    assert {"Status", "Watchlist", "Active subscribers", "Postings (24h)",
                   "Deliveries sent (24h)", "Deliveries failed (24h)"} <= names
    assert "Spend" not in names


# ---- send_status no-ops on empty fields -----------------------------------

def test_send_status_noop_on_empty_fields():
    from jobhunt.notifier import DiscordNotifier

    posted = []
    notifier = DiscordNotifier(webhook_url="https://example/webhook")
    notifier._post = lambda embeds: posted.append(embeds)
    notifier.send_status("title", [], 0x000000)
    assert posted == []


def test_heartbeat_without_admin_webhook_posts_nothing(monkeypatch, tmp_path):
    from conftest import hook_url

    called = []

    class _Fake:
        def __init__(self, *a, **k):
            called.append(a)

    db = str(tmp_path / "jobhunt_v2.db")
    v2db.connect(db).close()
    monkeypatch.setattr(cli, "CRON_LOG_PATH", tmp_path / "none.log")
    monkeypatch.setattr(cli, "LAST_RUN_PATH", tmp_path / "none.json")
    monkeypatch.setattr(cli, "load_config", lambda: _fake_cfg())
    monkeypatch.setattr(cli, "DiscordNotifier", _Fake)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", hook_url(8))
    monkeypatch.delenv("ADMIN_WEBHOOK_URL", raising=False)
    result = runner.invoke(cli.app, ["heartbeat", "--db", db])
    assert result.exit_code == 0 and "ADMIN_WEBHOOK_URL" in result.output
    assert called == []
