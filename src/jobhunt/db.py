"""v2 SQLite schema (WAL) + small data-access helpers.

Portable SQL only: TEXT / INTEGER columns, arrays stored as JSON text,
timestamps as fixed-width ISO-8601 UTC TEXT (so string comparison == time order).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .classify import Classification
from .models import Job

DEFAULT_DB_PATH = Path("jobhunt_v2.db")
SCHEMA_VERSION = 1

_SCHEMA = """
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
    first_seen_at TEXT NOT NULL
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
    rules_version   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY,
    discord_user_id TEXT UNIQUE,
    created_at      TEXT NOT NULL,
    activated_at    TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'paused', 'disabled')),
    disabled_reason TEXT
);

CREATE TABLE IF NOT EXISTS destinations (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL,
    ref        TEXT NOT NULL,
    last_ok_at TEXT,
    fail_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_destinations_user ON destinations(user_id);

CREATE TABLE IF NOT EXISTS prefs (
    user_id            INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    levels             TEXT NOT NULL DEFAULT '[]',
    countries          TEXT NOT NULL DEFAULT '[]',
    cities             TEXT NOT NULL DEFAULT '[]',
    remote_ok          INTEGER NOT NULL DEFAULT 1,
    favorite_companies TEXT NOT NULL DEFAULT '[]',
    muted_companies    TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS deliveries (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    uid     TEXT NOT NULL REFERENCES postings(uid) ON DELETE CASCADE,
    status  TEXT NOT NULL CHECK (status IN ('pending', 'sent', 'failed')),
    sent_at TEXT,
    PRIMARY KEY (user_id, uid)
);
CREATE INDEX IF NOT EXISTS idx_deliveries_sent_at ON deliveries(sent_at);
"""


def utcnow_iso() -> str:
    """Fixed-width ISO-8601 UTC timestamp (microseconds always present)."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def to_iso(dt: datetime) -> str:
    """Normalise any datetime (naive = UTC) to the fixed-width UTC format."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


def connect(path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open (creating if needed) the v2 database with WAL + FKs + 30s busy timeout."""
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA)
    if conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 0:
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
    conn.commit()
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]


# ---- postings / tags ------------------------------------------------------


def insert_new_postings(
    conn: sqlite3.Connection, jobs: list[Job], seen_at: str | None = None
) -> list[Job]:
    """Insert postings not yet stored; return only those that were new."""
    seen_at = seen_at or utcnow_iso()
    new: list[Job] = []
    for job in jobs:
        cur = conn.execute(
            "INSERT OR IGNORE INTO postings (uid, source, company, title, url, "
            "location_raw, posted_at, posted_note, first_seen_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                job.uid, job.source, job.company, job.title, job.url, job.location,
                to_iso(job.posted_at) if job.posted_at else None, job.posted_note, seen_at,
            ),
        )
        if cur.rowcount:
            new.append(job)
    conn.commit()
    return new


def upsert_tags(
    conn: sqlite3.Connection, uid: str, cls: Classification, classified_at: str | None = None
) -> None:
    conn.execute(
        "INSERT INTO posting_tags (uid, eligible, reject_reason, categories, level, countries, "
        "cities, remote, location_opaque, classified_at, rules_version) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(uid) DO UPDATE SET eligible=excluded.eligible, "
        "reject_reason=excluded.reject_reason, categories=excluded.categories, "
        "level=excluded.level, countries=excluded.countries, cities=excluded.cities, "
        "remote=excluded.remote, location_opaque=excluded.location_opaque, "
        "classified_at=excluded.classified_at, rules_version=excluded.rules_version",
        (
            uid, int(cls.eligible), cls.reject_reason, json.dumps(cls.categories), cls.level,
            json.dumps(cls.countries), json.dumps(cls.cities), int(cls.remote),
            int(cls.location_opaque), classified_at or utcnow_iso(), cls.rules_version,
        ),
    )
    conn.commit()


def tags_from_row(row: sqlite3.Row) -> Classification:
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
    )


def job_from_row(row: sqlite3.Row) -> Job:
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


# ---- users ----------------------------------------------------------------


@dataclass
class Prefs:
    levels: list[str] = field(default_factory=lambda: ["internship", "coop"])
    countries: list[str] = field(default_factory=lambda: ["CA", "US"])
    cities: list[str] = field(default_factory=list)
    remote_ok: bool = True
    favorite_companies: list[str] = field(default_factory=list)
    muted_companies: list[str] = field(default_factory=list)


@dataclass
class Destination:
    id: int
    kind: str
    ref: str
    last_ok_at: str | None = None
    fail_count: int = 0


@dataclass
class UserRecord:
    id: int
    discord_user_id: str | None
    activated_at: str
    status: str
    prefs: Prefs
    destination: Destination
    disabled_reason: str | None = None


def create_user(
    conn: sqlite3.Connection,
    prefs: Prefs,
    destination_kind: str,
    destination_ref: str,
    *,
    discord_user_id: str | None = None,
    status: str = "active",
    activated_at: str | None = None,
) -> int:
    now = utcnow_iso()
    cur = conn.execute(
        "INSERT INTO users (discord_user_id, created_at, activated_at, status) VALUES (?,?,?,?)",
        (discord_user_id, now, activated_at or now, status),
    )
    uid = cur.lastrowid
    conn.execute(
        "INSERT INTO prefs (user_id, levels, countries, cities, remote_ok, favorite_companies, "
        "muted_companies) VALUES (?,?,?,?,?,?,?)",
        (
            uid, json.dumps(prefs.levels), json.dumps(prefs.countries), json.dumps(prefs.cities),
            int(prefs.remote_ok), json.dumps(prefs.favorite_companies),
            json.dumps(prefs.muted_companies),
        ),
    )
    conn.execute(
        "INSERT INTO destinations (user_id, kind, ref) VALUES (?,?,?)",
        (uid, destination_kind, destination_ref),
    )
    conn.commit()
    return uid


def find_user_by_destination(
    conn: sqlite3.Connection, kind: str, ref: str
) -> int | None:
    row = conn.execute(
        "SELECT user_id FROM destinations WHERE kind=? AND ref=? ORDER BY user_id LIMIT 1",
        (kind, ref),
    ).fetchone()
    return row["user_id"] if row else None


def _user_from_row(row: sqlite3.Row) -> UserRecord:
    return UserRecord(
        id=row["id"],
        discord_user_id=row["discord_user_id"],
        activated_at=row["activated_at"],
        status=row["status"],
        disabled_reason=row["disabled_reason"],
        prefs=Prefs(
            levels=json.loads(row["levels"]),
            countries=json.loads(row["countries"]),
            cities=json.loads(row["cities"]),
            remote_ok=bool(row["remote_ok"]),
            favorite_companies=json.loads(row["favorite_companies"]),
            muted_companies=json.loads(row["muted_companies"]),
        ),
        destination=Destination(
            id=row["dest_id"], kind=row["kind"], ref=row["ref"],
            last_ok_at=row["last_ok_at"], fail_count=row["fail_count"],
        ),
    )


_USER_SELECT = (
    "SELECT u.id, u.discord_user_id, u.activated_at, u.status, u.disabled_reason, "
    "p.levels, p.countries, p.cities, p.remote_ok, p.favorite_companies, p.muted_companies, "
    "d.id AS dest_id, d.kind, d.ref, d.last_ok_at, d.fail_count "
    "FROM users u JOIN prefs p ON p.user_id = u.id "
    "JOIN destinations d ON d.id = (SELECT MIN(id) FROM destinations WHERE user_id = u.id) "
)


def list_users(conn: sqlite3.Connection, status: str | None = None) -> list[UserRecord]:
    sql = _USER_SELECT + ("WHERE u.status = ? " if status else "") + "ORDER BY u.id"
    rows = conn.execute(sql, (status,) if status else ()).fetchall()
    return [_user_from_row(r) for r in rows]


def list_active_users(conn: sqlite3.Connection) -> list[UserRecord]:
    """Active users that have prefs and a destination."""
    return list_users(conn, status="active")


def disable_user(conn: sqlite3.Connection, user_id: int, reason: str) -> None:
    conn.execute(
        "UPDATE users SET status='disabled', disabled_reason=? WHERE id=?", (reason, user_id)
    )
    conn.commit()


def destination_ok(conn: sqlite3.Connection, dest_id: int) -> None:
    conn.execute(
        "UPDATE destinations SET last_ok_at=?, fail_count=0 WHERE id=?", (utcnow_iso(), dest_id)
    )
    conn.commit()


def destination_failed(conn: sqlite3.Connection, dest_id: int) -> None:
    conn.execute("UPDATE destinations SET fail_count = fail_count + 1 WHERE id=?", (dest_id,))
    conn.commit()


# ---- candidates + deliveries ----------------------------------------------


def candidate_rows(conn: sqlite3.Connection, activated_at: str) -> list[sqlite3.Row]:
    """Eligible postings first seen at/after `activated_at`, joined with their tags."""
    return conn.execute(
        "SELECT p.*, t.eligible, t.reject_reason, t.categories, t.level, t.countries, "
        "t.cities, t.remote, t.location_opaque, t.rules_version "
        "FROM postings p JOIN posting_tags t ON t.uid = p.uid "
        "WHERE t.eligible = 1 AND p.first_seen_at >= ?",
        (activated_at,),
    ).fetchall()


def delivery_status(conn: sqlite3.Connection, user_id: int, uid: str) -> str | None:
    row = conn.execute(
        "SELECT status FROM deliveries WHERE user_id=? AND uid=?", (user_id, uid)
    ).fetchone()
    return row["status"] if row else None


def blocked_uids(conn: sqlite3.Connection, user_id: int) -> set[str]:
    """Uids this user already has a pending or sent delivery for (failed ones may retry)."""
    rows = conn.execute(
        "SELECT uid FROM deliveries WHERE user_id=? AND status IN ('pending','sent')",
        (user_id,),
    ).fetchall()
    return {r["uid"] for r in rows}


def reserve_delivery(conn: sqlite3.Connection, user_id: int, uid: str) -> bool:
    """Atomically claim (user, uid). True if this caller now owns the send.

    INSERT OR IGNORE a 'pending' row; if a 'failed' row exists it is flipped back
    to 'pending' (retry). A 'sent' or in-flight 'pending' row is never re-claimed.
    """
    cur = conn.execute(
        "INSERT OR IGNORE INTO deliveries (user_id, uid, status) VALUES (?,?,'pending')",
        (user_id, uid),
    )
    reserved = cur.rowcount == 1
    if not reserved:
        cur = conn.execute(
            "UPDATE deliveries SET status='pending' "
            "WHERE user_id=? AND uid=? AND status='failed'",
            (user_id, uid),
        )
        reserved = cur.rowcount == 1
    conn.commit()
    return reserved


def mark_sent(conn: sqlite3.Connection, user_id: int, uids: list[str]) -> None:
    now = utcnow_iso()
    conn.executemany(
        "UPDATE deliveries SET status='sent', sent_at=? WHERE user_id=? AND uid=?",
        [(now, user_id, u) for u in uids],
    )
    conn.commit()


def mark_failed(conn: sqlite3.Connection, user_id: int, uids: list[str]) -> None:
    conn.executemany(
        "UPDATE deliveries SET status='failed' WHERE user_id=? AND uid=?",
        [(user_id, u) for u in uids],
    )
    conn.commit()


# ---- stats ----------------------------------------------------------------


def stats_24h(conn: sqlite3.Connection) -> dict[str, int]:
    """Counts for the heartbeat: totals + postings ingested / eligible / alerts sent in 24h."""
    since = to_iso(datetime.now(timezone.utc) - timedelta(days=1))
    q = conn.execute
    return {
        "total": q("SELECT COUNT(*) FROM postings").fetchone()[0],
        "ingested_24h": q(
            "SELECT COUNT(*) FROM postings WHERE first_seen_at >= ?", (since,)
        ).fetchone()[0],
        "eligible_24h": q(
            "SELECT COUNT(*) FROM postings p JOIN posting_tags t ON t.uid = p.uid "
            "WHERE t.eligible = 1 AND p.first_seen_at >= ?", (since,)
        ).fetchone()[0],
        "sent_24h": q(
            "SELECT COUNT(*) FROM deliveries WHERE status='sent' AND sent_at >= ?", (since,)
        ).fetchone()[0],
    }
