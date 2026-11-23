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


def subscribe(client, n=1, ip="1.1.1.1", countries=("CA", "US"), levels=("internship", "coop"),
              updates="1"):
    return client.post("/subscribe", headers={"x-forwarded-for": ip},
                       data={"webhook_url": hook_url(n), "countries": list(countries),
                             "levels": list(levels), "updates": updates})


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
    assert sub.status == "pending" and not sub.updates_opt_out  # signup box ticked

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
    assert sub.updates_opt_out  # "updates" checkbox left unticked
    manage(client, t, "update", countries=["CA"], levels=["coop"], updates="1")
    assert not row(conn).updates_opt_out

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
    titles = [json.loads(r.content)["embeds"][0]["title"] for r in posts(discord)]
    assert titles == ["Activate your jobhunt alerts", "Your new jobhunt manage link"]
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


def test_no_prefs_rejected(client, discord, conn):
    for countries, levels in (([], ["coop"]), (["CA"], []), ([], [])):
        r = subscribe(client, countries=countries, levels=levels)
        assert r.status_code == 400 and "Pick at least one country and one level." in r.text
    assert not discord.calls and row(conn) is None


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
    assert json.loads(posts(discord)[-1].content)["embeds"][0]["title"] == "Activate your jobhunt alerts"


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


def test_signup_without_grad_date_is_null(client, discord, conn):
    assert subscribe(client).status_code == 200
    assert row(conn).grad_date is None


def test_signup_with_grad_date(client, discord, conn):
    r = client.post("/subscribe", headers={"x-forwarded-for": "1.1.1.1"},
                    data={"webhook_url": hook_url(1), "countries": ["CA"], "levels": ["coop"],
                          "grad_month": "4", "grad_year": str(web._grad_years()[2])})
    assert r.status_code == 200
    assert row(conn).grad_date == f"{web._grad_years()[2]}-04"


@pytest.mark.parametrize("month,year", [("13", "{y}"), ("0", "{y}"), ("4", "1999"), ("4", "{far}"),
                                        ("4", ""), ("", "{y}"), ("x", "{y}"), ("4", "20 28")])
def test_signup_invalid_grad_rejected(client, discord, conn, month, year):
    y = web._grad_years()[0]
    year = year.format(y=y, far=y + 7)
    r = client.post("/subscribe", headers={"x-forwarded-for": "1.1.1.1"},
                    data={"webhook_url": hook_url(1), "countries": ["CA"], "levels": ["coop"],
                          "grad_month": month, "grad_year": year})
    assert r.status_code == 400 and web._BAD in r.text
    assert row(conn) is None and not posts(discord)


def test_manage_set_and_clear_grad_date(client, discord, conn):
    subscribe(client)
    t = token_from(discord)
    y = web._grad_years()[1]
    r = manage(client, t, "update", countries=["CA"], levels=["coop"], grad_month="9", grad_year=str(y))
    assert "Preferences saved" in r.text and row(conn).grad_date == f"{y}-09"
    page = client.get(f"/manage?t={t}").text
    assert re.search(r'<option value="9" selected>', page) and f'<option value="{y}" selected>' in page
    bad = manage(client, t, "update", countries=["CA"], levels=["coop"], grad_month="2", grad_year="1990")
    assert bad.status_code == 400 and row(conn).grad_date == f"{y}-09"  # unchanged
    r = manage(client, t, "update", countries=["CA"], levels=["coop"], grad_month="", grad_year="")
    assert "Preferences saved" in r.text and row(conn).grad_date is None


def test_signup_page_has_grad_selects(client):
    page = client.get("/").text
    assert 'name="grad_month"' in page and 'name="grad_year"' in page and "No filter" in page


def test_signup_with_updates_unticked_opts_out(client, discord, conn):
    subscribe(client, updates="")
    assert row(conn).updates_opt_out


def test_companies_page_lists_new_tracked_and_untrackable(client, monkeypatch):
    import re
    from datetime import date, timedelta

    from jobhunt.web import app as web
    tracked, added = web._tracked_companies()
    assert len(tracked) > 400 and "SimplifyJobs" not in tracked
    today = date.today()
    monkeypatch.setattr(web, "_tracked_companies",
                        lambda: (["Acme", "Old Co"], [(today, "Acme"), (today - timedelta(days=60), "Old Co")]))
    page = client.get("/companies").text
    new = page.split("New (last")[1].split("</h2>")[1].split("<h2>")[0]
    assert "Acme" in new and "Old Co" not in new and "track (2)" in page
    assert '<a href="/companies">' in client.get("/").text
    # A company on the can't-track list must not also be tracked.
    cant = re.search(r"can't track</h2>.*?<p class=\"muted\">(.*?)</p>", page, re.S).group(1)
    assert len(cant.split(", ")) > 30 and not set(cant.split(", ")) & set(tracked)


# ---- feedback ----------------------------------------------------------------------------


def test_feedback_stores_and_thanks(client, conn):
    assert client.get("/feedback").status_code == 200
    r = client.post("/feedback", data={"good": " fast alerts ", "improve": "", "features": "<b>x</b>"})
    assert r.status_code == 200 and "Thanks" in r.text
    [(_, _, good, improve, features)] = db.list_feedback(conn)
    assert (good, improve, features) == ("fast alerts", "", "<b>x</b>")


def test_feedback_rejects_empty_and_too_long(client, conn):
    assert client.post("/feedback", data={"good": "  "}).status_code == 400
    r = client.post("/feedback", data={"good": "a" * (web.FEEDBACK_MAX + 1)})
    assert r.status_code == 400 and "a" * 50 in r.text  # input kept for editing
    assert db.list_feedback(conn) == []


def test_feedback_rate_limited(client, conn):
    for _ in range(web.FEEDBACK_PER_IP_PER_HOUR):
        assert client.post("/feedback", data={"good": "hi"}).status_code == 200
    assert client.post("/feedback", data={"good": "hi"}).status_code == 429
    assert len(db.list_feedback(conn)) == web.FEEDBACK_PER_IP_PER_HOUR


def test_pages_load_vercel_analytics(client):
    r = client.get("/")
    assert '<script defer src="/_vercel/insights/script.js"></script>' in r.text
    assert 'href="/feedback"' in r.text


def test_feedback_pings_admin(client, conn, discord, monkeypatch):
    monkeypatch.setenv("ADMIN_WEBHOOK_URL", hook_url(9))
    r = client.post("/feedback", data={"good": "fast", "features": "x" * web.FEEDBACK_MAX})
    assert r.status_code == 200
    [req] = posts(discord)
    assert str(req.url) == hook_url(9)
    body = json.loads(req.content)
    assert body["allowed_mentions"] == {"parse": []}
    fields = body["embeds"][0]["fields"]
    assert [f["name"] for f in fields] == ["What's good", "Feature requests"]
    assert fields[0]["value"] == "fast" and len(fields[1]["value"]) == 1024


def test_feedback_saved_when_admin_ping_fails_or_unset(client, conn, discord, monkeypatch):
    monkeypatch.delenv("ADMIN_WEBHOOK_URL", raising=False)
    assert client.post("/feedback", data={"good": "a"}).status_code == 200
    assert posts(discord) == []
    monkeypatch.setenv("ADMIN_WEBHOOK_URL", hook_url(9))
    discord.post(url__regex=r".*").respond(500)
    assert client.post("/feedback", data={"good": "b"}).status_code == 200
    assert len(db.list_feedback(conn)) == 2


def test_feedback_redirects_so_reload_does_not_resubmit(client, conn, discord, monkeypatch):
    monkeypatch.setenv("ADMIN_WEBHOOK_URL", hook_url(9))
    r = client.post("/feedback", data={"good": "once"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/feedback?sent=1"
    for _ in range(3):  # reloading the thanks page is a GET: no new row, no new ping
        page = client.get(r.headers["location"])
        assert page.status_code == 200 and "Thanks" in page.text
    assert len(db.list_feedback(conn)) == 1 and len(posts(discord)) == 1


def test_header_faq_and_cache_busted_css(client):
    page = client.get("/faq").text
    assert "How fast do alerts arrive?" in page
    assert '<a href="/faq" aria-current="page">FAQ</a>' in page and 'class="brand"' in page
    assert re.search(r'/static/style\.css\?v=[0-9a-f]{10}"', page)
    assert "Built by" in page and 'class="star"' in page


def test_home_shows_last_checked_from_worker_scan(client, conn, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from jobhunt.web import app as web
    monkeypatch.setattr(web, "_last_scan", (0.0, None))
    assert "Last checked" not in client.get("/").text  # no scan yet: line hidden
    db.set_meta(conn, "last_scan_at", db.to_iso(datetime.now(timezone.utc) - timedelta(minutes=12)))
    monkeypatch.setattr(web, "_last_scan", (0.0, None))
    assert "Last checked 12 minutes ago." in client.get("/").text


def test_manage_test_alert_posts_sample_and_shares_daily_cap(client, discord, conn):
    subscribe(client)
    t = token_from(discord)  # confirmation used 1 of 3 daily messages
    for _ in range(2):
        assert "Test alert sent" in manage(client, t, "test").text
    body = json.loads(posts(discord)[-1].content)["embeds"][0]
    assert body["title"].startswith("jobhunt") and "Real alerts look like this" in body["description"]
    assert any(f["name"] == "Job ID" for f in body["fields"])
    r = manage(client, t, "test")
    assert r.status_code == 429 and "Daily limit" in r.text and len(posts(discord)) == 3


def test_manage_page_loads_no_analytics(client, discord):
    subscribe(client)
    assert "/_vercel/insights/script.js" in client.get("/").text
    assert "/_vercel/insights/script.js" not in client.get(f"/manage?t={token_from(discord)}").text


def test_vercel_uploads_the_watchlist_files_the_companies_page_reads():
    from pathlib import Path
    allow = Path(__file__).parents[1].joinpath(".vercelignore").read_text().split()
    assert "!config.yaml" in allow and "!companies.resolved.yaml" in allow
