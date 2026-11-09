"""v2 schema + data-access helpers, on SQLite and (optionally) Postgres."""

from datetime import datetime, timedelta, timezone

import pytest
from conftest import make_sub

from jobhunt import db
from jobhunt.classify import Classification
from jobhunt.models import Job

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


def _job(i, **kw):
    return Job(source="gh", company="Acme", external_id=str(i), title=f"SWE Intern {i}",
               url=f"https://x/{i}", location="Toronto, ON", **kw)


def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_version_and_idempotent_init(conn):
    assert db.schema_version(conn) == 2
    db.init_schema(conn)
    assert _count(conn, "schema_version") == 1
    assert _count(conn, "rate_limits") == 0


def test_sqlite_pragmas(conn):
    if conn.is_postgres:
        pytest.skip("sqlite only")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30000


def test_old_tables_gone(conn):
    names = {"users", "destinations", "prefs"}
    if conn.is_postgres:
        rows = conn.execute("SELECT table_name FROM information_schema.tables "
                            "WHERE table_schema = current_schema()").fetchall()
    else:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    assert not names & {r[0] for r in rows}


def test_connect_picks_backend_from_database_url(monkeypatch, tmp_path):
    assert db.is_postgres_url("postgresql://h/d") and not db.is_postgres_url("x.db")
    monkeypatch.chdir(tmp_path)
    c = db.connect()
    assert c.backend == "sqlite" and (tmp_path / "jobhunt_v2.db").exists()
    c.close()


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
    rows = conn.execute("SELECT * FROM posting_tags").fetchall()
    assert len(rows) == 1
    t = db.tags_from_row(rows[0])
    assert t.eligible and t.level == "coop" and t.countries == ["CA"] and t.remote


def test_level_check_constraint(conn):
    j = _job(1)
    db.insert_new_postings(conn, [j])
    with pytest.raises(Exception) as e:
        db.upsert_tags(conn, j.uid, Classification(level="fulltime"))
    assert "IntegrityError" in type(e.value).__name__ or "Check" in type(e.value).__name__
    conn.rollback()


def test_cascade_delete(conn):
    j = _job(1)
    db.insert_new_postings(conn, [j])
    db.upsert_tags(conn, j.uid, Classification(eligible=True))
    sid = make_sub(conn)
    db.reserve_delivery(conn, sid, j.uid)
    conn.execute("DELETE FROM postings WHERE uid=?", (j.uid,))
    assert _count(conn, "posting_tags") == 0 and _count(conn, "deliveries") == 0
    j2 = _job(2)
    db.insert_new_postings(conn, [j2])
    db.reserve_delivery(conn, sid, j2.uid)
    assert db.remove_subscriber(conn, sid)
    assert _count(conn, "deliveries") == 0 and _count(conn, "postings") == 1


def test_subscribers_crud(conn):
    s1 = make_sub(conn, 1, countries=("US",), levels=("coop",))
    s2 = make_sub(conn, 2)
    s3 = make_sub(conn, 3, status="pending")
    assert db.add_subscriber(conn, "h", "e", ["CA"], ["coop"]) is not None
    # duplicate webhook hash -> None, no error
    from jobhunt import crypto, webhooks
    dup = crypto.webhook_hash(webhooks.normalize(__import__("conftest").hook_url(1)))
    assert db.add_subscriber(conn, dup, "e2", ["CA"], ["coop"]) is None
    db.disable_subscriber(conn, s2, "gone")
    assert [s.id for s in db.list_active_subscribers(conn)] == [s1]
    got = db.get_subscriber(conn, s1)
    assert got.countries == ["US"] and got.levels == ["coop"] and got.activated_at
    assert db.get_subscriber(conn, s2).disabled_reason == "gone"
    assert db.get_subscriber(conn, s3).activated_at is None
    assert db.activate_subscriber(conn, s3) and not db.activate_subscriber(conn, s3)
    assert db.get_subscriber(conn, s3).status == "active"
    assert not db.activate_subscriber(conn, s2)  # disabled stays disabled


def test_fail_count_rules(conn):
    sid = make_sub(conn)
    for _ in range(4):
        assert db.subscriber_failed(conn, sid) is False
    assert db.get_subscriber(conn, sid).fail_count == 4
    db.subscriber_ok(conn, sid)
    s = db.get_subscriber(conn, sid)
    assert s.fail_count == 0 and s.last_ok_at
    for _ in range(4):
        db.subscriber_failed(conn, sid)
    assert db.subscriber_failed(conn, sid) is True
    assert db.get_subscriber(conn, sid).status == "disabled"


def test_reserve_delivery_semantics(conn):
    j = _job(1)
    db.insert_new_postings(conn, [j])
    sid = make_sub(conn)
    assert db.reserve_delivery(conn, sid, j.uid) is True
    assert db.reserve_delivery(conn, sid, j.uid) is False  # fresh pending
    assert db.blocked_uids(conn, sid) == {j.uid}
    db.mark_failed(conn, sid, [j.uid])
    assert db.blocked_uids(conn, sid) == set()
    assert db.reserve_delivery(conn, sid, j.uid) is True  # failed -> retry
    db.mark_sent(conn, sid, [j.uid])
    assert db.reserve_delivery(conn, sid, j.uid) is False
    assert db.delivery_status(conn, sid, j.uid) == "sent"
    row = conn.execute("SELECT sent_at, attempts FROM deliveries").fetchone()
    assert row["sent_at"] is not None and row["attempts"] == 2


def test_stale_pending_becomes_retryable(conn):
    j = _job(1)
    db.insert_new_postings(conn, [j])
    sid = make_sub(conn)
    assert db.reserve_delivery(conn, sid, j.uid, now=NOW)
    # 59 min later: still in flight
    later = NOW + timedelta(minutes=59)
    assert db.blocked_uids(conn, sid, now=later) == {j.uid}
    assert not db.reserve_delivery(conn, sid, j.uid, now=later)
    # 61 min later: crashed run, claimable again
    later = NOW + timedelta(minutes=61)
    assert db.blocked_uids(conn, sid, now=later) == set()
    assert db.reserve_delivery(conn, sid, j.uid, now=later)
    assert conn.execute("SELECT attempts FROM deliveries").fetchone()[0] == 2


def test_prune_postings_cascades(conn):
    old, new = _job(1), _job(2)
    db.insert_new_postings(conn, [old], seen_at=db.to_iso(NOW - timedelta(days=91)))
    db.insert_new_postings(conn, [new], seen_at=db.to_iso(NOW - timedelta(days=89)))
    for j in (old, new):
        db.upsert_tags(conn, j.uid, Classification(eligible=True))
    sid = make_sub(conn)
    db.reserve_delivery(conn, sid, old.uid)
    db.reserve_delivery(conn, sid, new.uid)
    assert db.prune_postings(conn, now=NOW) == 1
    assert [r[0] for r in conn.execute("SELECT uid FROM postings").fetchall()] == [new.uid]
    assert _count(conn, "posting_tags") == 1 and _count(conn, "deliveries") == 1


def test_stats_24h(conn):
    j1, j2 = _job(1), _job(2)
    db.insert_new_postings(conn, [j1, j2])
    db.insert_new_postings(conn, [_job(3)], seen_at=db.to_iso(datetime.now(timezone.utc) - timedelta(days=3)))
    sid = make_sub(conn)
    make_sub(conn, 2, status="pending")
    db.reserve_delivery(conn, sid, j1.uid)
    db.mark_sent(conn, sid, [j1.uid])
    db.reserve_delivery(conn, sid, j2.uid)
    db.mark_failed(conn, sid, [j2.uid])
    assert db.stats_24h(conn) == {"active_subscribers": 1, "postings_24h": 2,
                                  "sent_24h": 1, "failed_24h": 1}


def test_prune_keeps_postings_still_on_board(conn):
    """Open (re-seen) postings survive the prune, so they are never re-alerted."""
    j = _job(1)
    db.insert_new_postings(conn, [j], seen_at=db.to_iso(NOW - timedelta(days=100)))
    db.touch_postings(conn, [j.uid], now=NOW - timedelta(days=2))
    assert db.prune_postings(conn, now=NOW) == 0
    assert db.insert_new_postings(conn, [j], seen_at=db.to_iso(NOW)) == []
    # gone from the board for 90+ days -> pruned
    assert db.prune_postings(conn, now=NOW + timedelta(days=95)) == 1


def test_init_schema_skip_path_keeps_data(conn):
    sid = make_sub(conn)
    db.init_schema(conn)
    db.init_schema(conn)
    assert db.get_subscriber(conn, sid) is not None
    assert _count(conn, "schema_version") == 1


def test_prune_pending_subscribers(conn):
    old_p, new_p, old_a = make_sub(conn, 1, status="pending"), make_sub(conn, 2, status="pending"), make_sub(conn, 3)
    for sid, days in ((old_p, 8), (new_p, 6), (old_a, 30)):
        conn.execute("UPDATE subscribers SET created_at=? WHERE id=?",
                     (db.to_iso(NOW - timedelta(days=days)), sid))
    conn.commit()
    assert db.prune_pending_subscribers(conn, now=NOW) == 1
    assert sorted(r[0] for r in conn.execute("SELECT id FROM subscribers").fetchall()) == [new_p, old_a]
