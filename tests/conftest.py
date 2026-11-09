"""Shared fixtures: `conn` runs each test on SQLite and (if configured) Postgres."""

import os

import pytest

from jobhunt import crypto, db

PG_URL = os.environ.get("JOBHUNT_TEST_DATABASE_URL")


@pytest.fixture(scope="session")
def secret_key():
    return crypto.generate_secret_key()


@pytest.fixture(autouse=True)
def _env(monkeypatch, secret_key):
    monkeypatch.setenv("JOBHUNT_SECRET_KEY", secret_key)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("ADMIN_WEBHOOK_URL", raising=False)


@pytest.fixture(
    params=[
        "sqlite",
        pytest.param(
            "postgres",
            marks=pytest.mark.skipif(not PG_URL, reason="JOBHUNT_TEST_DATABASE_URL not set"),
        ),
    ]
)
def conn(request, tmp_path):
    if request.param == "postgres":
        c = db.connect(PG_URL)
        db.truncate_all(c)
    else:
        c = db.connect(tmp_path / "t.db")
    yield c
    c.close()


def hook_url(n: int = 1, host: str = "discord.com") -> str:
    """A syntactically valid (fake) Discord webhook URL."""
    return f"https://{host}/api/webhooks/{str(n).zfill(18)}/{chr(64 + n) * 64}"


def make_sub(conn, n=1, *, status="active", countries=("CA", "US"), levels=("internship", "coop"),
             activated_at=None):
    """Insert a subscriber for fake webhook #n; returns its id."""
    from jobhunt import webhooks

    url = webhooks.normalize(hook_url(n))
    sid = db.add_subscriber(conn, crypto.webhook_hash(url), crypto.encrypt_url(url),
                            list(countries), list(levels), status=status)
    if activated_at is not None:
        conn.execute("UPDATE subscribers SET activated_at=? WHERE id=?",
                     (db.to_iso(activated_at), sid))
        conn.commit()
    return sid
