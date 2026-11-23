"""CLI: db init, keygen, subscriber admin, scan without LLM (offline)."""

from conftest import hook_url
from typer.testing import CliRunner

from jobhunt import cli, db

runner = CliRunner()
TOKEN = "A" * 64


def _add(path, env_val=None, **opts):
    env = {"MY_HOOK": env_val or hook_url(1)}
    args = ["subscriber", "add", "--webhook-env", "MY_HOOK", "--db", path]
    for k, v in opts.items():
        args += [f"--{k}", v]
    return runner.invoke(cli.app, args, env=env)


def test_db_init_and_keygen(tmp_path):
    r = runner.invoke(cli.app, ["db", "init", "--db", str(tmp_path / "x.db")])
    assert r.exit_code == 0 and "version=6" in r.output
    r = runner.invoke(cli.app, ["keygen"])
    assert r.exit_code == 0
    from jobhunt import crypto

    crypto.require_key(r.output.strip())


def test_subscriber_add_list_remove_never_prints_url(tmp_path):
    path = str(tmp_path / "v2.db")
    outputs = []
    r = _add(path, countries="US", levels="coop")
    outputs.append(r.output)
    assert r.exit_code == 0, r.output
    r2 = _add(path)  # same webhook again
    outputs.append(r2.output)
    assert r2.exit_code == 1 and "already subscribed" in r2.output
    lst = runner.invoke(cli.app, ["subscriber", "list", "--db", path])
    outputs.append(lst.output)
    assert lst.exit_code == 0 and "active" in lst.output and "...0001" in lst.output.replace(" ", "")
    conn = db.connect(path)
    sub = db.list_subscribers(conn)[0]
    conn.close()
    assert sub.status == "active" and sub.activated_at
    assert sub.countries == ["US"] and sub.levels == ["coop"]
    assert hook_url(1) not in sub.webhook_enc
    rm = runner.invoke(cli.app, ["subscriber", "remove", str(sub.id), "--db", path])
    outputs.append(rm.output)
    assert rm.exit_code == 0
    assert runner.invoke(cli.app, ["subscriber", "remove", "999", "--db", path]).exit_code == 1
    blob = "\n".join(outputs)
    assert "discord.com/api/webhooks" not in blob and "AAAAAAAA" not in blob


def test_subscriber_add_rejects_bad_input(tmp_path):
    path = str(tmp_path / "v2.db")
    r = _add(path, env_val="https://evil.com/api/webhooks/1/2")
    assert r.exit_code == 2 and "evil.com" not in r.output
    assert _add(path, countries="FR").exit_code == 2
    assert _add(path, levels="fulltime").exit_code == 2
    r = runner.invoke(cli.app, ["subscriber", "add", "--webhook-env", "NOPE", "--db", path])
    assert r.exit_code == 2


def test_subscriber_add_without_key_fails_clearly(tmp_path, monkeypatch):
    monkeypatch.delenv("JOBHUNT_SECRET_KEY")
    r = _add(str(tmp_path / "v2.db"))
    assert r.exit_code == 2 and "JOBHUNT_SECRET_KEY" in r.output
    assert "discord.com/api" not in r.output


def test_scan_has_no_llm_surface():
    import inspect

    src = inspect.getsource(cli.scan)
    for banned in ("llm_match", "BudgetGuard", "use_llm_filter", "ANTHROPIC", "get_api_key"):
        assert banned not in src
    from jobhunt import matcher

    assert not hasattr(matcher, "llm_match")


def test_db_option_defaults_defer_to_database_url():
    import typer.main

    cmd = typer.main.get_command(cli.app)
    for name in ("scan", "heartbeat", "list-matches"):
        opt = next(p for p in cmd.commands[name].params if p.name == "db_path")
        assert opt.default is None


def test_scan_survives_bad_subscriber_row_and_leaks_no_url(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from conftest import make_sub

    from jobhunt.classify import Classification
    from jobhunt.models import Job

    path = str(tmp_path / "scan.db")
    now = datetime.now(timezone.utc)
    conn = db.connect(path)
    bad = make_sub(conn, 1, activated_at=now - timedelta(days=1))
    good = make_sub(conn, 2, activated_at=now - timedelta(days=1))
    conn.execute("UPDATE subscribers SET webhook_enc='garbage' WHERE id=?", (bad,))
    conn.commit()
    job = Job(source="x", company="Acme", external_id="1", title="SWE Intern",
              url="https://x/1", location="Toronto, ON")
    db.insert_new_postings(conn, [job], seen_at=db.to_iso(now))
    db.upsert_tags(conn, job.uid, Classification(eligible=True, level="internship",
                                                 countries=["CA"]))
    conn.close()

    monkeypatch.setattr(cli.pipeline, "ingest", lambda *a, **k: [])
    sent = []

    class FakeNotifier:
        def __init__(self, url):
            sent.append(url)

        def send_embeds(self, embeds):
            pass

    real_deliver = cli.pipeline.deliver
    monkeypatch.setattr(
        cli.pipeline, "deliver",
        lambda *a, **k: real_deliver(*a, **{**k, "notifier_factory": FakeNotifier}),
    )
    r = runner.invoke(cli.app, ["scan", "--db", path, "--force", "--no-jitter"])
    assert r.exit_code == 0, r.output
    assert "discord.com/api/webhooks" not in r.output
    conn = db.connect(path)
    assert db.get_subscriber(conn, bad).status == "disabled"
    assert db.get_subscriber(conn, good).status == "active"
    assert db.delivery_status(conn, good, job.uid) == "sent"
    conn.close()
    assert len(sent) == 1


def _scan_setup(tmp_path, monkeypatch, n=2):
    from datetime import datetime, timedelta, timezone

    from conftest import make_sub

    from jobhunt.classify import Classification
    from jobhunt.models import Job

    path = str(tmp_path / "scan.db")
    now = datetime.now(timezone.utc)
    conn = db.connect(path)
    ids = [make_sub(conn, i + 1, activated_at=now - timedelta(days=1)) for i in range(n)]
    job = Job(source="x", company="Acme", external_id="1", title="SWE Intern",
              url="https://x/1", location="Toronto, ON")
    db.insert_new_postings(conn, [job], seen_at=db.to_iso(now))
    db.upsert_tags(conn, job.uid, Classification(eligible=True, level="internship",
                                                 countries=["CA"]))
    conn.close()
    monkeypatch.setattr(cli.pipeline, "ingest", lambda *a, **k: [])
    real_deliver = cli.pipeline.deliver

    class FakeNotifier:
        def __init__(self, url):
            pass

        def send_embeds(self, embeds):
            pass

    monkeypatch.setattr(
        cli.pipeline, "deliver",
        lambda *a, **k: real_deliver(*a, **{**k, "notifier_factory": FakeNotifier}),
    )
    return path, ids


def test_scan_wrong_worker_key_disables_nobody(tmp_path, monkeypatch):
    import base64

    path, ids = _scan_setup(tmp_path, monkeypatch)
    monkeypatch.setenv("JOBHUNT_SECRET_KEY", base64.urlsafe_b64encode(b"\x07" * 32).decode())
    r = runner.invoke(cli.app, ["scan", "--db", path, "--force", "--no-jitter"])
    assert r.exit_code != 0
    assert r.exit_code == 3 and "No subscriber disabled" in r.output
    conn = db.connect(path)
    assert [db.get_subscriber(conn, i).status for i in ids] == ["active", "active"]
    conn.close()


def test_scan_error_text_with_markup_does_not_crash(tmp_path, monkeypatch):
    path, _ = _scan_setup(tmp_path, monkeypatch, n=1)
    monkeypatch.setattr(cli.pipeline, "ingest",
                        lambda cfg, conn, errors, resolved, **kw: errors.append("board boom [/x] [red]") or [])
    r = runner.invoke(cli.app, ["scan", "--db", path, "--force", "--no-jitter"])
    assert r.exit_code == 0, r.output
    assert "[/x]" in r.output


def test_feedback_list_prints_user_text_literally(tmp_path):
    path = str(tmp_path / "fb.db")
    c = db.connect(path)
    db.add_feedback(c, "[red]loud[/red]", "", "bell\x1b[2J")
    c.close()
    r = runner.invoke(cli.app, ["feedback", "list", "--db", path])
    assert r.exit_code == 0
    assert "[red]loud[/red]" in r.output and "\x1b" not in r.output


def test_broadcast_sends_green_embed_and_disables_deleted_webhooks(tmp_path):
    import json

    import respx

    path = str(tmp_path / "v2.db")
    for n in (1, 2, 3):
        assert _add(path, env_val=hook_url(n)).exit_code == 0
    msg = tmp_path / "msg.md"
    msg.write_text("Hello **all**\n\nline two\n")
    with respx.mock() as m:
        ok = m.post(url__regex=r".*/api/webhooks/0+1/.*").respond(204)
        m.post(url__regex=r".*/api/webhooks/0+2/.*").respond(404)
        m.post(url__regex=r".*/api/webhooks/0+3/.*").respond(204)
        r = runner.invoke(cli.app, ["broadcast", str(msg), "--yes", "--db", path])
    assert r.exit_code == 1 and "sent to 2" in r.output and "B" * 20 not in r.output.replace("\n", "")
    body = json.loads(ok.calls[0].request.content)
    assert body["embeds"] == [{"description": "Hello **all**\n\nline two", "color": 0x2ECC71,
                               "footer": {"text": cli.BROADCAST_FOOTER}}]
    assert body["allowed_mentions"] == {"parse": []}
    conn = db.connect(path)
    assert {s.id: s.status for s in db.list_subscribers(conn)}[2] == "disabled"
    conn.close()


def test_broadcast_needs_yes_unless_targeted(tmp_path):
    import json

    import respx

    path = str(tmp_path / "v2.db")
    for n in (1, 2):
        assert _add(path, env_val=hook_url(n)).exit_code == 0
    msg = tmp_path / "msg.md"
    msg.write_text("hi")
    with respx.mock() as m:
        route = m.post(url__regex=r".*/api/webhooks/.*").respond(204)
        assert runner.invoke(cli.app, ["broadcast", str(msg), "--db", path]).exit_code == 2
        assert route.call_count == 0
        r = runner.invoke(cli.app, ["broadcast", str(msg), "--to", "1", "--to", "9",
                                    "--title", "A note", "--db", path])
    assert r.exit_code == 0 and route.call_count == 1 and "[9]" in r.output
    assert json.loads(route.calls[0].request.content)["embeds"][0]["title"] == "A note"
    assert runner.invoke(cli.app, ["broadcast", str(msg), "--to", "1", "--title", "t" * 257,
                                   "--db", path]).exit_code == 2
    msg.write_text("x" * 4097)
    assert runner.invoke(cli.app, ["broadcast", str(msg), "--to", "1", "--db", path]).exit_code == 2


def test_broadcast_skips_subscribers_opted_out_of_updates(tmp_path):
    import respx

    path = str(tmp_path / "v2.db")
    for n in (1, 2):
        assert _add(path, env_val=hook_url(n)).exit_code == 0
    conn = db.connect(path)
    db.set_updates_opt_out(conn, 2, True)
    conn.close()
    msg = tmp_path / "msg.md"
    msg.write_text("hi")
    with respx.mock(assert_all_called=False) as m:
        one = m.post(url__regex=r".*/api/webhooks/0+1/.*").respond(204)
        two = m.post(url__regex=r".*/api/webhooks/0+2/.*").respond(204)
        r = runner.invoke(cli.app, ["broadcast", str(msg), "--yes", "--db", path])
    assert r.exit_code == 0 and one.call_count == 1 and two.call_count == 0
    assert "Opted out of updates, skipped: [2]" in r.output and "sent to 1" in r.output


def test_rate_limit_warns_before_and_halts_only_the_limited_company(tmp_path, monkeypatch):
    import json

    import respx

    host = "api.smartrecruiters.com"
    seen: list[list[str]] = []
    plan = iter([
        ({host: {"429": 2}}, None),  # scan 1: 429s, retries got through -> warning only
        ({host: {"429": 5}}, None),  # scan 2: same, within 24 h -> no second warning
        ({host: {"429": 9}}, "HTTP 429"),  # scan 3: first company's fetch fails -> halted
        ({}, None),  # scan 4: that company skipped, the rest of the host still polled
    ])

    def fake_ingest(cfg, conn, errors, companies, report=None, **k):
        on_host = [c for c in companies if cli.scheduler.host_key(c) == host]
        seen.append([c.name for c in on_host])
        report.hosts, err = next(plan)
        report.companies = [{"name": c.name, "host": host, "error": err if i == 0 else None}
                            for i, c in enumerate(on_host)]
        return []

    monkeypatch.setattr(cli.pipeline, "ingest", fake_ingest)
    monkeypatch.setenv("ADMIN_WEBHOOK_URL", hook_url(9))
    args = ["scan", "--db", str(tmp_path / "scan.db"), "--force", "--no-jitter", "--no-notify"]
    with respx.mock() as m:
        admin = m.post(url__regex=r".*/api/webhooks/0+9/.*").respond(204)
        for _ in range(4):
            assert runner.invoke(cli.app, args).exit_code == 0
    assert len(seen[0]) > 1, "config has several SmartRecruiters API companies"
    limited = seen[0][0]
    assert admin.call_count == 1  # one early warning, nothing when the halt happens
    embed = json.loads(admin.calls[0].request.content)["embeds"][0]
    assert host in embed["title"] and "Close to a rate limit" in embed["title"]
    assert set(json.loads(cli.HALTED_PATH.read_text())) == {limited}
    assert limited not in seen[3] and set(seen[3]) == set(seen[0]) - {limited}

    assert limited in runner.invoke(cli.app, ["halted", "list"]).output
    assert runner.invoke(cli.app, ["halted", "resume", limited]).exit_code == 0
    assert json.loads(cli.HALTED_PATH.read_text()) == {}
    assert runner.invoke(cli.app, ["halted", "resume", limited]).exit_code == 1


def test_updates_send_posts_each_new_note_once_from_a_git_ref(tmp_path, monkeypatch):
    import json
    import subprocess
    from types import SimpleNamespace
    from datetime import datetime, timedelta, timezone

    import respx

    db_path = str(tmp_path / "v2.db")
    for n in (1, 2):
        assert _add(db_path, env_val=hook_url(n)).exit_code == 0
    conn = db.connect(db_path)
    db.set_updates_opt_out(conn, 2, True)
    conn.close()
    repo = tmp_path / "repo"
    (repo / "updates").mkdir(parents=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    old = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%d")
    (repo / "updates" / f"{today}-new.md").write_text("- Faster alerts\n")
    (repo / "updates" / f"{old}-old.md").write_text("- Ancient news\n")
    git = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True,  # noqa: E731
                                    capture_output=True, text=True).stdout.strip()
    git("init", "-q")
    git("add", ".")
    tree = git("write-tree")  # a tree-ish works like origin/main for ls-tree / show
    (repo / "updates" / f"{today}-uncommitted.md").write_text("- not pushed\n")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(cli, "UPDATES_SENT_PATH", tmp_path / "updates_sent.json")
    monkeypatch.setattr(cli, "load_config", lambda: SimpleNamespace(schedule=None))  # no config.yaml here
    monkeypatch.setattr(cli, "_in_quiet_hours", lambda *a, **k: False)
    args = ["updates", "send", "--ref", tree, "--db", db_path]
    with respx.mock(assert_all_called=False) as m:
        one = m.post(url__regex=r".*/api/webhooks/0+1/.*").respond(204)
        two = m.post(url__regex=r".*/api/webhooks/0+2/.*").respond(204)
        r = runner.invoke(cli.app, args)
        assert r.exit_code == 0 and one.call_count == 1 and two.call_count == 0
        embed = json.loads(one.calls[0].request.content)["embeds"][0]
        assert embed["title"] == cli.UPDATES_TITLE and embed["description"] == "- Faster alerts"
        assert runner.invoke(cli.app, args).exit_code == 0 and one.call_count == 1  # no resend
    assert set(json.loads(cli.UPDATES_SENT_PATH.read_text())) == {f"{today}-new.md"}
