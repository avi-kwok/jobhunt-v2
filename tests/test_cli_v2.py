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
    assert r.exit_code == 0 and "version=4" in r.output
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
