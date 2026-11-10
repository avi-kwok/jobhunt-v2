"""Admin dashboard: stats queries, rendered HTML safety, and the localhost-only server."""

from __future__ import annotations

import http.client
import threading
from datetime import datetime, timedelta, timezone

import pytest
from conftest import hook_url, make_sub

from jobhunt import crypto, dashboard, webhooks
from jobhunt import db as v2db
from jobhunt.models import Job

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)


def _set(conn, sql, *params):
    conn.execute(sql, params)
    conn.commit()


def test_status_counts_and_totals(conn):
    make_sub(conn, 1)
    make_sub(conn, 2, status="pending")
    make_sub(conn, 3, status="pending")
    assert v2db.subscriber_status_counts(conn) == {
        "active": 1, "pending": 2, "paused": 0, "disabled": 0,
    }
    assert v2db.totals(conn) == {"postings": 0, "sent": 0}


def test_daily_activity_buckets_by_utc_day(conn):
    a = make_sub(conn, 1)
    b = make_sub(conn, 2, status="pending")
    _set(conn, "UPDATE subscribers SET created_at=?, activated_at=? WHERE id=?",
         v2db.to_iso(NOW), v2db.to_iso(NOW), a)
    _set(conn, "UPDATE subscribers SET created_at=? WHERE id=?",
         v2db.to_iso(NOW - timedelta(days=1)), b)
    jobs = [Job(source="x", company="C", external_id=str(i), title="SWE Intern", url="u")
            for i in range(3)]
    v2db.insert_new_postings(conn, jobs[:2], seen_at=v2db.to_iso(NOW))
    v2db.insert_new_postings(conn, jobs[2:], seen_at=v2db.to_iso(NOW - timedelta(days=30)))
    v2db.reserve_delivery(conn, a, jobs[0].uid)
    v2db.mark_sent(conn, a, [jobs[0].uid])
    v2db.reserve_delivery(conn, a, jobs[1].uid)
    v2db.mark_failed(conn, a, [jobs[1].uid])
    # pin delivery timestamps to known days (late-evening UTC edge: stays in its own day)
    _set(conn, "UPDATE deliveries SET sent_at=? WHERE status='sent'",
         v2db.to_iso(NOW - timedelta(days=2)).replace("12:00", "23:59"))
    _set(conn, "UPDATE deliveries SET updated_at=? WHERE status='failed'",
         v2db.to_iso(NOW - timedelta(days=3)))

    rows = v2db.daily_activity(conn, 14, now=NOW)
    assert len(rows) == 14 and rows[-1]["day"] == "2026-11-09" and rows[0]["day"] == "2026-10-27"
    by = {r["day"]: r for r in rows}
    assert by["2026-11-09"]["signups"] == 1 and by["2026-11-09"]["activations"] == 1
    assert by["2026-11-09"]["postings"] == 2
    assert by["2026-11-08"]["signups"] == 1
    assert by["2026-11-07"]["sent"] == 1
    assert by["2026-11-06"]["failed"] == 1
    assert sum(r["postings"] for r in rows) == 2  # 30-day-old posting outside the window
    assert v2db.totals(conn) == {"postings": 3, "sent": 1}


def test_stats_24h_growth_fields(conn):
    make_sub(conn, 1)
    make_sub(conn, 2, status="pending")
    sid = make_sub(conn, 3)
    v2db.disable_subscriber(conn, sid, "x")
    s = v2db.stats_24h(conn)
    assert (s["active_subscribers"], s["pending_subscribers"], s["disabled_subscribers"]) == (1, 1, 1)
    assert s["signups_24h"] == 3 and s["activations_24h"] == 2


def test_page_has_counts_and_no_secrets(conn):
    make_sub(conn, 1)
    sid = make_sub(conn, 2, status="pending")
    _set(conn, "UPDATE subscribers SET disabled_reason=? WHERE id=?", "<script>x</script>", sid)
    html = dashboard.render_page(conn, "5 min ago", "10 min ago")
    assert "...0001" in html and "...0002" in html
    assert "<script>x</script>" not in html and "&lt;script&gt;" in html
    assert "5 min ago" in html and "Postings stored" in html
    url = webhooks.normalize(hook_url(1))
    secrets = [url, webhooks.parse(url).id, "A" * 64, crypto.webhook_hash(url)]
    secrets += [r[0] for r in conn.execute("SELECT webhook_enc FROM subscribers")]
    secrets += [r[0] for r in conn.execute("SELECT manage_token_hash FROM subscribers")
                if r[0]]
    for sec in secrets:
        assert sec and sec not in html


def test_bind_is_loopback_only():
    srv = dashboard.make_server(0, lambda: "ok")
    try:
        assert srv.server_address[0] == "127.0.0.1"
    finally:
        srv.server_close()


@pytest.fixture
def server():
    srv = dashboard.make_server(0, lambda: "<p>hi</p>")
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def _req(srv, method, path, host=None):
    port = srv.server_address[1]
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.putrequest(method, path, skip_host=True)
    c.putheader("Host", host or f"127.0.0.1:{port}")
    c.putheader("Content-Length", "0")
    c.endheaders()
    r = c.getresponse()
    r.read()
    c.close()
    return r


def test_server_get_ok_with_security_headers(server):
    r = _req(server, "GET", "/")
    assert r.status == 200
    assert "default-src 'none'" in r.getheader("Content-Security-Policy")
    assert r.getheader("X-Frame-Options") == "DENY"
    assert r.getheader("Referrer-Policy") == "no-referrer"
    assert r.getheader("Cache-Control") == "no-store"
    assert _req(server, "GET", "/", host=f"localhost:{server.server_address[1]}").status == 200


def test_server_rejects_bad_host_unknown_path_and_non_get(server):
    assert _req(server, "GET", "/", host="evil.example").status == 403
    assert _req(server, "GET", "/", host="127.0.0.1:1").status == 403
    assert _req(server, "GET", "/other").status == 404
    assert _req(server, "POST", "/").status == 405
