"""v2 database: SQLite (default, local/tests) or Postgres (DATABASE_URL), one API.

Portable SQL only: TEXT / INTEGER columns, arrays as JSON text, booleans as INTEGER,
timestamps as fixed-width ISO-8601 UTC TEXT (string comparison == time order).
Only the DDL differs per backend. Queries are written with `?` placeholders; the
Postgres wrapper translates them to `%s`.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .classify import Classification
from .models import Job

DEFAULT_DB_PATH = Path("jobhunt_v2.db")
SCHEMA_VERSION = 4
PENDING_RETRY_AFTER = timedelta(hours=1)
MAX_FAIL_CYCLES = 5
RETENTION_DAYS = 90

_TABLES = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS postings (
    uid           TEXT PRIMARY KEY,
    source        TEXT NOT NULL,
    company       TEXT NOT NULL,
    title         TEXT NOT NULL,
    url           TEXT NOT NULL,
    location_raw  TEXT,
    posted_at     TEXT,
    posted_note   TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_postings_first_seen ON postings(first_seen_at);

CREATE TABLE IF NOT EXISTS posting_tags (
    uid             TEXT PRIMARY KEY REFERENCES postings(uid) ON DELETE CASCADE,
    eligible        INTEGER NOT NULL,
    reject_reason   TEXT,
    categories      TEXT NOT NULL DEFAULT '[]',
    level           TEXT CHECK (level IS NULL OR level IN ('internship', 'coop')),
    countries       TEXT NOT NULL DEFAULT '[]',
    cities          TEXT NOT NULL DEFAULT '[]',
    remote          INTEGER NOT NULL DEFAULT 0,
    location_opaque INTEGER NOT NULL DEFAULT 0,
    classified_at   TEXT NOT NULL,
    rules_version   INTEGER NOT NULL,
    terms           TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS subscribers (
    id                  {ID_COL},
    webhook_hash        TEXT NOT NULL UNIQUE,
    webhook_enc         TEXT NOT NULL,
    manage_token_hash   TEXT UNIQUE,
    status              TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'active', 'paused', 'disabled')),
    disabled_reason     TEXT,
    countries           TEXT NOT NULL DEFAULT '["CA","US"]',
    levels              TEXT NOT NULL DEFAULT '["internship","coop"]',
    created_at          TEXT NOT NULL,
    activated_at        TEXT,
    last_ok_at          TEXT,
    fail_count          INTEGER NOT NULL DEFAULT 0,
    verify_count        INTEGER NOT NULL DEFAULT 0,
    verify_window_start TEXT,
    grad_date           TEXT
);

CREATE TABLE IF NOT EXISTS deliveries (
    subscriber_id {SUB_FK} NOT NULL REFERENCES subscribers(id) ON DELETE CASCADE,
    uid           TEXT NOT NULL REFERENCES postings(uid) ON DELETE CASCADE,
    status        TEXT NOT NULL CHECK (status IN ('pending', 'sent', 'failed')),
    attempts      INTEGER NOT NULL DEFAULT 0,
    updated_at    TEXT NOT NULL,
    sent_at       TEXT,
    PRIMARY KEY (subscriber_id, uid)
);
CREATE INDEX IF NOT EXISTS idx_deliveries_sent_at ON deliveries(sent_at);

CREATE TABLE IF NOT EXISTS rate_limits (
    key_hash     TEXT NOT NULL,
    window_start TEXT NOT NULL,
    count        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (key_hash, window_start)
);
"""

_SCHEMA_SQLITE = _TABLES.format(ID_COL="INTEGER PRIMARY KEY", SUB_FK="INTEGER")
_SCHEMA_PG = _TABLES.format(
    ID_COL="BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY", SUB_FK="BIGINT"
)
DATA_TABLES = ("rate_limits", "deliveries", "subscribers", "posting_tags", "postings")


def utcnow_iso() -> str:
    """Fixed-width ISO-8601 UTC timestamp (microseconds always present)."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def to_iso(dt: datetime) -> str:
    """Normalise any datetime (naive = UTC) to the fixed-width UTC format."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


# ---- connections ----------------------------------------------------------


class Row(tuple):
    """Result row addressable by index or column name (Postgres side)."""

    _names: tuple[str, ...] = ()

    def __getitem__(self, key):
        if isinstance(key, str):
            try:
                return tuple.__getitem__(self, self._names.index(key))
            except ValueError:
                raise KeyError(key) from None
        return tuple.__getitem__(self, key)

    def keys(self):
        return list(self._names)


def _pg_row_factory(cursor):
    names = tuple(c.name for c in cursor.description) if cursor.description else ()
    cls = type("Row", (Row,), {"_names": names})
    return lambda values: cls(values)


class Conn:
    """Thin wrapper giving psycopg the sqlite3 calling convention (`?`, execute on conn)."""

    def __init__(self, raw, backend: str):
        self.raw = raw
        self.backend = backend

    @property
    def is_postgres(self) -> bool:
        return self.backend == "postgres"

    def execute(self, sql: str, params=()):
        if self.is_postgres:
            sql = sql.replace("?", "%s")
        return self.raw.execute(sql, params)

    def executemany(self, sql: str, seq):
        if self.is_postgres:
            sql = sql.replace("?", "%s")
            cur = self.raw.cursor()
            cur.executemany(sql, seq)
            return cur
        return self.raw.executemany(sql, seq)

    def commit(self) -> None:
        self.raw.commit()

    def rollback(self) -> None:
        self.raw.rollback()

    def close(self) -> None:
        self.raw.close()


def is_postgres_url(target) -> bool:
    return isinstance(target, str) and target.startswith(("postgres://", "postgresql://"))


def connect(target: Path | str | None = None, *, ensure_schema: bool = True) -> Conn:
    """Open the database and ensure the schema.

    `target`: a postgres URL, a SQLite path, or None (DATABASE_URL if set, else the
    default SQLite file). `ensure_schema=False` skips the schema check (the web app
    does it once per process).
    """
    if target is None:
        target = os.environ.get("DATABASE_URL") or DEFAULT_DB_PATH
    if is_postgres_url(target):
        import psycopg

        # prepare_threshold=None: Supabase's transaction pooler has no prepared statements.
        raw = psycopg.connect(str(target), prepare_threshold=None, row_factory=_pg_row_factory)
        conn = Conn(raw, "postgres")
    else:
        raw = sqlite3.connect(str(target), timeout=30)
        raw.row_factory = sqlite3.Row
        raw.execute("PRAGMA busy_timeout=30000")
        raw.execute("PRAGMA journal_mode=WAL")
        raw.execute("PRAGMA foreign_keys=ON")
        conn = Conn(raw, "sqlite")
    if ensure_schema:
        init_schema(conn)
    return conn


def init_schema(conn: Conn) -> None:
    """Create tables if missing and record the schema version. Idempotent."""
    if conn.is_postgres:
        have = conn.execute("SELECT to_regclass('schema_version') IS NOT NULL").fetchone()[0]
        if have and (schema_version(conn) or 0) >= SCHEMA_VERSION:
            conn.rollback()
            return
        for stmt in _SCHEMA_PG.split(";"):
            if stmt.strip():
                conn.execute(stmt)
    else:
        conn.raw.executescript(_SCHEMA_SQLITE)
    if conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 0:
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
    elif schema_version(conn) < SCHEMA_VERSION:
        _migrate(conn)
        conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
    conn.commit()


def _migrate(conn: Conn) -> None:
    """Additive migrations from older schema versions (the CREATEs above are IF NOT EXISTS)."""
    # v2 -> v3: posting_tags.terms (JSON list of "Season YYYY"); old rows default to '[]'.
    if conn.is_postgres:
        conn.execute("ALTER TABLE posting_tags ADD COLUMN IF NOT EXISTS terms TEXT NOT NULL DEFAULT '[]'")
    else:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(posting_tags)").fetchall()}
        if "terms" not in cols:
            conn.execute("ALTER TABLE posting_tags ADD COLUMN terms TEXT NOT NULL DEFAULT '[]'")
    # v3 -> v4: subscribers.grad_date ("YYYY-MM", NULL = no graduation filter).
    if conn.is_postgres:
        conn.execute("ALTER TABLE subscribers ADD COLUMN IF NOT EXISTS grad_date TEXT")
    else:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(subscribers)").fetchall()}
        if "grad_date" not in cols:
            conn.execute("ALTER TABLE subscribers ADD COLUMN grad_date TEXT")


def schema_version(conn: Conn) -> int:
    return conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]


def truncate_all(conn: Conn) -> None:
    """Empty every data table (tests / dev reset)."""
    for t in DATA_TABLES:
        conn.execute(f"DELETE FROM {t}")
    conn.commit()


# ---- postings / tags ------------------------------------------------------


def insert_new_postings(
    conn: Conn, jobs: list[Job], seen_at: str | None = None
) -> list[Job]:
    """Insert postings not yet stored; return only those that were new (input order).

    O(chunks) round trips: SELECT the already-stored uids (500 per query), then one
    executemany for the rest, one commit. ON CONFLICT DO NOTHING keeps a racing writer
    safe; at worst both report the same row as new (scans are flock-serialized anyway).
    """
    seen_at = seen_at or utcnow_iso()
    uids = [j.uid for j in jobs]
    existing: set[str] = set()
    for i in range(0, len(uids), 500):
        chunk = uids[i : i + 500]
        marks = ",".join("?" * len(chunk))
        existing.update(
            r[0] for r in conn.execute(f"SELECT uid FROM postings WHERE uid IN ({marks})", chunk)
        )
    new: list[Job] = []
    seen_new: set[str] = set()
    for job in jobs:
        if job.uid not in existing and job.uid not in seen_new:
            seen_new.add(job.uid)
            new.append(job)
    if new:
        conn.executemany(
            "INSERT INTO postings (uid, source, company, title, url, "
            "location_raw, posted_at, posted_note, first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT (uid) DO NOTHING",
            [
                (
                    j.uid, j.source, j.company, j.title, j.url, j.location,
                    to_iso(j.posted_at) if j.posted_at else None, j.posted_note, seen_at, seen_at,
                )
                for j in new
            ],
        )
    conn.commit()
    return new


def posting_known(conn: Conn, uid: str, urls: list[str]) -> bool:
    """True if a posting has this uid or any of these exact urls (no index on url: small table)."""
    marks = ",".join("?" * len(urls))
    return conn.execute(
        f"SELECT 1 FROM postings WHERE uid = ? OR url IN ({marks}) LIMIT 1", (uid, *urls)
    ).fetchone() is not None


def insert_posting(conn: Conn, job: Job, seen_at: str | None = None) -> bool:
    """Insert one posting; False if its uid already exists (lost a race)."""
    seen_at = seen_at or utcnow_iso()
    cur = conn.execute(
        "INSERT INTO postings (uid, source, company, title, url, location_raw, posted_at, "
        "posted_note, first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT (uid) DO NOTHING",
        (job.uid, job.source, job.company, job.title, job.url, job.location,
         to_iso(job.posted_at) if job.posted_at else None, job.posted_note, seen_at, seen_at),
    )
    conn.commit()
    return cur.rowcount == 1


def untagged_postings(conn: Conn, limit: int) -> list[Job]:
    """Oldest postings with no posting_tags row (e.g. pushed by POST /ingest/posting)."""
    rows = conn.execute(
        "SELECT p.* FROM postings p LEFT JOIN posting_tags t ON t.uid = p.uid "
        "WHERE t.uid IS NULL ORDER BY p.first_seen_at LIMIT ?", (limit,)
    ).fetchall()
    return [job_from_row(r) for r in rows]


def update_location(conn: Conn, uid: str, location: str) -> None:
    """Replace a stored posting's raw location (e.g. a resolved "2 Locations")."""
    conn.execute("UPDATE postings SET location_raw=? WHERE uid=?", (location, uid))
    conn.commit()


def touch_postings(conn: Conn, uids: list[str], now: datetime | None = None) -> None:
    """Mark postings still on the board (at most one write per posting per day)."""
    now = now or datetime.now(timezone.utc)
    stamp, day_ago = to_iso(now), to_iso(now - timedelta(days=1))
    for i in range(0, len(uids), 500):
        chunk = uids[i : i + 500]
        marks = ",".join("?" * len(chunk))
        conn.execute(
            f"UPDATE postings SET last_seen_at=? WHERE uid IN ({marks}) "
            "AND (last_seen_at IS NULL OR last_seen_at < ?)",
            (stamp, *chunk, day_ago),
        )
    conn.commit()


def upsert_tags_many(
    conn: Conn, items: list[tuple[str, Classification]], classified_at: str | None = None
) -> None:
    """Upsert many (uid, Classification) rows: one executemany, one commit."""
    if not items:
        return
    stamp = classified_at or utcnow_iso()
    conn.executemany(
        "INSERT INTO posting_tags (uid, eligible, reject_reason, categories, level, countries, "
        "cities, remote, location_opaque, classified_at, rules_version, terms) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(uid) DO UPDATE SET eligible=excluded.eligible, "
        "reject_reason=excluded.reject_reason, categories=excluded.categories, "
        "level=excluded.level, countries=excluded.countries, cities=excluded.cities, "
        "remote=excluded.remote, location_opaque=excluded.location_opaque, "
        "classified_at=excluded.classified_at, rules_version=excluded.rules_version, "
        "terms=excluded.terms",
        [
            (
                uid, int(c.eligible), c.reject_reason, json.dumps(c.categories), c.level,
                json.dumps(c.countries), json.dumps(c.cities), int(c.remote),
                int(c.location_opaque), stamp, c.rules_version, json.dumps(c.terms),
            )
            for uid, c in items
        ],
    )
    conn.commit()


def upsert_tags(
    conn: Conn, uid: str, cls: Classification, classified_at: str | None = None
) -> None:
    upsert_tags_many(conn, [(uid, cls)], classified_at)


def tags_from_row(row) -> Classification:
    """Rebuild a Classification from a posting_tags row (or a join containing its columns)."""
    return Classification(
        eligible=bool(row["eligible"]),
        reject_reason=row["reject_reason"],
        categories=json.loads(row["categories"]),
        level=row["level"],
        countries=json.loads(row["countries"]),
        cities=json.loads(row["cities"]),
        remote=bool(row["remote"]),
        location_opaque=bool(row["location_opaque"]),
        rules_version=row["rules_version"],
        terms=json.loads(row["terms"]),
    )


def job_from_row(row) -> Job:
    """Rebuild a (description-less) Job from a postings row."""
    posted = datetime.fromisoformat(row["posted_at"]) if row["posted_at"] else None
    return Job(
        source=row["source"],
        company=row["company"],
        external_id=row["uid"].split(":", 2)[2],
        title=row["title"],
        location=row["location_raw"],
        url=row["url"],
        posted_at=posted,
        posted_note=row["posted_note"],
    )


# ---- subscribers ----------------------------------------------------------


@dataclass
class Subscriber:
    id: int
    status: str
    countries: list[str]
    levels: list[str]
    webhook_enc: str
    created_at: str
    activated_at: str | None = None
    disabled_reason: str | None = None
    last_ok_at: str | None = None
    fail_count: int = 0
    grad_date: str | None = None


_SUB_COLS = (
    "id, status, countries, levels, webhook_enc, created_at, activated_at, "
    "disabled_reason, last_ok_at, fail_count, grad_date"
)


def _sub_from_row(r) -> Subscriber:
    return Subscriber(
        id=r["id"], status=r["status"], countries=json.loads(r["countries"]),
        levels=json.loads(r["levels"]), webhook_enc=r["webhook_enc"],
        created_at=r["created_at"], activated_at=r["activated_at"],
        disabled_reason=r["disabled_reason"], last_ok_at=r["last_ok_at"],
        fail_count=r["fail_count"], grad_date=r["grad_date"],
    )


def add_subscriber(
    conn: Conn,
    webhook_hash: str,
    webhook_enc: str,
    countries: list[str],
    levels: list[str],
    *,
    status: str = "pending",
    manage_token_hash: str | None = None,
    grad_date: str | None = None,
) -> int | None:
    """Insert a subscriber. Returns the new id, or None if that webhook already exists."""
    now = utcnow_iso()
    row = conn.execute(
        "INSERT INTO subscribers (webhook_hash, webhook_enc, manage_token_hash, status, "
        "countries, levels, created_at, activated_at, grad_date) VALUES (?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT (webhook_hash) DO NOTHING RETURNING id",
        (
            webhook_hash, webhook_enc, manage_token_hash, status, json.dumps(countries),
            json.dumps(levels), now, now if status == "active" else None, grad_date,
        ),
    ).fetchone()
    conn.commit()
    return row[0] if row else None


def get_subscriber(conn: Conn, sub_id: int) -> Subscriber | None:
    r = conn.execute(f"SELECT {_SUB_COLS} FROM subscribers WHERE id=?", (sub_id,)).fetchone()
    return _sub_from_row(r) if r else None


def list_subscribers(conn: Conn, status: str | None = None) -> list[Subscriber]:
    sql = f"SELECT {_SUB_COLS} FROM subscribers " + ("WHERE status=? " if status else "")
    rows = conn.execute(sql + "ORDER BY id", (status,) if status else ()).fetchall()
    return [_sub_from_row(r) for r in rows]


def list_active_subscribers(conn: Conn) -> list[Subscriber]:
    return list_subscribers(conn, "active")


def activate_subscriber(conn: Conn, sub_id: int) -> bool:
    cur = conn.execute(
        "UPDATE subscribers SET status='active', activated_at=?, fail_count=0, "
        "disabled_reason=NULL WHERE id=? AND status IN ('pending','paused')",
        (utcnow_iso(), sub_id),
    )
    conn.commit()
    return cur.rowcount == 1


def disable_subscriber(conn: Conn, sub_id: int, reason: str) -> None:
    conn.execute(
        "UPDATE subscribers SET status='disabled', disabled_reason=? WHERE id=?",
        (reason, sub_id),
    )
    conn.commit()


def get_subscriber_by_hash(conn: Conn, webhook_hash: str) -> Subscriber | None:
    r = conn.execute(
        f"SELECT {_SUB_COLS} FROM subscribers WHERE webhook_hash=?", (webhook_hash,)
    ).fetchone()
    return _sub_from_row(r) if r else None


def get_subscriber_by_token(conn: Conn, manage_token_hash: str) -> Subscriber | None:
    r = conn.execute(
        f"SELECT {_SUB_COLS} FROM subscribers WHERE manage_token_hash=?", (manage_token_hash,)
    ).fetchone()
    return _sub_from_row(r) if r else None


def rotate_manage_token(conn: Conn, sub_id: int, manage_token_hash: str) -> None:
    """Replace the manage token (the old one stops working). A disabled subscriber whose
    webhook verified again goes back to pending so it can be re-activated."""
    conn.execute(
        "UPDATE subscribers SET manage_token_hash=?, "
        "disabled_reason=CASE WHEN status='disabled' THEN NULL ELSE disabled_reason END, "
        "fail_count=CASE WHEN status='disabled' THEN 0 ELSE fail_count END, "
        "status=CASE WHEN status='disabled' THEN 'pending' ELSE status END WHERE id=?",
        (manage_token_hash, sub_id),
    )
    conn.commit()


def set_prefs(conn: Conn, sub_id: int, countries: list[str], levels: list[str]) -> None:
    conn.execute(
        "UPDATE subscribers SET countries=?, levels=? WHERE id=?",
        (json.dumps(countries), json.dumps(levels), sub_id),
    )
    conn.commit()


def set_grad_date(conn: Conn, sub_id: int, grad_date: str | None) -> None:
    conn.execute("UPDATE subscribers SET grad_date=? WHERE id=?", (grad_date, sub_id))
    conn.commit()


def pause_subscriber(conn: Conn, sub_id: int) -> bool:
    cur = conn.execute(
        "UPDATE subscribers SET status='paused' WHERE id=? AND status='active'", (sub_id,)
    )
    conn.commit()
    return cur.rowcount == 1


def take_verify_slot(
    conn: Conn, sub_id: int, limit: int = 3, window: timedelta = timedelta(hours=24)
) -> bool:
    """Atomically count one channel message for this subscriber; False if over `limit` per window."""
    now = datetime.now(timezone.utc)
    cutoff, now_iso = to_iso(now - window), to_iso(now)
    expired = "(verify_window_start IS NULL OR verify_window_start < ?)"
    cur = conn.execute(
        f"UPDATE subscribers SET verify_count=CASE WHEN {expired} THEN 1 ELSE verify_count+1 END, "
        f"verify_window_start=CASE WHEN {expired} THEN ? ELSE verify_window_start END "
        f"WHERE id=? AND ({expired} OR verify_count < ?)",
        (cutoff, cutoff, now_iso, sub_id, cutoff, limit),
    )
    conn.commit()
    return cur.rowcount == 1


def refund_verify_slot(conn: Conn, sub_id: int) -> None:
    """Give back one slot after a channel message failed to send."""
    conn.execute(
        "UPDATE subscribers SET verify_count=CASE WHEN verify_count>0 THEN verify_count-1 "
        "ELSE 0 END WHERE id=?",
        (sub_id,),
    )
    conn.commit()


def rate_limit_hit(conn: Conn, key_hash: str, limit: int) -> bool:
    """Count one hit in the current hour (fixed window); True if still within `limit`."""
    now = datetime.now(timezone.utc)
    start = to_iso(now.replace(minute=0, second=0, microsecond=0))
    conn.execute("DELETE FROM rate_limits WHERE window_start < ?", (to_iso(now - timedelta(hours=2)),))
    n = conn.execute(
        "INSERT INTO rate_limits (key_hash, window_start, count) VALUES (?,?,1) "
        "ON CONFLICT (key_hash, window_start) DO UPDATE SET count = rate_limits.count + 1 "
        "RETURNING count",
        (key_hash, start),
    ).fetchone()[0]
    conn.commit()
    return n <= limit


def remove_subscriber(conn: Conn, sub_id: int) -> bool:
    cur = conn.execute("DELETE FROM subscribers WHERE id=?", (sub_id,))
    conn.commit()
    return cur.rowcount == 1


def subscriber_ok(conn: Conn, sub_id: int) -> None:
    conn.execute(
        "UPDATE subscribers SET last_ok_at=?, fail_count=0 WHERE id=?", (utcnow_iso(), sub_id)
    )
    conn.commit()


def subscriber_failed(conn: Conn, sub_id: int, max_fails: int = MAX_FAIL_CYCLES) -> bool:
    """Count one failed cycle; disable at `max_fails` consecutive. True if now disabled."""
    conn.execute("UPDATE subscribers SET fail_count = fail_count + 1 WHERE id=?", (sub_id,))
    n = conn.execute("SELECT fail_count FROM subscribers WHERE id=?", (sub_id,)).fetchone()[0]
    conn.commit()
    if n >= max_fails:
        disable_subscriber(conn, sub_id, f"{n} consecutive failed delivery cycles")
        return True
    return False


# ---- candidates + deliveries ----------------------------------------------


def candidate_rows(conn: Conn, activated_at: str) -> list:
    """Eligible postings first seen at/after `activated_at`, joined with their tags."""
    return conn.execute(
        "SELECT p.*, t.eligible, t.reject_reason, t.categories, t.level, t.countries, "
        "t.cities, t.remote, t.location_opaque, t.rules_version, t.terms "
        "FROM postings p JOIN posting_tags t ON t.uid = p.uid "
        "WHERE t.eligible = 1 AND p.first_seen_at >= ?",
        (activated_at,),
    ).fetchall()


def _stale_cutoff(now: datetime | None = None) -> str:
    return to_iso((now or datetime.now(timezone.utc)) - PENDING_RETRY_AFTER)


def delivery_status(conn: Conn, sub_id: int, uid: str) -> str | None:
    row = conn.execute(
        "SELECT status FROM deliveries WHERE subscriber_id=? AND uid=?", (sub_id, uid)
    ).fetchone()
    return row[0] if row else None


def blocked_uids(conn: Conn, sub_id: int, now: datetime | None = None) -> set[str]:
    """Uids already sent, or in flight (pending < 1h). Failed / stale pending may retry."""
    rows = conn.execute(
        "SELECT uid FROM deliveries WHERE subscriber_id=? AND "
        "(status='sent' OR (status='pending' AND updated_at >= ?))",
        (sub_id, _stale_cutoff(now)),
    ).fetchall()
    return {r[0] for r in rows}


def reserve_delivery(
    conn: Conn, sub_id: int, uid: str, now: datetime | None = None
) -> bool:
    """Atomically claim (subscriber, uid). True if this caller now owns the send.

    New row -> 'pending'. An existing 'failed' row, or a 'pending' row not touched for
    over an hour (crashed run), is re-claimed. 'sent' and fresh 'pending' never are.
    """
    ts = to_iso(now) if now else utcnow_iso()
    cur = conn.execute(
        "INSERT INTO deliveries (subscriber_id, uid, status, attempts, updated_at) "
        "VALUES (?,?,'pending',1,?) ON CONFLICT (subscriber_id, uid) DO NOTHING",
        (sub_id, uid, ts),
    )
    reserved = cur.rowcount == 1
    if not reserved:
        cur = conn.execute(
            "UPDATE deliveries SET status='pending', attempts = attempts + 1, updated_at=? "
            "WHERE subscriber_id=? AND uid=? AND "
            "(status='failed' OR (status='pending' AND updated_at < ?))",
            (ts, sub_id, uid, _stale_cutoff(now)),
        )
        reserved = cur.rowcount == 1
    conn.commit()
    return reserved


def mark_sent(conn: Conn, sub_id: int, uids: list[str]) -> None:
    now = utcnow_iso()
    conn.executemany(
        "UPDATE deliveries SET status='sent', sent_at=?, updated_at=? "
        "WHERE subscriber_id=? AND uid=?",
        [(now, now, sub_id, u) for u in uids],
    )
    conn.commit()


def mark_failed(conn: Conn, sub_id: int, uids: list[str]) -> None:
    now = utcnow_iso()
    conn.executemany(
        "UPDATE deliveries SET status='failed', updated_at=? WHERE subscriber_id=? AND uid=?",
        [(now, sub_id, u) for u in uids],
    )
    conn.commit()


# ---- retention + stats ----------------------------------------------------


def prune_postings(conn: Conn, days: int = RETENTION_DAYS, now: datetime | None = None) -> int:
    """Delete postings first seen more than `days` ago AND not seen on the board for `days`
    (tags and deliveries cascade). Still-open postings are kept so they are never re-alerted."""
    cutoff = to_iso((now or datetime.now(timezone.utc)) - timedelta(days=days))
    cur = conn.execute(
        "DELETE FROM postings WHERE first_seen_at < ? "
        "AND (last_seen_at IS NULL OR last_seen_at < ?)",
        (cutoff, cutoff),
    )
    conn.commit()
    return cur.rowcount


PENDING_EXPIRY_DAYS = 7


def prune_pending_subscribers(
    conn: Conn, days: int = PENDING_EXPIRY_DAYS, now: datetime | None = None
) -> int:
    """Delete subscribers still 'pending' (never activated) created more than `days` ago."""
    cutoff = to_iso((now or datetime.now(timezone.utc)) - timedelta(days=days))
    cur = conn.execute(
        "DELETE FROM subscribers WHERE status='pending' AND created_at < ?", (cutoff,)
    )
    conn.commit()
    return cur.rowcount


def stats_24h(conn: Conn) -> dict[str, int]:
    """Heartbeat numbers: subscriber growth plus postings and deliveries in the last 24h."""
    since = to_iso(datetime.now(timezone.utc) - timedelta(days=1))
    q = conn.execute

    def one(sql: str, *args) -> int:
        return q(sql, args).fetchone()[0]

    by_status = subscriber_status_counts(conn)
    return {
        "active_subscribers": by_status["active"],
        "pending_subscribers": by_status["pending"],
        "disabled_subscribers": by_status["disabled"],
        "signups_24h": one("SELECT COUNT(*) FROM subscribers WHERE created_at >= ?", since),
        "activations_24h": one("SELECT COUNT(*) FROM subscribers WHERE activated_at >= ?", since),
        "postings_24h": one("SELECT COUNT(*) FROM postings WHERE first_seen_at >= ?", since),
        "sent_24h": one(
            "SELECT COUNT(*) FROM deliveries WHERE status='sent' AND sent_at >= ?", since),
        "failed_24h": one(
            "SELECT COUNT(*) FROM deliveries WHERE status='failed' AND updated_at >= ?", since),
    }


SUBSCRIBER_STATUSES = ("active", "pending", "paused", "disabled")


def subscriber_status_counts(conn: Conn) -> dict[str, int]:
    """Subscriber count per status (every status present, zero if none)."""
    out = dict.fromkeys(SUBSCRIBER_STATUSES, 0)
    for status, n in conn.execute("SELECT status, COUNT(*) FROM subscribers GROUP BY status"):
        out[status] = n
    return out


def daily_activity(conn: Conn, days: int = 14, now: datetime | None = None) -> list[dict]:
    """Per UTC day, oldest first, for the last `days` days (today included; zeros filled).

    Timestamps are fixed-width ISO strings, so substr(col,1,10) is the UTC day on both backends.
    """
    now = now or datetime.now(timezone.utc)
    day_list = [(now - timedelta(days=i)).date().isoformat() for i in range(days - 1, -1, -1)]
    since = day_list[0]
    rows = {d: {"day": d, "signups": 0, "activations": 0, "sent": 0, "failed": 0, "postings": 0}
            for d in day_list}
    queries = (
        ("signups", "SELECT substr(created_at,1,10), COUNT(*) FROM subscribers "
                    "WHERE created_at >= ? GROUP BY 1"),
        ("activations", "SELECT substr(activated_at,1,10), COUNT(*) FROM subscribers "
                        "WHERE activated_at >= ? GROUP BY 1"),
        ("sent", "SELECT substr(sent_at,1,10), COUNT(*) FROM deliveries "
                 "WHERE status='sent' AND sent_at >= ? GROUP BY 1"),
        ("failed", "SELECT substr(updated_at,1,10), COUNT(*) FROM deliveries "
                   "WHERE status='failed' AND updated_at >= ? GROUP BY 1"),
        ("postings", "SELECT substr(first_seen_at,1,10), COUNT(*) FROM postings "
                     "WHERE first_seen_at >= ? GROUP BY 1"),
    )
    for key, sql in queries:
        for day, n in conn.execute(sql, (since,)).fetchall():
            if day in rows:
                rows[day][key] = n
    return [rows[d] for d in day_list]


def totals(conn: Conn) -> dict[str, int]:
    """All-time totals for the admin dashboard."""
    q = conn.execute
    return {
        "postings": q("SELECT COUNT(*) FROM postings").fetchone()[0],
        "sent": q("SELECT COUNT(*) FROM deliveries WHERE status='sent'").fetchone()[0],
    }
