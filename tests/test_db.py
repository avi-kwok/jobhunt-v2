"""v2 schema + data-access helpers."""

import sqlite3

import pytest

from jobhunt import db
from jobhunt.classify import Classification
from jobhunt.models import Job


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    yield c
    c.close()


def _job(i, **kw):
    return Job(source="gh", company="Acme", external_id=str(i), title=f"SWE Intern {i}",
               url=f"https://x/{i}", location="Toronto, ON", **kw)


def test_pragmas_and_version(conn):
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
    assert db.schema_version(conn) == 1
    db.connect(conn.execute("PRAGMA database_list").fetchone()[2]).close()  # re-open is idempotent
    assert conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 1


def test_default_path_is_v2():
    assert db.DEFAULT_DB_PATH.name == "jobhunt_v2.db"


def test_insert_new_postings_returns_only_new(conn):
    a, b = _job(1), _job(2)
    assert [j.uid for j in db.insert_new_postings(conn, [a])] == [a.uid]
    assert [j.uid for j in db.insert_new_postings(conn, [a, b])] == [b.uid]
    assert db.insert_new_postings(conn, [a, b]) == []


def test_upsert_tags_roundtrip_and_update(conn):
    j = _job(1)
    db.insert_new_postings(conn, [j])
    db.upsert_tags(conn, j.uid, Classification(eligible=False, reject_reason="x"))
    db.upsert_tags(conn, j.uid, Classification(
        eligible=True, categories=["swe"], level="coop", countries=["CA"], cities=["toronto"],
        remote=True))
    row = conn.execute("SELECT * FROM posting_tags").fetchall()
    assert len(row) == 1
    t = db.tags_from_row(row[0])
    assert t.eligible and t.level == "coop" and t.countries == ["CA"] and t.remote


def test_level_check_constraint(conn):
    j = _job(1)
    db.insert_new_postings(conn, [j])
    with pytest.raises(sqlite3.IntegrityError):
        db.upsert_tags(conn, j.uid, Classification(level="fulltime"))


def test_cascade_delete(conn):
    j = _job(1)
    db.insert_new_postings(conn, [j])
    db.upsert_tags(conn, j.uid, Classification(eligible=True))
    uid = db.create_user(conn, db.Prefs(), "webhook_env", "X")
    db.reserve_delivery(conn, uid, j.uid)
    conn.execute("DELETE FROM postings WHERE uid=?", (j.uid,))
    assert conn.execute("SELECT COUNT(*) FROM posting_tags").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] == 0
    conn.execute("DELETE FROM users WHERE id=?", (uid,))
    for t in ("prefs", "destinations"):
        assert conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 0


def test_users_and_listing(conn):
    p = db.Prefs(levels=["coop"], favorite_companies=["Acme"], cities=["toronto"])
    u1 = db.create_user(conn, p, "webhook_env", "ENV_A", discord_user_id="123")
    u2 = db.create_user(conn, db.Prefs(), "webhook_env", "ENV_B")
    db.disable_user(conn, u2, "gone")
    active = db.list_active_users(conn)
    assert [u.id for u in active] == [u1]
    u = active[0]
    assert u.prefs.levels == ["coop"] and u.prefs.cities == ["toronto"]
    assert u.destination.ref == "ENV_A" and u.destination.kind == "webhook_env"
    assert [x.id for x in db.list_users(conn)] == [u1, u2]
    assert db.find_user_by_destination(conn, "webhook_env", "ENV_B") == u2
    with pytest.raises(sqlite3.IntegrityError):
        db.create_user(conn, db.Prefs(), "webhook_env", "ENV_C", discord_user_id="123")


def test_reserve_delivery_semantics(conn):
    j = _job(1)
    db.insert_new_postings(conn, [j])
    uid = db.create_user(conn, db.Prefs(), "webhook_env", "X")
    assert db.reserve_delivery(conn, uid, j.uid) is True
    assert db.reserve_delivery(conn, uid, j.uid) is False  # pending
    assert db.blocked_uids(conn, uid) == {j.uid}
    db.mark_failed(conn, uid, [j.uid])
    assert db.blocked_uids(conn, uid) == set()
    assert db.reserve_delivery(conn, uid, j.uid) is True  # failed -> retry
    db.mark_sent(conn, uid, [j.uid])
    assert db.reserve_delivery(conn, uid, j.uid) is False
    assert db.delivery_status(conn, uid, j.uid) == "sent"
    assert conn.execute("SELECT sent_at FROM deliveries").fetchone()[0] is not None


def test_stats_24h(conn):
    j = _job(1)
    db.insert_new_postings(conn, [j])
    db.upsert_tags(conn, j.uid, Classification(eligible=True))
    s = db.stats_24h(conn)
    assert (s["total"], s["ingested_24h"], s["eligible_24h"], s["sent_24h"]) == (1, 1, 1, 0)
