"""jobhunt web: paste a Discord webhook, confirm via a private link posted into the channel.

No accounts, no cookies, no JavaScript. Possession of the manage token (readable only by
people who can read the channel) is the only credential. See docs/architecture.md.
"""

from __future__ import annotations

import hmac
import ipaddress
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import httpx
import jinja2
from fastapi import FastAPI, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.templating import Jinja2Templates

from .. import crypto, db, ingest, webhooks
from ..models import Job

HERE = Path(__file__).parent
COUNTRIES = ("CA", "US")
LEVELS = ("internship", "coop")
SUBSCRIBE_PER_IP_PER_HOUR = 10
MESSAGES_PER_WEBHOOK_PER_DAY = 3
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
    return _page(request, "index.html", error=None, **_grad_ctx())


@app.get("/privacy", response_class=HTMLResponse)
def privacy(request: Request):
    return _page(request, "privacy.html")


@app.get("/terms", response_class=HTMLResponse)
def terms(request: Request):
    return _page(request, "terms.html")


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


@app.post("/subscribe", response_class=HTMLResponse)
def subscribe(
    request: Request,
    webhook_url: str = Form(""),
    countries: list[str] = Form([]),
    levels: list[str] = Form([]),
    grad_month: str = Form(""),
    grad_year: str = Form(""),
):
    conn = _open()
    try:
        ip_key = crypto.keyed_hash("ip:" + _client_ip(request))
        if not db.rate_limit_hit(conn, ip_key, SUBSCRIBE_PER_IP_PER_HOUR):
            return _page(request, "check.html")
        prefs = _prefs(countries, levels)
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
            created = True
        else:
            sub_id = sub.id
        if not db.take_verify_slot(conn, sub_id, MESSAGES_PER_WEBHOOK_PER_DAY):
            return _page(request, "check.html")

        posted = _discord_post(wh, {
            "title": "Activate your jobhunt alerts",
            "description": (
                "Someone asked for SWE internship and co-op alerts in this channel. "
                f"To activate them, open this private link and press Activate:\n"
                f"{_base_url()}/manage?t={token}\n\n"
                "Anyone who can read this channel can use the link. "
                "If this was not you, ignore it: nothing is sent until it is activated."
            ),
            "color": 0x5865F2,
        })
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
                error, status = "Pick at least one country and one level.", 400
            elif not grad_ok:
                error, status = "Pick a valid graduation month and year, or leave both blank.", 400
            else:
                db.set_prefs(conn, sub.id, *prefs)
                db.set_grad_date(conn, sub.id, grad)
                notice = "Preferences saved."
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
