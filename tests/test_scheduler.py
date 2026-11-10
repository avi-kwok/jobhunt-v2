"""Concurrent fetch scheduler: per-host caps/pauses, Retry-After, 429 adaptation, breaker,
deadline, DB equivalence with a sequential run, and the visibility surfaces."""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

import httpx
import respx
from typer.testing import CliRunner

from jobhunt import cli, dashboard, pipeline, scheduler
from jobhunt import db as v2db
from jobhunt.adapters import base
from jobhunt.config import load_config
from jobhunt.models import CompanyConfig, Job, PolitenessConfig

T0 = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
ATS = ["greenhouse", "lever", "ashby"]
runner = CliRunner()


def co(name, ats="greenhouse"):
    return CompanyConfig(name=name, ats=ats, slug=name.lower())


def pol(**kw):
    d = dict(company_delay_min=0.0, company_delay_max=0.0, max_workers=6, per_host_concurrency=2)
    d.update(kw)
    return PolitenessConfig(**d)


def cfg_with(p):
    c = load_config(Path("config.yaml"), resolved_path=Path("/nonexistent.yaml"))
    c.politeness = p
    return c


def job(company, i=0):
    return Job(source="fake", company=company, external_id=str(i), title=f"Software Engineer Intern {i}",
               url=f"https://x.example/{company}/{i}", location="Toronto, ON")


class Probe:
    """Fake fetcher: sleeps, tracks per-host and global in-flight maxima and start times."""

    def __init__(self, sleep=0.02, hook=None):
        self.sleep, self.hook = sleep, hook
        self.lock = threading.Lock()
        self.cur: dict[str, int] = {}
        self.max: dict[str, int] = {}
        self.max_all = 0
        self.starts: dict[str, list[float]] = {}
        self.order: list[str] = []

    def __call__(self, company, errors):
        h = scheduler.host_key(company)
        with self.lock:
            self.cur[h] = self.cur.get(h, 0) + 1
            self.max[h] = max(self.max.get(h, 0), self.cur[h])
            self.max_all = max(self.max_all, sum(self.cur.values()))
            self.starts.setdefault(h, []).append(time.monotonic())
            self.order.append(company.name)
        try:
            if self.hook:
                self.hook(company, h)
            time.sleep(self.sleep)
            return [job(company.name)]
        finally:
            with self.lock:
                self.cur[h] -= 1


def run(conn, p, companies, fetch, **kw):
    rep = scheduler.ScanReport()
    errors: list[str] = []
    new = pipeline.ingest(cfg_with(p), conn, errors, companies, fetch=fetch, now=T0, report=rep, **kw)
    return new, errors, rep


# ---- host key -------------------------------------------------------------

def test_host_keys():
    assert scheduler.host_key(co("a")) == "boards-api.greenhouse.io"
    assert scheduler.host_key(co("a", "lever")) == "api.lever.co"
    assert scheduler.host_key(co("a", "ashby")) == "api.ashbyhq.com"
    assert scheduler.host_key(co("a", "amazon")) == "www.amazon.jobs"
    wd = CompanyConfig(name="w", ats="workday", tenant="nvidia", wd="wd5", site="s")
    assert scheduler.host_key(wd) == "nvidia.wd5.myworkdayjobs.com"


# ---- concurrency / politeness ---------------------------------------------

def test_per_host_cap_never_exceeded(conn):
    probe = Probe(sleep=0.03)
    _, _, rep = run(conn, pol(per_host_concurrency=2), [co(f"g{i}") for i in range(14)], probe)
    assert probe.max["boards-api.greenhouse.io"] == 2
    assert rep.scanned == 14 and not rep.deferred


def test_cap_one_is_strictly_sequential_per_host(conn):
    probe = Probe(sleep=0.01)
    run(conn, pol(per_host_concurrency=1), [co(f"g{i}") for i in range(6)], probe)
    assert probe.max["boards-api.greenhouse.io"] == 1


def test_different_hosts_run_in_parallel(conn):
    probe = Probe(sleep=0.05)
    companies = [co(f"{a}{i}", a) for i in range(4) for a in ATS]
    t = time.monotonic()
    run(conn, pol(per_host_concurrency=1), companies, probe)
    elapsed = time.monotonic() - t
    assert probe.max_all >= 3
    assert elapsed < 12 * 0.05 * 0.6  # sequential would be 0.6 s


def test_pause_applies_per_host_not_globally(conn):
    probe = Probe(sleep=0.0)
    companies = [co(f"g{i}") for i in range(3)] + [co(f"l{i}", "lever") for i in range(3)]
    t = time.monotonic()
    run(conn, pol(company_delay_min=0.1, company_delay_max=0.1, per_host_concurrency=2),
        companies, probe)
    # per host: starts >= 0.1 s apart; both hosts overlap, so ~0.2 s total, not 0.5 s global
    for h, starts in probe.starts.items():
        gaps = [b - a for a, b in zip(starts, starts[1:])]
        assert all(g >= 0.09 for g in gaps), (h, gaps)
    assert time.monotonic() - t < 0.45


# ---- Retry-After ----------------------------------------------------------

def test_retry_after_seconds_http_date_and_cap(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(base.time, "sleep", sleeps.append)
    base.begin_scan()
    url = "https://api.lever.co/v0/postings/x"
    soon = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=30), usegmt=True)
    with respx.mock:
        route = respx.get(url).mock(side_effect=[
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(503, headers={"Retry-After": soon}),
            httpx.Response(200, json=[]),
        ])
        with httpx.Client() as c:
            assert base.request_with_retry(c, "GET", url).status_code == 200
        assert route.call_count == 3
    assert 7 <= sleeps[0] <= 7.5
    assert 28 <= sleeps[1] <= 30.6
    snap = base.STATS.snapshot()["api.lever.co"]
    assert snap["429"] == 1 and snap["5xx"] == 1 and snap["retries"] == 2
    assert snap["retry_after_honored"] == 2 and snap["requests"] == 3
    assert base.parse_retry_after("99999") == 60.0
    assert base.parse_retry_after("garbage") is None
    assert base.parse_retry_after(None) is None


def test_no_retry_after_uses_backoff(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(base.time, "sleep", sleeps.append)
    base.begin_scan()
    url = "https://api.lever.co/v0/postings/x"
    with respx.mock:
        respx.get(url).mock(side_effect=[httpx.Response(429), httpx.Response(200, json=[])])
        with httpx.Client() as c:
            base.request_with_retry(c, "GET", url)
    assert 1.0 <= sleeps[0] <= 1.5
    assert base.STATS.snapshot()["api.lever.co"]["retry_after_honored"] == 0


# ---- 429 adaptation, breaker, deadline ------------------------------------

def test_429_drops_host_concurrency_and_doubles_pause(conn):
    def hook(company, host):
        if company.name == "g0":
            base.STATS.record_response(host, 429)

    probe = Probe(sleep=0.15, hook=hook)
    run(conn, pol(company_delay_min=0.1, company_delay_max=0.1, per_host_concurrency=2,
                  breaker_threshold=50), [co(f"g{i}") for i in range(4)], probe)
    starts = probe.starts["boards-api.greenhouse.io"]
    assert probe.max["boards-api.greenhouse.io"] == 1
    gaps = [b - a for a, b in zip(starts[1:], starts[2:])]
    assert all(g >= 0.18 for g in gaps)  # pause doubled (0.2) for the rest of the scan


def test_breaker_skips_host_and_defers_rest(conn):
    def hook(company, host):
        if host == "boards-api.greenhouse.io":
            base.STATS.record_response(host, 429)

    probe = Probe(sleep=0.01, hook=hook)
    companies = [co(f"g{i}") for i in range(12)] + [co(f"l{i}", "lever") for i in range(3)]
    new, errors, rep = run(conn, pol(company_delay_min=0.01, company_delay_max=0.01,
                                     breaker_threshold=3), companies, probe)
    gh = [c for c in rep.deferred if c.startswith("g")]
    assert rep.breaker_hosts == ["boards-api.greenhouse.io"]
    assert len(gh) >= 1 and not [c for c in rep.deferred if c.startswith("l")]
    assert rep.hosts["boards-api.greenhouse.io"]["skipped_by_breaker"] == len(gh)
    assert sum(1 for c in rep.companies if c["host"] == "api.lever.co") == 3
    assert len(gh) + sum(1 for c in rep.companies if c["host"].startswith("boards")) == 12
    assert any("RATE LIMIT" in e and "boards-api.greenhouse.io" in e for e in errors)


def test_deferred_companies_scanned_first_next_run(conn):
    companies = [co(f"g{i}") for i in range(10)]
    p = pol(max_workers=1, per_host_concurrency=1)
    probe = Probe(sleep=0.05)
    _, _, rep = run(conn, p, companies, probe, deadline_seconds=0.12)
    assert rep.deadline_hit and 0 < len(rep.deferred) < 10
    reordered = pipeline.deferred_first(companies, rep.deferred)
    assert [c.name for c in reordered[: len(rep.deferred)]] == [
        c.name for c in companies if c.name in set(rep.deferred)]
    probe2 = Probe(sleep=0.0)
    run(conn, p, reordered, probe2)
    assert probe2.order[: len(rep.deferred)] == [c.name for c in reordered[: len(rep.deferred)]]


def test_deadline_stops_submission_and_defers_rest(conn):
    companies = [co(f"g{i}") for i in range(20)]
    probe = Probe(sleep=0.05)
    t = time.monotonic()
    _, errors, rep = run(conn, pol(max_workers=1, per_host_concurrency=1), companies, probe,
                         deadline_seconds=0.12)
    assert time.monotonic() - t < 0.6
    assert rep.deadline_hit
    assert rep.scanned + len(rep.deferred) == 20 and rep.scanned >= 1
    assert any("SCAN DEADLINE" in e for e in errors)


# ---- DB equivalence -------------------------------------------------------

def test_db_results_match_sequential_ingest(conn, tmp_path):
    def fetch(company, errors):
        time.sleep(0.005)
        n = int(company.name[1:])
        if n % 7 == 0:
            errors.append(f"{company.name}: boom")
            return []
        return [job(company.name, i) for i in range(n % 4 + 1)]

    companies = [co(f"c{i}", ATS[i % 3]) for i in range(30)]
    seq = v2db.connect(tmp_path / "seq.db")
    a, ea, _ = run(seq, pol(max_workers=1, per_host_concurrency=1), companies, fetch)
    b, eb, rb = run(conn, pol(), companies, fetch)
    assert [j.uid for j in a] == [j.uid for j in b]
    assert sorted(map(str, ea)) == sorted(map(str, eb))
    q = "SELECT uid FROM postings"
    assert sorted(r[0] for r in seq.execute(q).fetchall()) == sorted(
        r[0] for r in conn.execute(q).fetchall())
    assert rb.errored == len([i for i in range(30) if i % 7 == 0])
    seq.close()


# ---- last_run.json, dashboard, heartbeat ----------------------------------

def _report():
    rep = scheduler.ScanReport(total_seconds=12.3, ingest_seconds=10.1, scanned=30, errored=2,
                               deadline_hit=True, deferred=["Zed"], breaker_hosts=["api.lever.co"])
    rep.companies = [{"name": f"C{i}", "host": "h", "seconds": float(i), "jobs": i, "error": None}
                     for i in range(40)]
    rep.hosts = {"api.lever.co": {"requests": 9, "429": 3, "403": 1, "5xx": 2, "retries": 4,
                                  "retry_after_honored": 2, "skipped_by_breaker": 5}}
    return rep


def test_last_run_json_has_stats_and_stays_small(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "LOGS_DIR", tmp_path)
    monkeypatch.setattr(cli, "LAST_RUN_PATH", tmp_path / "last_run.json")
    cli._write_last_run_marker(40, 1, 0, _report().to_dict())
    d = json.loads((tmp_path / "last_run.json").read_text())
    assert d["companies"] == 40 and d["total_seconds"] == 12.3 and d["ingest_seconds"] == 10.1
    assert len(d["slowest_companies"]) == 20 and d["slowest_companies"][0]["name"] == "C39"
    assert d["hosts"]["api.lever.co"]["429"] == 3 and d["deferred"] == ["Zed"]
    assert d["breaker_hosts"] == ["api.lever.co"]


def test_ingest_report_records_timings_and_hosts(conn):
    _, _, rep = run(conn, pol(), [co("a"), co("b", "lever")], Probe(sleep=0.01))
    d = rep.to_dict()
    assert d["ingest_seconds"] > 0 and d["companies_scanned"] == 2
    assert {c["name"] for c in d["slowest_companies"]} == {"a", "b"}
    assert all(c["jobs"] == 1 and c["error"] is None for c in d["slowest_companies"])


def test_dashboard_renders_hosts_table_escaped(conn):
    lr = _report().to_dict()
    lr["hosts"]["<script>alert(1)</script>"] = dict(lr["hosts"]["api.lever.co"])
    lr["slowest_companies"][0]["name"] = "<b>x</b>"
    html = dashboard.render_page(conn, "1m", "2m", lr)
    assert "<h2>Hosts</h2>" in html and "api.lever.co" in html and "Breaker skips" in html
    assert "<script>alert(1)" not in html and "&lt;script&gt;" in html
    assert "<b>x</b>" not in html and "Last scan" in html
    assert "No fetch measurements" in dashboard.render_page(conn, "1m", "2m", None)


def _heartbeat(monkeypatch, tmp_path, last_run):
    db = str(tmp_path / "v2.db")
    v2db.connect(db).close()
    cron = tmp_path / "cron.log"
    cron.write_text("x")
    lr = tmp_path / "last_run.json"
    lr.write_text(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), **last_run}))
    monkeypatch.setattr(cli, "CRON_LOG_PATH", cron)
    monkeypatch.setattr(cli, "LAST_RUN_PATH", lr)
    from types import SimpleNamespace
    monkeypatch.setattr(cli, "load_config",
                        lambda: SimpleNamespace(companies=[SimpleNamespace(name="a", ats="x")]))
    cap = {}

    class Fake:
        def __init__(self, url=None): pass
        def send_status(self, title, fields, color):
            cap.update(title=title, fields=dict(fields), color=color)

    from conftest import hook_url
    monkeypatch.setattr(cli, "DiscordNotifier", Fake)
    monkeypatch.setenv("ADMIN_WEBHOOK_URL", hook_url(9))
    assert runner.invoke(cli.app, ["heartbeat", "--db", db]).exit_code == 0
    return cap


def test_heartbeat_rate_limit_field_and_escalation(monkeypatch, tmp_path):
    ok = _heartbeat(monkeypatch, tmp_path, {"hosts": {"h": {"429": 0, "403": 0}}, "breaker_hosts": []})
    assert ok["fields"]["Rate limits (last scan)"].startswith("0 x 429/403 · 0 host")
    assert ok["title"].startswith("🟢")
    bad = _heartbeat(monkeypatch, tmp_path, {
        "hosts": {"h": {"429": 3, "403": 1}}, "breaker_hosts": ["h"]})
    assert bad["fields"]["Rate limits (last scan)"] == "4 x 429/403 · 1 host(s) tripped"
    assert bad["title"].startswith("🟡") and bad["color"] != ok["color"]
    old = _heartbeat(monkeypatch, tmp_path, {})
    assert old["fields"]["Rate limits (last scan)"] == "n/a"
