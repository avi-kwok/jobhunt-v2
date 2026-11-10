"""POST /ingest/posting + worker classify_untagged, on SQLite and Postgres."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import respx
from conftest import PG_URL, hook_url, make_sub
from fastapi.testclient import TestClient

from jobhunt import db, ingest, pipeline
from jobhunt.config import load_config
from jobhunt.models import Job
from jobhunt.web import app as web

SECRET = "s" * 40
AUTH = {"Authorization": f"Bearer {SECRET}"}
GOOD = {
    "title": "Software Engineer Intern, Summer 2027",
    "company": "Example Co",
    "apply_url": "https://boards.greenhouse.io/example/jobs/123",
    "location": "San Jose, CA",
    "country": "US",
    "level": "intern",
}


@pytest.fixture(
    params=["sqlite", pytest.param("postgres", marks=pytest.mark.skipif(not PG_URL, reason="no PG url"))]
)
def target(request, tmp_path):
    if request.param == "postgres":
        c = db.connect(PG_URL)
        db.truncate_all(c)
        c.close()
        return PG_URL
    return str(tmp_path / "ingest.db")


@pytest.fixture
def client(target, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", target)
    monkeypatch.setenv("INGEST_SECRET", SECRET)
    monkeypatch.setenv("INGEST_ENABLED", "true")
    return TestClient(web.app)


@pytest.fixture
def conn(target):
    c = db.connect(target)
    yield c
    c.close()


@pytest.fixture
def cfg():
    return load_config(Path("config.yaml"), resolved_path=Path("/nonexistent.yaml"))


def post(client, body=None, headers=AUTH, **over):
    data = dict(GOOD if body is None else body, **over)
    data = {k: v for k, v in data.items() if v is not ...}
    return client.post("/ingest/posting", content=json.dumps(data), headers=headers)


def count(conn):
    return conn.execute("SELECT COUNT(*) FROM postings").fetchone()[0]


# ---- gate + auth ----


@pytest.mark.parametrize("secret", [None, "", "short" * 4, "x" * 31])
def test_503_when_secret_unset_or_short(client, monkeypatch, conn, secret):
    if secret is None:
        monkeypatch.delenv("INGEST_SECRET")
    else:
        monkeypatch.setenv("INGEST_SECRET", secret)
    r = post(client, headers={"Authorization": f"Bearer {secret}"})
    assert r.status_code == 503 and count(conn) == 0


@pytest.mark.parametrize("val", [None, "false", "TRUE", "1", ""])
def test_503_when_not_enabled(client, monkeypatch, conn, val):
    if val is None:
        monkeypatch.delenv("INGEST_ENABLED")
    else:
        monkeypatch.setenv("INGEST_ENABLED", val)
    assert post(client).status_code == 503 and count(conn) == 0


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer wrong"}, {"Authorization": SECRET},
     {"Authorization": f"Basic {SECRET}"}, {"Authorization": "Bearer "}],
)
def test_401_nothing_stored(client, conn, headers):
    assert post(client, headers=headers).status_code == 401
    assert count(conn) == 0


# ---- happy path ----


def test_accepted_stored_untagged_with_server_side_source(client, conn):
    r = post(client, source="evil", deadline="2027-01-01")
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "accepted" and body["posting_id"].startswith("muse-pipeline:Example Co:")
    row = conn.execute("SELECT * FROM postings").fetchone()
    assert row["uid"] == body["posting_id"] and row["source"] == "muse-pipeline"
    assert row["url"] == GOOD["apply_url"] and row["location_raw"] == "San Jose, CA"
    assert conn.execute("SELECT COUNT(*) FROM posting_tags").fetchone()[0] == 0
    assert "Strict-Transport-Security" in r.headers and "access-control-allow-origin" not in r.headers


def test_location_filled_from_country_only_when_missing(client, conn):
    assert post(client, location=...).status_code == 201
    assert conn.execute("SELECT location_raw FROM postings").fetchone()[0] == "United States"
    assert post(client, apply_url="https://x.example/2", location=..., country="CA").status_code == 201
    assert post(client, apply_url="https://x.example/3", location=..., country="FR").status_code == 201
    locs = [r[0] for r in conn.execute("SELECT location_raw FROM postings ORDER BY url")]
    assert sorted(l or "" for l in locs) == ["", "Canada", "United States"]


def test_posted_at_date_and_datetime(client, conn):
    assert post(client, posted_at="2026-11-09").status_code == 201
    assert post(client, apply_url="https://x.example/2", posted_at="2026-11-09T10:00:00Z").status_code == 201
    got = {r[0] for r in conn.execute("SELECT posted_at FROM postings")}
    assert got == {None, db.to_iso(datetime(2026, 11, 9, 10, tzinfo=timezone.utc))}
    note = conn.execute("SELECT posted_note FROM postings WHERE posted_at IS NULL").fetchone()[0]
    assert note == "posted 2026-11-09"
    assert post(client, apply_url="https://x.example/3", posted_at="yesterday").status_code == 422


# ---- dedup ----


def test_duplicate_same_url_and_idempotent_retry(client, conn):
    first = post(client)
    again = post(client)
    assert first.status_code == 201 and again.status_code == 200
    assert again.json() == {"status": "duplicate"} and count(conn) == 1
    # same page, different query / fragment / case / trailing slash, other company text
    assert post(client, apply_url="HTTPS://Boards.Greenhouse.io:443/example/jobs/123/#x").status_code == 200
    assert count(conn) == 1


def test_query_string_distinguishes_jobs(client, conn):
    u = "https://careers.example/apply"
    assert post(client, apply_url=u + "?gh_jid=1").status_code == 201
    assert post(client, apply_url=u + "?gh_jid=2").status_code == 201
    assert post(client, apply_url=u + "?gh_jid=1").status_code == 200
    assert post(client, apply_url=u + "/?gh_jid=2#f").status_code == 200
    assert count(conn) == 2


def test_duplicate_of_scanner_posting_url_incl_variants(client, conn):
    jobs = [
        Job(source="greenhouse", company="A", external_id="1", title="t", url="https://jobs.example/a/1"),
        Job(source="greenhouse", company="B", external_id="2", title="t", url="https://jobs.example/b/2/"),
        Job(source="lever", company="C", external_id="3", title="t", url="http://jobs.example/c/3"),
        Job(source="lever", company="D", external_id="4", title="t", url="https://jobs.example/d/4?gh=9"),
    ]
    db.insert_new_postings(conn, jobs)
    for u in ("https://jobs.example/a/1", "https://jobs.example/a/1/", "https://jobs.example/b/2",
              "https://JOBS.example/c/3", "https://jobs.example/d/4?gh=9",
              "https://jobs.example/d/4/?gh=9"):
        r = post(client, apply_url=u, company="Other")
        assert r.status_code == 200 and r.json() == {"status": "duplicate"}, u
    assert count(conn) == 4


def test_insert_race_reports_duplicate(client, conn, monkeypatch):
    monkeypatch.setattr(db, "posting_known", lambda *a: False)  # both requests passed the check
    assert post(client).status_code == 201
    r = post(client)
    assert r.status_code == 200 and r.json() == {"status": "duplicate"} and count(conn) == 1


# ---- validation ----


@pytest.mark.parametrize("field", ["title", "company", "apply_url", "country", "level"])
def test_422_missing_required_field(client, conn, field):
    r = post(client, **{field: ...})
    assert r.status_code == 422 and r.json()["error"].startswith(field + ":")
    assert count(conn) == 0


@pytest.mark.parametrize(
    "over,field",
    [({"level": "internship"}, "level"), ({"level": "full"}, "level"),
     ({"country": "USA"}, "country"), ({"country": "1A"}, "country"), ({"country": 5}, "country"),
     ({"title": "x" * 301}, "title"), ({"company": "x" * 201}, "company"),
     ({"location": "x" * 201}, "location"), ({"title": 5}, "title"),
     ({"apply_url": "http://a.example/1"}, "apply_url"),
     ({"apply_url": "ftp://a.example/1"}, "apply_url"),
     ({"apply_url": "https://user:pw@a.example/1"}, "apply_url"),
     ({"apply_url": "https://a.example/ 1"}, "apply_url"),
     ({"apply_url": "https://a.example/\n1"}, "apply_url"),
     ({"apply_url": "https:///path"}, "apply_url"),
     ({"apply_url": "https://a.example/" + "x" * 2000}, "apply_url"),
     ({"title": "a\x00b"}, "title")],
)
def test_422_bad_values(client, conn, over, field):
    r = post(client, **over)
    assert r.status_code == 422 and r.json()["error"].startswith(field + ":")
    assert count(conn) == 0


@pytest.mark.parametrize("raw", [b"not json", b"[1]", b"null", b"", b'"s"'])
def test_422_malformed_json(client, conn, raw):
    assert client.post("/ingest/posting", content=raw, headers=AUTH).status_code == 422
    assert count(conn) == 0


def test_413_oversize_real_length(client, conn):
    big = json.dumps(dict(GOOD, title="x" * 5000))
    assert client.post("/ingest/posting", content=big, headers=AUTH).status_code == 413

    def chunks():  # no Content-Length: chunked body is counted as it streams
        yield b'{"title": "'
        yield b"x" * 5000
        yield b'"}'

    assert client.post("/ingest/posting", content=chunks(), headers=AUTH).status_code == 413
    assert count(conn) == 0


def test_429_rate_limit(client, conn, monkeypatch):
    monkeypatch.setattr(web, "INGEST_PER_HOUR", 2)
    codes = [post(client, apply_url=f"https://x.example/{i}").status_code for i in range(4)]
    assert codes == [201, 201, 429, 429] and count(conn) == 2


def test_secret_not_logged(client, caplog):
    caplog.set_level("DEBUG")
    post(client)
    post(client, headers={"Authorization": "Bearer wrong-wrong-wrong"})
    assert SECRET not in caplog.text and "wrong-wrong" not in caplog.text and "Example Co" not in caplog.text


# ---- worker: classify + deliver ----


def _ingest(client, **over):
    r = post(client, **over)
    assert r.status_code == 201
    return r.json()["posting_id"]


def _tags(conn, uid):
    r = conn.execute("SELECT * FROM posting_tags WHERE uid=?", (uid,)).fetchone()
    return db.tags_from_row(r) if r else None


def _deliver_all(cfg, conn):
    sent = []
    for sub in db.list_active_subscribers(conn):
        plan = pipeline.match_subscribers(cfg, conn, [sub])
        res = pipeline.deliver(conn, sub, plan[sub.id], log=lambda m: None)
        sent.append(res.sent)
    return sent


@respx.mock
def test_worker_classifies_and_delivers_to_matching_subscriber(client, conn, cfg):
    route = respx.post(hook_url(1)).mock(return_value=httpx.Response(204))
    make_sub(conn, 1, countries=("US",), levels=("internship",),
             activated_at=datetime.now(timezone.utc) - timedelta(days=1))
    uid = _ingest(client)
    assert _tags(conn, uid) is None and _deliver_all(cfg, conn) == [0]
    assert pipeline.classify_untagged(cfg, conn) == 1
    t = _tags(conn, uid)
    assert t.eligible and t.level == "internship" and "US" in t.countries
    assert pipeline.classify_untagged(cfg, conn) == 0  # nothing left
    assert _deliver_all(cfg, conn) == [1]
    embeds = json.loads(route.calls[0].request.content)["embeds"]
    assert "Software Engineer Intern" in embeds[0]["title"]
    assert _deliver_all(cfg, conn) == [0]  # not sent twice


@respx.mock
def test_worker_respects_subscriber_prefs(client, conn, cfg):
    respx.post(hook_url(1)).mock(return_value=httpx.Response(204))
    make_sub(conn, 1, countries=("CA",), levels=("internship",),
             activated_at=datetime.now(timezone.utc) - timedelta(days=1))
    _ingest(client)
    pipeline.classify_untagged(cfg, conn)
    assert _deliver_all(cfg, conn) == [0]


@respx.mock
def test_stale_ingested_posting_tagged_stale_not_delivered(client, conn, cfg):
    respx.post(hook_url(1)).mock(return_value=httpx.Response(204))
    make_sub(conn, 1, activated_at=datetime.now(timezone.utc) - timedelta(days=3))
    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    uid = _ingest(client, posted_at=old)
    pipeline.classify_untagged(cfg, conn)
    t = _tags(conn, uid)
    assert not t.eligible and t.reject_reason == pipeline.STALE_REASON
    assert _deliver_all(cfg, conn) == [0]


@respx.mock
@respx.mock
def test_date_only_posted_at_yesterday_is_delivered(client, conn, cfg):
    respx.post(hook_url(1)).mock(return_value=httpx.Response(204))
    make_sub(conn, 1, activated_at=datetime.now(timezone.utc) - timedelta(days=3))
    day = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
    uid = _ingest(client, posted_at=day)
    pipeline.classify_untagged(cfg, conn)
    assert _tags(conn, uid).eligible and _deliver_all(cfg, conn) == [1]


@respx.mock
def test_pm_title_stored_but_rejected_by_rules(client, conn, cfg):
    respx.post(hook_url(1)).mock(return_value=httpx.Response(204))
    make_sub(conn, 1, activated_at=datetime.now(timezone.utc) - timedelta(days=1))
    uid = _ingest(client, title="Product Manager Intern", apply_url="https://x.example/pm")
    pipeline.classify_untagged(cfg, conn)
    assert count(conn) == 1 and not _tags(conn, uid).eligible
    assert _deliver_all(cfg, conn) == [0]


def test_client_level_and_country_not_trusted(client, conn, cfg):
    uid = _ingest(client, title="Senior Software Engineer Intern", level="intern", country="US")
    pipeline.classify_untagged(cfg, conn)
    assert not _tags(conn, uid).eligible


def test_classify_untagged_is_bounded(conn, cfg):
    jobs = [Job(source="muse-pipeline", company="A", external_id=str(i),
                title="Software Engineer Intern", url=f"https://x.example/{i}", location="Toronto, ON")
            for i in range(7)]
    db.insert_new_postings(conn, jobs)
    assert pipeline.classify_untagged(cfg, conn, limit=5) == 5
    assert pipeline.classify_untagged(cfg, conn, limit=5) == 2


def test_normalize_url():
    n = ingest.normalize_url
    assert n("HTTPS://Ex.com:443/a/b/?q=1#f") == "https://ex.com/a/b?q=1"
    assert n("https://ex.com:8443/a") == "https://ex.com:8443/a"
    assert n("https://ex.com/") == "https://ex.com"
