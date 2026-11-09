"""Web app: FastAPI TestClient + respx (no real network), on SQLite and Postgres."""

import json
import re

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from conftest import PG_URL, hook_url
from jobhunt import crypto, db, webhooks
from jobhunt.web import app as web

BASE = "https://jobhunt.example"


@pytest.fixture(
    params=[
        "sqlite",
        pytest.param("postgres", marks=pytest.mark.skipif(not PG_URL, reason="no PG url")),
    ]
)
def target(request, tmp_path):
    if request.param == "postgres":
        c = db.connect(PG_URL)
        db.truncate_all(c)
        c.close()
        return PG_URL
    return str(tmp_path / "web.db")


@pytest.fixture
def client(target, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", target)
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE)
    return TestClient(web.app)


@pytest.fixture
def conn(target):
    c = db.connect(target)
    yield c
    c.close()


@pytest.fixture
def discord():
    with respx.mock(assert_all_called=False) as m:
        m.get(url__regex=r"https://discord\.com/api/webhooks/\d+/[\w-]+").respond(200, json={})
        m.post(url__regex=r"https://discord\.com/api/webhooks/\d+/[\w-]+").respond(204)
        yield m


def posts(m):
    return [c.request for c in m.calls if c.request.method == "POST"]


def token_from(m, n=-1):
    body = json.loads(posts(m)[n].content)
    return re.search(r"/manage\?t=([\w-]+)", body["embeds"][0]["description"]).group(1)


def subscribe(client, n=1, ip="1.1.1.1", countries=("CA", "US"), levels=("internship", "coop")):
    return client.post("/subscribe", headers={"x-forwarded-for": ip},
                       data={"webhook_url": hook_url(n), "countries": list(countries),
                             "levels": list(levels)})


def manage(client, t, action, **extra):
    return client.post("/manage", data={"t": t, "action": action, **extra})


def row(conn, n=1):
    h = crypto.webhook_hash(webhooks.normalize(hook_url(n)))
    return db.get_subscriber_by_hash(conn, h)


def test_full_flow(client, discord, conn):
    r = subscribe(client)
    assert r.status_code == 200 and "Check your Discord channel" in r.text
    assert len(posts(discord)) == 1
    t = token_from(discord)
    sub = row(conn)
    assert sub.status == "pending"

    page = client.get(f"/manage?t={t}")
    assert page.status_code == 200 and "pending" in page.text
    assert row(conn).status == "pending"  # GET never activates

    assert "Alerts are on" in manage(client, t, "activate").text
    sub = row(conn)
    assert sub.status == "active" and sub.activated_at

    r = manage(client, t, "update", countries=["CA"], levels=["coop"])
    assert "Preferences saved" in r.text
    sub = row(conn)
    assert sub.countries == ["CA"] and sub.levels == ["coop"]

    assert manage(client, t, "pause").status_code == 200 and row(conn).status == "paused"
    assert manage(client, t, "resume").status_code == 200 and row(conn).status == "active"

    before = len(posts(discord))
    r = manage(client, t, "delete")
    assert "Unsubscribed" in r.text and row(conn) is None
    assert len(posts(discord)) == before + 1  # goodbye message
    assert not [c for c in discord.calls if c.request.method == "DELETE"]  # webhook untouched
    assert client.get(f"/manage?t={t}").status_code == 404


def test_delete_cascades_deliveries(client, discord, conn):
    subscribe(client)
    sub = row(conn)
    conn.execute("INSERT INTO postings (uid, source, company, title, url, first_seen_at) "
                 "VALUES ('u','s','c','t','http://x', ?)", (db.utcnow_iso(),))
    conn.execute("INSERT INTO deliveries (subscriber_id, uid, status, updated_at) "
                 "VALUES (?, 'u', 'sent', ?)", (sub.id, db.utcnow_iso()))
    conn.commit()
    manage(client, token_from(discord), "delete")
    assert conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] == 0
    conn.commit()


def test_resubscribe_identical_and_rotates(client, discord, conn):
    first = subscribe(client, countries=["CA"], levels=["coop"])
    t1 = token_from(discord)
    manage(client, t1, "activate")
    second = subscribe(client, countries=["US"], levels=["internship"])
    t2 = token_from(discord)
    assert first.text == second.text and first.status_code == second.status_code
    assert t1 != t2
    assert client.get(f"/manage?t={t1}").status_code == 404
    assert manage(client, t1, "pause").status_code == 404
    assert client.get(f"/manage?t={t2}").status_code == 200
    sub = row(conn)
    assert sub.status == "active" and sub.countries == ["CA"] and sub.levels == ["coop"]
    assert conn.execute("SELECT COUNT(*) FROM subscribers").fetchone()[0] == 1


@pytest.mark.parametrize("bad", [
    "", "not a url", "http://discord.com/api/webhooks/123/abc",
    "https://evil.example/api/webhooks/" + "1" * 18 + "/" + "A" * 64,
    "https://discord.com/api/webhooks/" + "1" * 18 + "/" + "A" * 64 + "?x=1",
])
def test_invalid_urls_rejected(client, discord, bad):
    r = client.post("/subscribe", data={"webhook_url": bad, "countries": ["CA"], "levels": ["coop"]})
    assert r.status_code == 400 and "could not use that webhook" in r.text
    assert not discord.calls
    assert bad not in r.text or bad == ""


def test_no_prefs_rejected(client, discord):
    r = subscribe(client, countries=[], levels=["coop"])
    assert r.status_code == 400 and not discord.calls


def test_webhook_404_rejected_generic(client, conn):
    with respx.mock(assert_all_called=False) as m:
        m.get(url__regex=r"https://discord\.com/api/webhooks/.*").respond(404)
        r = subscribe(client)
        assert r.status_code == 400 and "could not use that webhook" in r.text
        assert not posts(m)
    assert conn.execute("SELECT COUNT(*) FROM subscribers").fetchone()[0] == 0


def test_webhook_network_error_rejected(client):
    with respx.mock(assert_all_called=False) as m:
        m.get(url__regex=r"https://discord\.com/api/webhooks/.*").mock(
            side_effect=httpx.ConnectTimeout("t"))
        assert subscribe(client).status_code == 400


def test_ip_rate_limit(client, discord):
    for i in range(10):
        assert subscribe(client, n=i + 1, ip="9.9.9.9").status_code == 200
    assert len(posts(discord)) == 10
    r = subscribe(client, n=11, ip="9.9.9.9")
    assert r.status_code == 200 and "Check your Discord channel" in r.text
    assert len(posts(discord)) == 10  # nothing posted over the limit
    assert subscribe(client, n=11, ip="8.8.8.8").status_code == 200  # other IP unaffected
    assert len(posts(discord)) == 11


def test_webhook_rate_limit(client, discord, conn):
    for i in range(3):
        subscribe(client, ip=f"2.2.2.{i}")
    assert len(posts(discord)) == 3
    t3 = token_from(discord)
    r = subscribe(client, ip="2.2.2.9")
    assert "Check your Discord channel" in r.text
    assert len(posts(discord)) == 3
    assert client.get(f"/manage?t={t3}").status_code == 200  # token not rotated when limited
    conn.execute("UPDATE subscribers SET verify_window_start=?",
                 (db.to_iso(db.datetime.now(db.timezone.utc) - db.timedelta(hours=25)),))
    conn.commit()
    subscribe(client, ip="2.2.2.10")
    assert len(posts(discord)) == 4


def test_security_headers(client, discord):
    t = (subscribe(client), token_from(discord))[1]
    for path in ["/", "/privacy", "/terms", "/healthz", f"/manage?t={t}", "/manage?t=bad",
                 "/nope", "/static/style.css"]:
        r = client.get(path)
        assert r.headers["content-security-policy"] == (
            "default-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"), path
        assert r.headers["strict-transport-security"] == "max-age=63072000; includeSubDomains"
        assert r.headers["x-frame-options"] == "DENY"
        assert r.headers["referrer-policy"] == "no-referrer"
        assert r.headers["x-content-type-options"] == "nosniff"
        assert "set-cookie" not in r.headers
    assert client.get(f"/manage?t={t}").headers["cache-control"] == "no-store"
    assert client.post("/manage", data={"t": t, "action": "pause"}).headers["cache-control"] == "no-store"


def test_no_response_contains_webhook(client, discord):
    url = hook_url(1)
    secret = url.rsplit("/", 1)[1]
    bodies = [client.get("/").text, subscribe(client).text, subscribe(client).text]
    t = token_from(discord)
    bodies += [client.get(f"/manage?t={t}").text, manage(client, t, "activate").text,
               manage(client, t, "update", countries=["CA"], levels=["coop"]).text,
               client.post("/subscribe", data={"webhook_url": url + "?x", "levels": "coop"}).text,
               manage(client, t, "delete").text]
    for b in bodies:
        assert secret not in b and url not in b
    manage_pages = [bodies[3], bodies[4], bodies[5]]
    assert all(t in p for p in manage_pages)  # token only in the manage page's hidden field
    assert t not in bodies[1] and t not in bodies[2]


def test_unknown_or_bad_actions(client, discord):
    subscribe(client)
    t = token_from(discord)
    assert manage(client, t, "pause").status_code == 400  # pending cannot pause
    assert manage(client, t, "bogus").status_code == 400
    assert manage(client, t, "update", countries=["CA"]).status_code == 400  # no levels
    assert manage(client, "x" * 500, "activate").status_code == 404
    assert client.get("/manage").status_code == 404


def test_token_stored_hashed_and_url_encrypted(client, discord, conn):
    subscribe(client)
    t = token_from(discord)
    r = conn.execute("SELECT webhook_enc, manage_token_hash FROM subscribers").fetchone()
    assert r[1] == crypto.token_hash(t) and t not in r[1]
    assert hook_url(1) not in r[0] and crypto.decrypt_url(r[0]) == webhooks.normalize(hook_url(1))
    assert len(t) >= 43


def test_discord_requests_use_parsed_url(client, discord):
    subscribe(client)
    exp = webhooks.normalize(hook_url(1))
    assert all(str(c.request.url) == exp for c in discord.calls)


def test_healthz(client, monkeypatch):
    assert client.get("/healthz").text == "ok"
    monkeypatch.setenv("DATABASE_URL", "postgresql://127.0.0.1:1/none")
    r = client.get("/healthz")
    assert r.status_code == 503 and r.text == "error"


def test_disabled_resubscribe_goes_pending(client, discord, conn):
    subscribe(client)
    db.disable_subscriber(conn, row(conn).id, "discord 404")
    subscribe(client, ip="3.3.3.3")
    sub = row(conn)
    assert sub.status == "pending" and sub.disabled_reason is None


def test_activate_then_worker_sees_subscriber(client, discord, conn):
    subscribe(client)
    manage(client, token_from(discord), "activate")
    assert [s.id for s in db.list_active_subscribers(conn)] == [row(conn).id]


def test_get_manage_never_activates(client, discord, conn):
    subscribe(client)
    tok = token_from(discord)
    for _ in range(2):
        assert client.get("/manage", params={"t": tok}).status_code == 200
    assert row(conn).status == "pending"


def test_client_ip_groups_ipv6_64():
    from types import SimpleNamespace

    def ip(x):
        return web._client_ip(SimpleNamespace(headers={"x-forwarded-for": x}, client=None))

    assert ip("2001:db8:1:2:aaaa::1") == ip("2001:db8:1:2:bbbb::9")
    assert ip("2001:db8:1:3::1") != ip("2001:db8:1:2::1")
    assert ip("1.2.3.4, 5.6.7.8") == "1.2.3.4"


def test_failed_post_new_webhook_rejected_and_not_stored(client, conn):
    with respx.mock(assert_all_called=False) as m:
        m.get(url__regex=r"https://discord\.com/api/webhooks/.*").respond(200, json={})
        m.post(url__regex=r"https://discord\.com/api/webhooks/.*").respond(500)
        r = subscribe(client)
    assert r.status_code == 400
    assert row(conn) is None


def test_failed_post_existing_keeps_token_and_slot(client, conn):
    with respx.mock(assert_all_called=False) as m:
        m.get(url__regex=r"https://discord\.com/api/webhooks/.*").respond(200, json={})
        ok = m.post(url__regex=r"https://discord\.com/api/webhooks/.*").respond(204)
        subscribe(client)
        old = token_from(m)
        ok.respond(500)
        r = subscribe(client)
    assert r.status_code == 200
    assert client.get("/manage", params={"t": old}).status_code == 200
    assert conn.execute("SELECT verify_count FROM subscribers").fetchone()[0] == 1
