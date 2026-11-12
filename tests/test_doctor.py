"""jobhunt doctor: env, database, schema version, key round-trip; never prints secrets."""

from conftest import PG_URL, hook_url
from typer.testing import CliRunner

from jobhunt import cli, crypto, db

runner = CliRunner()


def _env(monkeypatch, db_url, **over):
    vals = {
        "DATABASE_URL": db_url,
        "JOBHUNT_SECRET_KEY": crypto.generate_secret_key(),
        "ADMIN_WEBHOOK_URL": hook_url(9),
    }
    vals.update(over)
    for k, v in vals.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    return vals


def _good_db(tmp_path):
    path = str(tmp_path / "d.db")
    db.connect(path).close()
    return path


def test_all_pass(monkeypatch, tmp_path):
    vals = _env(monkeypatch, _good_db(tmp_path))
    r = runner.invoke(cli.app, ["doctor"])
    assert r.exit_code == 0, r.output
    assert "FAIL" not in r.output
    for secret in vals.values():
        assert secret not in r.output
    assert "SUPERSECRET" not in r.output


def test_missing_env_vars_fail(monkeypatch, tmp_path):
    _env(monkeypatch, None, JOBHUNT_SECRET_KEY=None, ADMIN_WEBHOOK_URL=None)
    r = runner.invoke(cli.app, ["doctor"])
    assert r.exit_code == 1
    assert r.output.count("missing") >= 3


def test_unreachable_db_fails_without_leaking_url(monkeypatch, tmp_path):
    url = "postgresql://user:hunter2pw@127.0.0.1:1/nodb?connect_timeout=2"
    _env(monkeypatch, url)
    r = runner.invoke(cli.app, ["doctor"])
    assert r.exit_code == 1
    assert "hunter2pw" not in r.output and "127.0.0.1" not in r.output


def test_uninitialised_schema_fails(monkeypatch, tmp_path):
    import sqlite3

    path = str(tmp_path / "empty.db")
    sqlite3.connect(path).close()
    _env(monkeypatch, path)
    r = runner.invoke(cli.app, ["doctor"])
    assert r.exit_code == 1 and "db init" in r.output


def test_old_schema_version_fails(monkeypatch, tmp_path):
    path = _good_db(tmp_path)
    c = db.connect(path, ensure_schema=False)
    c.execute("UPDATE schema_version SET version = 1")
    c.commit()
    c.close()
    _env(monkeypatch, path)
    r = runner.invoke(cli.app, ["doctor"])
    assert r.exit_code == 1 and "need 4" in r.output


def test_bad_secret_key_fails(monkeypatch, tmp_path):
    _env(monkeypatch, _good_db(tmp_path), JOBHUNT_SECRET_KEY="not-a-key!!")
    r = runner.invoke(cli.app, ["doctor"])
    assert r.exit_code == 1
    assert "not-a-key" not in r.output


def test_bad_admin_webhook_fails(monkeypatch, tmp_path):
    _env(monkeypatch, _good_db(tmp_path), ADMIN_WEBHOOK_URL="https://example.com/x")
    r = runner.invoke(cli.app, ["doctor"])
    assert r.exit_code == 1 and "example.com" not in r.output


def test_postgres_backend(monkeypatch):
    import pytest

    if not PG_URL:
        pytest.skip("JOBHUNT_TEST_DATABASE_URL not set")
    db.connect(PG_URL).close()
    _env(monkeypatch, PG_URL)
    r = runner.invoke(cli.app, ["doctor"])
    assert r.exit_code == 0, r.output


def test_missing_company_endpoints_fail(monkeypatch, tmp_path):
    _env(monkeypatch, _good_db(tmp_path))
    monkeypatch.chdir(tmp_path)  # no config.yaml / companies.resolved.yaml here
    r = runner.invoke(cli.app, ["doctor"])
    assert r.exit_code == 1 and "company endpoints" in r.output
