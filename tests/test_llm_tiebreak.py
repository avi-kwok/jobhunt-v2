"""LLM tie-break for ambiguous-only titles. Fake client only: zero network, zero real API calls."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import respx
from conftest import hook_url, make_sub
from typer.testing import CliRunner

from jobhunt import cli, dashboard, db, llm_tiebreak, pipeline
from jobhunt.adapters.base import Adapter, register
from jobhunt.classify import RULES_VERSION, classify
from jobhunt.config import load_config
from jobhunt.models import CompanyConfig, Job, LLMTiebreakConfig, PolitenessConfig

FAKE_KEY = "sk-ant-api03-FAKEKEYFORTESTSONLY0000000000"
T0 = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
runner = CliRunner()


# ---- fakes ----------------------------------------------------------------


def resp(is_swe=True, reason="cloud platform software", *, stop="end_turn", text=None,
         in_tok=400, out_tok=40):
    body = text if text is not None else json.dumps(
        {"is_swe_or_adjacent": is_swe, "reason": reason})
    return SimpleNamespace(
        stop_reason=stop,
        content=[SimpleNamespace(type="text", text=body)],
        usage=SimpleNamespace(input_tokens=in_tok, output_tokens=out_tok),
    )


class FakeClient:
    """Stands in for anthropic.Anthropic: records create() kwargs, replays scripted results."""

    def __init__(self, *results):
        self.results = list(results) or [resp()]
        self.calls: list[dict] = []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        r = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(r, Exception):
            raise r
        return r


def make_tb(client, **over) -> llm_tiebreak.TieBreaker:
    return llm_tiebreak.TieBreaker(LLMTiebreakConfig(**over), client)


@pytest.fixture
def cfg():
    c = load_config(Path("config.yaml"), resolved_path=Path("/nonexistent.yaml"))
    c.companies = [CompanyConfig(name="Acme", ats="fake_llm")]
    c.politeness = PolitenessConfig(enabled=False)
    return c


def job(title, company="Acme", location="Toronto, ON", i=1, **kw):
    return Job(source="fake_llm", company=company, external_id=str(i), title=title,
               url=f"https://jobs.example/{i}", location=location, **kw)


# ---- verdict applied both ways --------------------------------------------


def test_ambiguous_only_llm_yes_overrides_rules_no(cfg):
    j = job("Systems Engineer Intern", company="Lockheed Martin", description="x" * 5000)
    rules = classify(j, cfg.criteria, cfg.location_tiers)
    assert not rules.eligible and rules.reject_reason == "no swe-category term in title"

    client = FakeClient(resp(True, "flight software team"))
    c = classify(j, cfg.criteria, cfg.location_tiers, make_tb(client))
    assert c.eligible and c.reject_reason is None
    assert c.categories == ["swe", "llm"] and c.rules_version == RULES_VERSION

    (kw,) = client.calls
    assert kw["model"] == "claude-haiku-5-5" and kw["max_tokens"] == 1024
    assert kw["output_config"]["effort"] == "low"
    fmt = kw["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["schema"]["required"] == ["is_swe_or_adjacent", "reason"]
    assert not {"temperature", "top_p", "top_k"} & set(kw)
    assert [m["role"] for m in kw["messages"]] == ["user"]  # no assistant prefill
    prompt = kw["messages"][0]["content"]
    assert "Lockheed Martin" in prompt and "Systems Engineer Intern" in prompt
    assert "Toronto, ON" in prompt
    assert prompt.count("x") <= llm_tiebreak.SNIPPET_CHARS + 5


def test_ambiguous_only_llm_no_overrides_rules_yes(cfg):
    j = job("Platform Engineer Intern")
    assert classify(j, cfg.criteria, cfg.location_tiers).eligible  # rules say yes

    c = classify(j, cfg.criteria, cfg.location_tiers,
                 make_tb(FakeClient(resp(False, "  vehicle\nplatform hardware  "))))
    assert not c.eligible
    assert c.reject_reason == "llm: vehicle platform hardware"
    assert c.categories == ["llm"] and c.level == "internship" and c.countries == ["CA"]


@pytest.mark.parametrize("title,kw", [
    ("Software Engineer Intern", {}),                              # strong term
    ("Platform Software Engineer Intern", {}),                     # strong + ambiguous
    ("Mechanical Engineer Intern", {}),                            # no role term at all
    ("Platform Engineer", {}),                                     # no intern term
    ("Senior Platform Engineer Intern", {}),                       # exclude term
    ("Platform Engineer Intern", {"location": "Berlin, Germany"}),  # location
    ("Platform Engineer Intern", {"description": "Requires 5+ years of experience."}),
])
def test_never_called_unless_ambiguous_only_and_other_gates_pass(cfg, title, kw):
    j = job(title, **kw)
    client = FakeClient()
    c = classify(j, cfg.criteria, cfg.location_tiers, make_tb(client))
    assert client.calls == []
    assert c == classify(j, cfg.criteria, cfg.location_tiers)


# ---- off switches ---------------------------------------------------------


def test_no_key_or_disabled_means_no_tiebreaker(monkeypatch, cfg):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert llm_tiebreak.from_config(LLMTiebreakConfig(), client=FakeClient()) is None
    assert cli._make_tiebreak(cfg) is None
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    assert llm_tiebreak.from_config(LLMTiebreakConfig(enabled=False), client=FakeClient()) is None
    assert isinstance(llm_tiebreak.from_config(LLMTiebreakConfig(), client=FakeClient()),
                      llm_tiebreak.TieBreaker)


def test_real_client_built_with_short_timeout_and_one_retry(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    tb = llm_tiebreak.from_config(LLMTiebreakConfig())
    assert tb is not None
    assert tb.client.timeout == llm_tiebreak.TIMEOUT_SECONDS == 20.0
    assert tb.client.max_retries == 1  # constructed only; never called


# ---- caps -----------------------------------------------------------------


def test_per_scan_cap_stops_calls(cfg):
    client = FakeClient(resp(False, "hardware"))
    tb = make_tb(client, max_calls_per_scan=2)
    out = [classify(job("Platform Engineer Intern", i=i), cfg.criteria, cfg.location_tiers, tb)
           for i in range(3)]
    assert len(client.calls) == 2 and tb.calls == 2 and tb.capped == 1
    assert [c.eligible for c in out] == [False, False, True]  # third = rules verdict
    assert out[2].categories == ["swe"]


def test_daily_cap_stops_calls(cfg):
    # each call: 400 in + 40 out = $0.00006; cap $0.0001 -> 2 calls, then the rules decide
    client = FakeClient(resp(False, "hardware"))
    tb = make_tb(client, daily_usd_cap=0.0001)
    for i in range(4):
        classify(job("Platform Engineer Intern", i=i), cfg.criteria, cfg.location_tiers, tb)
    assert len(client.calls) == 2 and tb.capped == 2


def test_daily_cap_already_spent_today_means_no_call(cfg):
    llm_tiebreak.USAGE_PATH.write_text(json.dumps(
        {"day": datetime.now(timezone.utc).date().isoformat(), "calls": 9,
         "input_tokens": 1, "output_tokens": 1, "usd": 0.25}))
    client = FakeClient()
    c = classify(job("Platform Engineer Intern"), cfg.criteria, cfg.location_tiers, make_tb(client))
    assert client.calls == [] and c.categories == ["swe"]


# ---- failures -> rules verdict --------------------------------------------


@pytest.mark.parametrize("result", [
    RuntimeError(f"boom {FAKE_KEY}"),
    TimeoutError("read timed out"),
    resp(stop="refusal"),
    resp(stop="max_tokens", text='{"is_swe_or_adjacent": fa'),
    resp(text="not json"),
    resp(text=json.dumps({"reason": "missing the bool"})),
    resp(text=json.dumps({"is_swe_or_adjacent": "yes", "reason": "string not bool"})),
    SimpleNamespace(stop_reason="end_turn", content=[], usage=None),
])
def test_errors_refusal_malformed_fall_back_to_rules(cfg, result):
    for title, company in (("Platform Engineer Intern", "Acme"),
                           ("Systems Engineer Intern", "Lockheed Martin")):
        j = job(title, company=company)
        tb = make_tb(FakeClient(result))
        c = classify(j, cfg.criteria, cfg.location_tiers, tb)
        assert c == classify(j, cfg.criteria, cfg.location_tiers)
        assert "llm" not in c.categories
        assert tb.calls == 1 and tb.fallbacks == 1 and tb.decided == 0
        assert FAKE_KEY not in (tb.last_error or "") and FAKE_KEY not in tb.summary()


def test_anthropic_sdk_errors_are_caught(cfg):
    import anthropic
    import httpx2

    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    for exc in (anthropic.APITimeoutError(request=req), anthropic.APIConnectionError(request=req)):
        tb = make_tb(FakeClient(exc))
        c = classify(job("Platform Engineer Intern"), cfg.criteria, cfg.location_tiers, tb)
        assert c.eligible and c.categories == ["swe"] and tb.fallbacks == 1


# ---- spend accounting -----------------------------------------------------


def test_spend_accounting_from_usage():
    tb = make_tb(FakeClient(resp(in_tok=1000, out_tok=100)))
    tb(job("Platform Engineer Intern", i=1))
    tb(job("Platform Engineer Intern", i=2))
    u = llm_tiebreak.read_usage()
    assert (u["calls"], u["input_tokens"], u["output_tokens"]) == (2, 2000, 200)
    # 2 x (1000 x $0.10 + 100 x $0.50) / 1M
    assert u["usd"] == pytest.approx(0.0003)
    assert llm_tiebreak.cost_usd(1_000_000, 1_000_000) == pytest.approx(0.60)


def test_usage_resets_on_a_new_utc_day_and_tolerates_corruption():
    p = llm_tiebreak.USAGE_PATH
    p.write_text(json.dumps({"day": "2000-01-01", "calls": 5, "usd": 9.0}))
    assert llm_tiebreak.read_usage()["usd"] == 0.0
    p.write_text("{corrupt")
    assert llm_tiebreak.read_usage()["calls"] == 0
    make_tb(FakeClient(resp(in_tok=10, out_tok=10)))(job("Platform Engineer Intern"))
    assert llm_tiebreak.read_usage()["calls"] == 1


def test_failed_call_counts_toward_calls_without_tokens():
    make_tb(FakeClient(RuntimeError("down")))(job("Platform Engineer Intern"))
    u = llm_tiebreak.read_usage()
    assert u["calls"] == 1 and u["usd"] == 0.0


# ---- once per posting, shared by every subscriber -------------------------


@register
class FakeLLMAdapter(Adapter):
    ats = "fake_llm"
    JOBS: list[Job] = []

    def fetch(self, company):
        return list(FakeLLMAdapter.JOBS)


@respx.mock
def test_classified_once_for_three_subscribers(cfg, conn):
    urls = [hook_url(n) for n in (1, 2, 3)]
    routes = [respx.post(u).mock(return_value=httpx.Response(204)) for u in urls]
    for n in (1, 2, 3):
        make_sub(conn, n, activated_at=T0 - timedelta(days=1))
    FakeLLMAdapter.JOBS = [
        job("Systems Engineer Intern", company="Acme", i=1, posted_at=T0),
        job("Software Engineer Intern", company="Acme", i=2, posted_at=T0),
        # stale ambiguous posting: tagged stale at ingest, never reaches the LLM
        job("Integration Engineer Intern", company="Acme", i=3, posted_at=T0 - timedelta(days=3)),
    ]
    client = FakeClient(resp(True, "devops platform"))
    tb = make_tb(client)
    try:
        errors: list[str] = []
        new = pipeline.ingest(cfg, conn, errors, cfg.companies, now=T0)
        pipeline.classify_new(cfg, conn, new, tb)
        pipeline.classify_untagged(cfg, conn, now=T0, tiebreak=tb)
        subs = db.list_active_subscribers(conn)
        plan = pipeline.match_subscribers(cfg, conn, subs)
        res = [pipeline.deliver(conn, s, plan[s.id], log=lambda m: None) for s in subs]
        # second cycle: nothing new, nothing re-classified
        new2 = pipeline.ingest(cfg, conn, errors, cfg.companies, now=T0 + timedelta(minutes=30))
        pipeline.classify_new(cfg, conn, new2, tb)
    finally:
        FakeLLMAdapter.JOBS = []

    assert errors == []
    assert len(client.calls) == 1 and "Systems Engineer Intern" in client.calls[0]["messages"][0]["content"]
    assert [r.sent for r in res] == [2, 2, 2]
    for route in routes:
        embeds = json.loads(route.calls[0].request.content)["embeds"]
        assert len(embeds) == 2
        assert "llm" not in json.dumps(embeds)  # internal marker never shown to subscribers
    row = conn.execute(
        "SELECT categories, rules_version FROM posting_tags WHERE uid=?",
        ("fake_llm:Acme:1",)).fetchone()
    assert json.loads(row["categories"]) == ["swe", "llm"] and row["rules_version"] == RULES_VERSION
    stale = conn.execute("SELECT reject_reason FROM posting_tags WHERE uid=?",
                         ("fake_llm:Acme:3",)).fetchone()
    assert stale["reject_reason"] == pipeline.STALE_REASON


# ---- visibility: heartbeat, doctor, dashboard -----------------------------


def _heartbeat(monkeypatch, tmp_path) -> str:
    dbp = str(tmp_path / "hb.db")
    db.connect(dbp).close()
    monkeypatch.setattr(cli, "CRON_LOG_PATH", tmp_path / "none.log")
    monkeypatch.setattr(cli, "LAST_RUN_PATH", tmp_path / "none.json")
    monkeypatch.setattr(cli, "load_config", lambda: SimpleNamespace(companies=[]))
    r = runner.invoke(cli.app, ["heartbeat", "--no-notify", "--db", dbp], env={"COLUMNS": "220"})
    assert r.exit_code == 0, r.output
    return r.output


def test_heartbeat_shows_llm_calls_and_spend(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    make_tb(FakeClient(resp(in_tok=4000, out_tok=1600)))(job("Platform Engineer Intern"))
    out = _heartbeat(monkeypatch, tmp_path)
    assert "LLM tie-break (today)" in out
    assert "1 calls · $0.0012 of $0.25" in out
    assert FAKE_KEY not in out


def test_heartbeat_says_disabled_without_key(monkeypatch, tmp_path):
    out = _heartbeat(monkeypatch, tmp_path)
    assert "disabled (ANTHROPIC_API_KEY not set) · 0 calls" in out


def _doctor_row(monkeypatch, tmp_path) -> str:
    from jobhunt import crypto

    path = str(tmp_path / "d.db")
    db.connect(path).close()
    monkeypatch.setenv("DATABASE_URL", path)
    monkeypatch.setenv("JOBHUNT_SECRET_KEY", crypto.generate_secret_key())
    monkeypatch.setenv("ADMIN_WEBHOOK_URL", hook_url(9))
    (row,) = [c for c in cli.run_doctor() if c[0] == "LLM tie-break"]
    assert row[1] is True  # informational: always PASS
    return row[2]


def test_doctor_llm_row(monkeypatch, tmp_path):
    assert _doctor_row(monkeypatch, tmp_path) == "disabled (ANTHROPIC_API_KEY not set)"
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    assert _doctor_row(monkeypatch, tmp_path) == "enabled (key set)"
    r = runner.invoke(cli.app, ["doctor"], env={"COLUMNS": "220"})
    assert "LLM tie-break" in r.output and FAKE_KEY not in r.output


def test_dashboard_llm_section(conn):
    llm = {"status": "enabled (key set)", "calls": 7, "input_tokens": 2800,
           "output_tokens": 280, "usd": 0.00042, "cap": 0.25}
    html = dashboard.render_page(conn, "1 min ago", "2 min ago", None, llm)
    assert "LLM tie-break (today, UTC)" in html and "<td>7</td>" in html
    assert "<td>0.0004</td>" in html and "<td>0.25</td>" in html
    assert "LLM tie-break" not in dashboard.render_page(conn, "1 min ago", "2 min ago")
