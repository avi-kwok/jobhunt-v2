"""jobhunt web: paste a Discord webhook, confirm via a private link posted into the channel.

No accounts, no cookies, no JavaScript of our own (only Vercel Web Analytics' script). Possession of the manage token (readable only by
people who can read the channel) is the only credential. See docs/architecture.md.
"""

from __future__ import annotations

import functools
import hashlib
import hmac
import ipaddress
import logging
import os
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import jinja2
from fastapi import FastAPI, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import (HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse,
                               Response)
from fastapi.templating import Jinja2Templates

from .. import crypto, db, ingest, notifier, webhooks
from ..models import Job

HERE = Path(__file__).parent
ROOT = HERE.parents[2]  # repo root: config.yaml, companies.resolved.yaml
NEW_COMPANY_DAYS = 30
COUNTRIES = ("CA", "US")
LEVELS = ("internship", "coop")
SUBSCRIBE_PER_IP_PER_HOUR = 10
MESSAGES_PER_WEBHOOK_PER_DAY = 3
FEEDBACK_PER_IP_PER_HOUR = 5
FEEDBACK_MAX = 2000
INGEST_PER_HOUR = 60  # rate_limit_hit is a fixed one-hour window
INGEST_MIN_SECRET = 32
DISCORD_TIMEOUT = 5.0
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
    "Strict-Transport-Security": "max-age=63072000; includeSubDomains",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}

log = logging.getLogger("jobhunt.web")
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
templates = Jinja2Templates(
    env=jinja2.Environment(loader=jinja2.FileSystemLoader(HERE / "templates"), autoescape=True)
)
_CSS = (HERE / "static" / "style.css").read_bytes()
# Changes with the stylesheet, so browsers fetch a new copy right after a deploy.
templates.env.globals["css_v"] = hashlib.sha256(_CSS).hexdigest()[:10]
LAST_SCAN_TTL = 60  # seconds the home page reuses the "last checked" lookup
_last_scan: tuple[float, datetime | None] = (0.0, None)
_schema_ready: set[str] = set()


# ---- plumbing --------------------------------------------------------------------------


def _open():
    target = os.environ.get("DATABASE_URL") or str(db.DEFAULT_DB_PATH)
    conn = db.connect(target, ensure_schema=target not in _schema_ready)
    _schema_ready.add(target)
    return conn


def _base_url() -> str:
    return (os.environ.get("PUBLIC_BASE_URL") or "http://localhost:8000").rstrip("/")


def _page(request: Request, name: str, status: int = 200, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, name, ctx, status_code=status)


def _message(request: Request, text: str, status: int = 200) -> HTMLResponse:
    return _page(request, "message.html", status, text=text)


@app.middleware("http")
async def _headers(request: Request, call_next):
    try:
        response = await call_next(request)
    except Exception as exc:  # never leak details (or secrets inside them) to the client
        log.error("unhandled %s on %s", type(exc).__name__, request.url.path)
        response = _message(request, "Something went wrong. Please try again later.", 500)
    for k, v in SECURITY_HEADERS.items():
        response.headers[k] = v
    if request.url.path == "/manage":
        response.headers["Cache-Control"] = "no-store"
    return response


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")  # Vercel overwrites this with the real IP
    raw = fwd.split(",")[0].strip() or (request.client.host if request.client else "unknown")
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return raw
    if ip.version == 6:
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{raw}/64", strict=False))  # one user owns a /64
    return str(ip)


def _prefs(countries: list[str], levels: list[str]) -> tuple[list[str], list[str]] | None:
    c = [x for x in COUNTRIES if x in countries]
    l = [x for x in LEVELS if x in levels]
    return (c, l) if c and l else None


def _grad_years() -> list[int]:
    y = datetime.now(timezone.utc).year
    return list(range(y, y + 7))


def _grad_date(month: str, year: str) -> tuple[bool, str | None]:
    """(valid, "YYYY-MM" or None). Both blank = no filter; anything else must be in range."""
    month, year = month.strip(), year.strip()
    if not month and not year:
        return True, None
    if not (month.isascii() and month.isdigit() and year.isascii() and year.isdigit()):
        return False, None
    if not 1 <= int(month) <= 12 or int(year) not in _grad_years():
        return False, None
    return True, f"{int(year):04d}-{int(month):02d}"


def _grad_ctx(current: str | None = None) -> dict:
    cur = current if current and len(current) == 7 else ""
    return {"grad_years": _grad_years(), "grad_year_sel": cur[:4], "grad_month_sel": cur[5:].lstrip("0")}


def _discord_post(wh: webhooks.Webhook, embed: dict) -> bool:
    """Best-effort post into the channel. Returns success; never raises or logs the URL."""
    try:
        r = httpx.post(wh.url, json={"embeds": [embed], "allowed_mentions": {"parse": []}},
                       timeout=DISCORD_TIMEOUT)
        return r.status_code < 300
    except httpx.HTTPError:
        return False


def _webhook_exists(wh: webhooks.Webhook) -> bool:
    try:
        return httpx.get(wh.url, timeout=DISCORD_TIMEOUT).status_code == 200
    except httpx.HTTPError:
        return False


# ---- public pages ----------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return _page(request, "index.html", error=None, last_checked=_last_checked(), **_grad_ctx())


def _last_checked() -> str | None:
    """'N minutes ago' for the worker's last finished scan; None if unknown (page still renders)."""
    global _last_scan
    if time.monotonic() - _last_scan[0] > LAST_SCAN_TTL:
        try:
            conn = _open()
            try:
                raw = db.get_meta(conn, "last_scan_at")
            finally:
                conn.close()
            _last_scan = (time.monotonic(), datetime.fromisoformat(raw) if raw else None)
        except Exception as exc:  # the home page must not fail because the DB is slow or down
            log.warning("last_scan_at lookup failed: %s", type(exc).__name__)
            return None
    when = _last_scan[1]
    if when is None:
        return None
    mins = max(0, int((datetime.now(timezone.utc) - when).total_seconds() // 60))
    if mins < 1:
        return "just now"
    if mins < 120:
        return f"{mins} minute{'s' * (mins != 1)} ago"
    return f"{mins // 60} hours ago"


@app.get("/faq", response_class=HTMLResponse)
def faq(request: Request):
    return _page(request, "faq.html")


@functools.lru_cache(maxsize=1)
def _tracked_companies() -> tuple[list[str], list[tuple[date, str]]]:
    """(all tracked names, [(added, name)]); the config files only change with a deploy."""
    from ..config import load_config  # lazy: only this page needs the watchlist
    cos = [c for c in load_config(ROOT / "config.yaml", ROOT / "companies.resolved.yaml").companies
           if (c.ats or c.covered_by) and c.ats != "simplify"]
    return sorted((c.name for c in cos), key=str.casefold), [(c.added, c.name) for c in cos if c.added]


@app.get("/companies", response_class=HTMLResponse)
def companies(request: Request):
    tracked, added = _tracked_companies()
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=NEW_COMPANY_DAYS)
    new = sorted((n for d, n in added if d >= cutoff), key=str.casefold)
    return _page(request, "companies.html", tracked=tracked, new=new, new_days=NEW_COMPANY_DAYS)


@app.get("/privacy", response_class=HTMLResponse)
def privacy(request: Request):
    return _page(request, "privacy.html")


@app.get("/terms", response_class=HTMLResponse)
def terms(request: Request):
    return _page(request, "terms.html")


@app.get("/feedback", response_class=HTMLResponse)
def feedback_get(request: Request, sent: str = ""):
    if sent:
        return _message(request, "Thanks! Your feedback was sent.")
    return _page(request, "feedback.html", error=None, form={})


@app.post("/feedback", response_class=HTMLResponse)
def feedback_post(
    request: Request,
    good: str = Form(""),
    improve: str = Form(""),
    features: str = Form(""),
):
    form = {"good": good.strip(), "improve": improve.strip(), "features": features.strip()}
    if not any(form.values()):
        return _page(request, "feedback.html", 400, error="Fill in at least one box.", form=form)
    if any(len(v) > FEEDBACK_MAX for v in form.values()):
        return _page(request, "feedback.html", 400,
                     error=f"Keep each box under {FEEDBACK_MAX} characters.", form=form)
    conn = _open()
    try:
        if not db.rate_limit_hit(conn, crypto.keyed_hash("fb:" + _client_ip(request)),
                                 FEEDBACK_PER_IP_PER_HOUR):
            return _message(request, "Too much feedback from here this hour. Try again later.", 429)
        db.add_feedback(conn, form["good"], form["improve"], form["features"])
    finally:
        conn.close()
    _notify_admin_feedback(form)
    # Redirect-after-POST: reloading the thanks page must not resubmit (and re-ping admin).
    return RedirectResponse("/feedback?sent=1", status_code=303)


def _notify_admin_feedback(form: dict) -> None:
    """Best-effort ping to #jobhunt-admin; feedback is already saved, so failures are ignored."""
    try:
        admin = webhooks.parse(os.environ.get("ADMIN_WEBHOOK_URL", ""))
    except webhooks.WebhookError:
        return
    labels = (("good", "What's good"), ("improve", "Needs improvement"), ("features", "Feature requests"))
    fields = [{"name": label, "value": form[k][:1024]} for k, label in labels if form[k]]  # Discord field cap
    _discord_post(admin, {"title": "New feedback", "fields": fields,
                          "footer": {"text": "jobhunt feedback list"}})


@app.get("/static/style.css", include_in_schema=False)
def css():
    return Response(_CSS, media_type="text/css", headers={"Cache-Control": "public, max-age=3600"})


@app.get("/healthz", response_class=PlainTextResponse)
def healthz():
    try:
        conn = _open()
        try:
            conn.execute("SELECT 1").fetchone()
        finally:
            conn.close()
        return PlainTextResponse("ok")
    except Exception:
        return PlainTextResponse("error", status_code=503)


# ---- subscribe -------------------------------------------------------------------------

_BAD = "We could not use that webhook. Check the URL and try again."
_NO_PREFS = "Pick at least one country and one level."


@app.post("/subscribe", response_class=HTMLResponse)
def subscribe(
    request: Request,
    webhook_url: str = Form(""),
    countries: list[str] = Form([]),
    levels: list[str] = Form([]),
    grad_month: str = Form(""),
    grad_year: str = Form(""),
    updates: str = Form(""),
):
    conn = _open()
    try:
        ip_key = crypto.keyed_hash("ip:" + _client_ip(request))
        if not db.rate_limit_hit(conn, ip_key, SUBSCRIBE_PER_IP_PER_HOUR):
            return _page(request, "check.html")
        prefs = _prefs(countries, levels)
        if prefs is None:  # an all-unticked signup would never get an alert
            return _page(request, "index.html", 400, error=_NO_PREFS, **_grad_ctx())
        grad_ok, grad = _grad_date(grad_month, grad_year)
        try:
            wh = webhooks.parse(webhook_url)
        except webhooks.WebhookError:
            wh = None
        if wh is None or prefs is None or not grad_ok or not _webhook_exists(wh):
            return _page(request, "index.html", 400, error=_BAD, **_grad_ctx())

        h = crypto.webhook_hash(wh.url)
        token = crypto.new_token()
        sub = db.get_subscriber_by_hash(conn, h)
        created = False
        if sub is None:
            sub_id = db.add_subscriber(
                conn, h, crypto.encrypt_url(wh.url), prefs[0], prefs[1],
                manage_token_hash=crypto.token_hash(token), grad_date=grad,
            )
            if sub_id is None:  # lost a race with a concurrent subscribe: the winner posts
                return _page(request, "check.html")
            if not updates:
                db.set_updates_opt_out(conn, sub_id, True)
            created = True
        else:
            sub_id = sub.id
        if not db.take_verify_slot(conn, sub_id, MESSAGES_PER_WEBHOOK_PER_DAY):
            return _page(request, "check.html")

        link = f"{_base_url()}/manage?t={token}"
        if created or sub.status in ("pending", "disabled"):  # disabled goes back to pending
            embed = {
                "title": "Activate your jobhunt alerts",
                "description": (
                    "Someone asked for SWE internship and co-op alerts in this channel. "
                    f"To activate them, open this private link and press Activate:\n{link}\n\n"
                    "Anyone who can read this channel can use the link. "
                    "If this was not you, ignore it: nothing is sent until it is activated."
                ),
                "color": 0x5865F2,
            }
        else:  # already subscribed: the form doubles as "send me a new manage link"
            embed = {
                "title": "Your new jobhunt manage link",
                "description": (
                    "Someone asked for a new link to manage this channel's jobhunt alerts "
                    f"(countries, levels, graduation date, updates, pause, delete):\n{link}\n\n"
                    "The old link no longer works. Anyone who can read this channel can use "
                    "the new one. Your current settings are unchanged."
                ),
                "color": 0x5865F2,
            }
        posted = _discord_post(wh, embed)
        if not posted:
            db.refund_verify_slot(conn, sub_id)
            if created:  # nothing existed before, so failing visibly leaks nothing
                db.remove_subscriber(conn, sub_id)
                return _page(request, "index.html", 400, error=_BAD, **_grad_ctx())
            return _page(request, "check.html")  # existing: keep old token, hide existence
        if not created:
            db.rotate_manage_token(conn, sub_id, crypto.token_hash(token))
        return _page(request, "check.html")
    finally:
        conn.close()


# ---- manage ----------------------------------------------------------------------------

_GONE = "This link is not valid any more. Request a new one from the home page."


def _lookup(conn, token: str):
    if not token or len(token) > 100:
        return None
    return db.get_subscriber_by_token(conn, crypto.token_hash(token))


def _manage_page(request, sub, token, notice=None, error=None, status=200):
    return _page(request, "manage.html", status, sub=sub, token=token, notice=notice,
                 error=error, countries=COUNTRIES, levels=LEVELS,
                 **_grad_ctx(sub.grad_date))


def _send_test_alert(conn, sub) -> tuple[str | None, int]:
    """Post one sample alert (shares the per-webhook daily message cap with confirmations)."""
    try:
        wh = webhooks.parse(crypto.decrypt_url(sub.webhook_enc))
    except (webhooks.WebhookError, crypto.CryptoError):
        return "We could not read your webhook. Subscribe again from the home page.", 400
    if not db.take_verify_slot(conn, sub.id, MESSAGES_PER_WEBHOOK_PER_DAY):
        return "Daily limit for test messages reached. Try again tomorrow.", 429
    job = Job(source="test", company="jobhunt", external_id="TEST-0001",
              title="Test alert: Software Engineer Intern", location="Toronto, ON, Canada",
              url=_base_url() + "/faq", posted_at=datetime.now(timezone.utc))
    embed = notifier.alert_embed(job, ["CA"], "internship", ["software"])
    embed["description"] = "This is a test from jobhunt. Real alerts look like this."
    if not _discord_post(wh, embed):
        db.refund_verify_slot(conn, sub.id)
        return "Discord did not accept the test message. Check that the webhook still exists.", 502
    return None, 200


@app.get("/manage", response_class=HTMLResponse)
def manage_get(request: Request, t: str = ""):
    conn = _open()
    try:
        sub = _lookup(conn, t)
        if sub is None:
            return _message(request, _GONE, 404)
        return _manage_page(request, sub, t)
    finally:
        conn.close()


@app.post("/manage", response_class=HTMLResponse)
def manage_post(
    request: Request,
    t: str = Form(""),
    action: str = Form(""),
    countries: list[str] = Form([]),
    levels: list[str] = Form([]),
    grad_month: str = Form(""),
    grad_year: str = Form(""),
    updates: str = Form(""),
):
    conn = _open()
    try:
        sub = _lookup(conn, t)
        if sub is None:
            return _message(request, _GONE, 404)
        notice = error = None
        status = 200
        if action == "activate" and sub.status == "pending":
            db.activate_subscriber(conn, sub.id)
            notice = "Alerts are on. New matching postings will be sent to your channel."
        elif action == "resume" and sub.status == "paused":
            db.activate_subscriber(conn, sub.id)
            notice = "Alerts resumed."
        elif action == "pause" and sub.status == "active":
            db.pause_subscriber(conn, sub.id)
            notice = "Alerts paused."
        elif action == "update":
            prefs = _prefs(countries, levels)
            grad_ok, grad = _grad_date(grad_month, grad_year)
            if prefs is None:
                error, status = _NO_PREFS, 400
            elif not grad_ok:
                error, status = "Pick a valid graduation month and year, or leave both blank.", 400
            else:
                db.set_prefs(conn, sub.id, *prefs)
                db.set_grad_date(conn, sub.id, grad)
                db.set_updates_opt_out(conn, sub.id, not updates)
                notice = "Preferences saved."
        elif action == "test" and sub.status != "disabled":
            error, status = _send_test_alert(conn, sub)
            notice = None if error else "Test alert sent. Check your Discord channel."
        elif action == "delete":
            try:
                wh = webhooks.parse(crypto.decrypt_url(sub.webhook_enc))
            except (webhooks.WebhookError, crypto.CryptoError):
                wh = None
            db.remove_subscriber(conn, sub.id)
            if wh is not None:
                _discord_post(wh, {
                    "title": "Unsubscribed",
                    "description": "jobhunt alerts for this channel were turned off and "
                                   "its data deleted. The webhook itself was not touched.",
                    "color": 0x99AAB5,
                })
            return _message(request, "Unsubscribed. Your data was deleted.")
        else:
            error, status = "That action is not available right now.", 400
        return _manage_page(request, db.get_subscriber(conn, sub.id), t, notice, error, status)
    finally:
        conn.close()


# ---- ingest (external pipeline pushes postings; the worker classifies them) -------------


def _ingest_store(post: ingest.Posting) -> tuple[int, dict]:
    conn = _open()
    try:
        if not db.rate_limit_hit(conn, crypto.keyed_hash("ingest"), INGEST_PER_HOUR):
            return 429, {"error": "rate limit"}
        if db.posting_known(conn, post.uid, post.url_variants):
            return 200, {"status": "duplicate"}
        job = Job(source=ingest.SOURCE, company=post.company, external_id=post.uid.rsplit(":", 1)[1],
                  title=post.title, location=post.location, url=post.url, posted_at=post.posted_at,
                  posted_note=post.posted_note)
        if not db.insert_posting(conn, job):
            return 200, {"status": "duplicate"}
        return 201, {"status": "accepted", "posting_id": post.uid}
    finally:
        conn.close()


async def _read_capped(request: Request) -> bytes | None:
    """The body, or None if it exceeds ingest.MAX_BODY (counts real bytes, not the header)."""
    try:
        if int(request.headers.get("content-length") or 0) > ingest.MAX_BODY:
            return None
    except ValueError:
        return None
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > ingest.MAX_BODY:
            return None
    return bytes(buf)


@app.post("/ingest/posting", include_in_schema=False)
async def ingest_posting(request: Request):
    secret = os.environ.get("INGEST_SECRET", "")
    if os.environ.get("INGEST_ENABLED") != "true" or len(secret) < INGEST_MIN_SECRET:
        return JSONResponse({"error": "disabled"}, status_code=503)
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(token.encode(), secret.encode()):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    raw = await _read_capped(request)
    if raw is None:
        return JSONResponse({"error": "body too large"}, status_code=413)
    try:
        post = ingest.parse(raw)
    except ingest.IngestError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    status, body = await run_in_threadpool(_ingest_store, post)
    return JSONResponse(body, status_code=status)
