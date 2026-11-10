"""Read-only admin dashboard: stdlib HTTP server on 127.0.0.1, reached via an SSH tunnel.

Aggregates plus a masked subscriber table only. Never a webhook URL, ciphertext, token or hash.
"""

from __future__ import annotations

from html import escape
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable

from . import crypto, webhooks
from . import db as v2db

BIND_HOST = "127.0.0.1"  # hard-coded on purpose: no option to expose it

_CSS = (
    "body{font:14px system-ui,sans-serif;margin:1.5rem;max-width:64rem}"
    "table{border-collapse:collapse;margin:.5rem 0 1.5rem}"
    "th,td{border:1px solid #bbb;padding:.2rem .6rem;text-align:left}"
    "th{background:#eee}td.n{text-align:right}"
)
_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
}


def _masked(webhook_enc: str) -> str:
    try:
        return webhooks.mask(webhooks.parse(crypto.decrypt_url(webhook_enc)).id)
    except Exception:  # noqa: BLE001 - key missing/wrong: still list, never fail
        return "?"


def _table(head: list[str], rows: list[list]) -> str:
    th = "".join(f"<th>{escape(h)}</th>" for h in head)
    body = "".join(
        "<tr>" + "".join(f"<td>{escape(str(c))}</td>" for c in r) + "</tr>" for r in rows
    )
    return f"<table><tr>{th}</tr>{body}</table>"


def _scan_sections(last_run: dict | None) -> list[str]:
    """'Last scan' + 'Hosts' + slowest companies, from logs/last_run.json (all escaped)."""
    if not last_run or "hosts" not in last_run:
        return ["<h2>Last scan</h2><p>No fetch measurements yet.</p>"]
    g = lambda k, d=0: last_run.get(k, d)  # noqa: E731
    out = [
        "<h2>Last scan</h2>",
        _table(
            ["Duration (s)", "Ingest (s)", "Scanned", "Deferred", "Errored", "Deadline hit",
             "Breaker-tripped hosts"],
            [[g("total_seconds"), g("ingest_seconds"), g("companies_scanned"),
              g("companies_deferred"), g("companies_errored"),
              "yes" if g("deadline_hit", False) else "no",
              ", ".join(map(str, g("breaker_hosts", []))) or "-"]],
        ),
        "<h2>Hosts</h2>",
    ]
    rows = [
        [h, v.get("requests", 0), v.get("429", 0), v.get("403", 0), v.get("5xx", 0),
         v.get("retries", 0), v.get("retry_after_honored", 0), v.get("skipped_by_breaker", 0)]
        for h, v in sorted((last_run.get("hosts") or {}).items())
    ]
    out.append(_table(["Host", "Requests", "429", "403", "5xx", "Retries", "Retry-After honored",
                       "Breaker skips"], rows))
    slow = (last_run.get("slowest_companies") or [])[:10]
    out.append("<h2>Slowest companies</h2>")
    out.append(_table(["Company", "Host", "Seconds", "Jobs", "Error"],
                      [[c.get("name", "?"), c.get("host", "?"), c.get("seconds", 0),
                        c.get("jobs", 0), c.get("error") or "-"] for c in slow]))
    return out


def _llm_section(llm: dict | None) -> list[str]:
    """'LLM tie-break (today)': state, calls, tokens, spend vs the daily cap."""
    if not llm:
        return []
    return [
        "<h2>LLM tie-break (today, UTC)</h2>",
        _table(["State", "Calls", "Input tokens", "Output tokens", "Spend (USD)", "Daily cap"],
               [[llm["status"], llm["calls"], llm["input_tokens"], llm["output_tokens"],
                 f"{llm['usd']:.4f}", f"{llm['cap']:.2f}"]]),
    ]


def render_page(
    conn, last_cron: str, last_scan: str, last_run: dict | None = None, llm: dict | None = None
) -> str:
    counts = v2db.subscriber_status_counts(conn)
    tot = v2db.totals(conn)
    days = v2db.daily_activity(conn, 14)
    subs = v2db.list_subscribers(conn)
    h = [
        "<!doctype html><html lang=en><meta charset=utf-8><title>jobhunt admin</title>",
        f"<style>{_CSS}</style><h1>jobhunt admin</h1>",
        f"<p>Last cron fire: {escape(last_cron)}<br>Last completed scan: {escape(last_scan)}</p>",
        *_scan_sections(last_run),
        *_llm_section(llm),
        "<h2>Totals</h2>",
        _table(["Postings stored", "Active subscribers", "Deliveries sent (all time)"],
               [[tot["postings"], counts["active"], tot["sent"]]]),
        "<h2>Subscribers by status</h2>",
        _table(list(counts), [list(counts.values())]),
        "<h2>Last 14 days (UTC)</h2>",
        _table(["Day", "Signups", "Activations", "Sent", "Failed", "Postings first seen"],
               [[d["day"], d["signups"], d["activations"], d["sent"], d["failed"], d["postings"]]
                for d in reversed(days)]),
        "<h2>Subscribers</h2>",
        _table(
            ["Webhook", "Status", "Countries", "Levels", "Created", "Activated", "Last ok",
             "Fails", "Disabled reason"],
            [[_masked(s.webhook_enc), s.status, ",".join(s.countries), ",".join(s.levels),
              (s.created_at or "-")[:19], (s.activated_at or "-")[:19],
              (s.last_ok_at or "-")[:19], s.fail_count, s.disabled_reason or "-"] for s in subs],
        ),
    ]
    return "".join(h)


def make_server(
    port: int, page: Callable[[], str], host: str = BIND_HOST
) -> HTTPServer:
    """Build the server. `host` exists only so tests can assert the default; the CLI never passes it."""

    class Handler(BaseHTTPRequestHandler):
        server_version = "jobhunt-admin"

        def _send(self, code: int, body: str, ctype: str = "text/plain; charset=utf-8") -> None:
            data = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            for k, v in _HEADERS.items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def _host_ok(self) -> bool:
            actual = self.server.server_address[1]
            return (self.headers.get("Host") or "") in {f"127.0.0.1:{actual}", f"localhost:{actual}"}

        def do_GET(self) -> None:  # noqa: N802
            if not self._host_ok():
                return self._send(403, "forbidden\n")
            if self.path != "/":
                return self._send(404, "not found\n")
            try:
                body = page()
            except Exception:  # noqa: BLE001 - never leak details (DSN etc.) to the page
                return self._send(500, "error\n")
            self._send(200, body, "text/html; charset=utf-8")

        def _method_not_allowed(self) -> None:
            self.send_response(405)
            self.send_header("Allow", "GET")
            self.send_header("Content-Length", "0")
            for k, v in _HEADERS.items():
                self.send_header(k, v)
            self.end_headers()

        do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _method_not_allowed

        def log_message(self, fmt, *args) -> None:  # quiet; paths carry nothing but keep it off stderr
            pass

    return HTTPServer((host, port), Handler)
