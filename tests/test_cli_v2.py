"""CLI: user seed-owner / list, scan without LLM, list-matches (offline)."""

from typer.testing import CliRunner

from jobhunt import cli, db

runner = CliRunner()


def test_seed_owner_idempotent_and_list(tmp_path):
    path = str(tmp_path / "v2.db")
    r = runner.invoke(cli.app, ["user", "seed-owner", "--db", path])
    assert r.exit_code == 0, r.output
    r = runner.invoke(cli.app, ["user", "seed-owner", "--db", path])
    assert r.exit_code == 0 and "already exists" in r.output
    conn = db.connect(path)
    users = db.list_users(conn)
    conn.close()
    assert len(users) == 1
    u = users[0]
    assert u.id == 1 and u.status == "active"
    assert u.destination.kind == "webhook_env" and u.destination.ref == "V2_DISCORD_WEBHOOK_URL"
    assert u.prefs.levels == ["internship", "coop"] and u.prefs.countries == ["CA", "US"]
    assert u.prefs.remote_ok and "Google" in u.prefs.favorite_companies
    out = runner.invoke(cli.app, ["user", "list", "--db", path]).output
    assert "active" in out and "Users" in out


def test_scan_has_no_llm_surface():
    import inspect

    src = inspect.getsource(cli.scan)
    for banned in ("llm_match", "BudgetGuard", "use_llm_filter", "ANTHROPIC", "get_api_key"):
        assert banned not in src
    from jobhunt import matcher

    assert not hasattr(matcher, "llm_match")


def test_default_db_is_v2():
    import typer.main

    cmd = typer.main.get_command(cli.app)
    for name in ("scan", "heartbeat", "list-matches"):
        opt = next(p for p in cmd.commands[name].params if p.name == "db_path")
        assert opt.default == "jobhunt_v2.db"
