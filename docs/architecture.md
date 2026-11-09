# jobhunt v2 architecture (current state)

Goal: a user gives a Discord webhook URL; jobhunt sends them SWE-and-adjacent internship/co-op
alerts for Canada and the US. No LLM anywhere. Running cost target: $0.

Built so far: the multi-user core and the website (FastAPI on Vercel, `src/jobhunt/web/`). The
Oracle worker deployment comes in a later phase. History: `docs/v2-phase0.md`.

## Pipeline (`jobhunt scan`)

1. `ingest` - fetch every company board, store postings not seen before. Stale ones are stored
   and tagged ineligible.
2. `classify_new` - deterministic rules (`classify.py`): level, SWE category, seniority and years
   excludes, CA/US location. No LLM.
3. `match_subscribers` - per active subscriber: eligible postings first seen at/after activation
   whose level is in `levels` and country in `countries`. Opaque locations ("Multiple Locations")
   and country-less remote roles pass. Best first: location tier, then company priority.
4. `deliver` - decrypt the stored URL, re-parse it, build the request URL from (id, token),
   post 10 embeds per message, at most 30 alerts per subscriber per cycle.
5. Retention - postings first seen more than 90 days ago AND not seen on the board for 90 days are deleted (tags and deliveries cascade). Postings still open stay, so they are never re-alerted.

Delivery is at-least-once: a `pending` delivery older than 1 hour is retried (a crash between
reserve and send cannot lose alerts; it can at worst duplicate one). Discord 404 or 401 disables
the subscriber at once. Any other failure counts one failed cycle; 5 in a row disable. A success
resets `fail_count` and sets `last_ok_at`.

`jobhunt heartbeat` posts to `ADMIN_WEBHOOK_URL` only: active subscribers, postings in 24h,
deliveries sent and failed in 24h, plus cron liveness.

## Database (`db.py`, schema version 2)

SQLite by default (local dev, tests). Postgres when `DATABASE_URL` is set (psycopg 3,
`prepare_threshold=None` for Supabase's pooler). One API; `?` placeholders are translated for
Postgres; only DDL differs. JSON is TEXT, booleans are INTEGER, timestamps are fixed-width
ISO-8601 UTC TEXT. `jobhunt db init` creates the schema (idempotent).

| Table | Purpose |
|---|---|
| `postings` | one row per job; PK `uid` |
| `posting_tags` | 1:1 classification (eligible, level, countries, ...) |
| `subscribers` | id, `webhook_hash` (UNIQUE), `webhook_enc`, `manage_token_hash` (UNIQUE, nullable), `status` (pending/active/paused/disabled), `disabled_reason`, `countries`, `levels`, `created_at`, `activated_at`, `last_ok_at`, `fail_count`, `verify_count`, `verify_window_start` |
| `deliveries` | PK (`subscriber_id`, `uid`), `status` (pending/sent/failed), `attempts`, `updated_at`, `sent_at`; cascades from both parents |
| `rate_limits` | `key_hash`, `window_start`, `count` (per-IP subscribe limit; rows older than 2 h are pruned on write) |

## Secrets

One master key, `JOBHUNT_SECRET_KEY` (urlsafe base64, 32 bytes; `jobhunt keygen`). `crypto.py`
derives two keys from it with HKDF-SHA256 and distinct labels: a Fernet key (webhook encryption)
and an HMAC key (`webhook_hash`, so duplicates are detected without storing plaintext). Manage
tokens are random; only their SHA-256 is stored. A missing or malformed key fails fast with a
clear error. Webhook URLs are never printed or logged; CLI output shows only the last 4 digits
of the webhook id.

`webhooks.py` accepts only `https://{discord.com,discordapp.com,ptb.discord.com,canary.discord.com}
/api/webhooks/<17-20 digit id>/<60-80 char token>` (optional trailing slash; no query, fragment,
userinfo or port) and normalizes to `https://discord.com/api/webhooks/<id>/<token>`.

## Web flow (`src/jobhunt/web/`)

FastAPI + Jinja2 (autoescape on), one CSS file, plain forms, no JavaScript, no cookies, no accounts.
Vercel Hobby serves it from the root `app.py` (which adds `src/` to the path and exports `app`);
`requirements.txt` is web-only; `.vercelignore` is an allowlist (`app.py`, `requirements.txt`,
`.python-version`, `vercel.json`, `src`) and `vercel.json` `excludeFiles` (documented Python-runtime
setting) is the backstop so databases, secrets, tests and docs stay out of the bundle even if
`.vercelignore` is not honored on Git deploys. `pyproject.toml` also lists the web deps, so either
manifest yields a working install. Python is pinned to 3.12 via `.python-version` (Vercel supports
3.12-3.14; zero-config detection finds `app` in `app.py`, so no routing config is needed).
Unverified until the first real deploy: pooler URL connectivity and `.vercelignore` behavior. The worker (Oracle VM) and the
web app share one Supabase Postgres; the web app uses the transaction pooler (port 6543, no
prepared statements, which `db.connect` already disables) and opens one short-lived connection per
request (schema check once per process).

| Route | Behaviour |
|---|---|
| `GET /` | what it does, 3-step webhook guide, form (webhook URL, countries, levels; all checked) |
| `POST /subscribe` | count the IP, parse the URL (`webhooks.py`), `GET` the webhook on Discord (5 s) to confirm it exists. New webhook: insert `pending`. Known webhook: rotate its manage token, keep prefs. Post one embed with `{PUBLIC_BASE_URL}/manage?t=<token>` into the channel. Always the same "check your Discord channel" page. Bad URL, missing prefs or a 404 from Discord: one generic 400 on the form. |
| `GET /manage?t=` | status, prefs form, Activate / Pause / Resume / Delete. Never changes state (link-preview crawlers fetch GET). `Cache-Control: no-store`. |
| `POST /manage` | token in a hidden field; actions `activate` (pending), `update`, `pause` (active), `resume` (paused), `delete`. Delete removes the subscriber (deliveries cascade) and best-effort posts "unsubscribed"; the Discord webhook itself is never deleted. |
| `GET /privacy`, `/terms` | short and honest |
| `GET /healthz` | `SELECT 1`; `ok` or `error`, no details |

Proof of channel ownership: the private link only exists inside the channel, so only someone who can
read it can press Activate. The manage token is 32 random bytes (urlsafe); only its SHA-256 is
stored. Resubscribing a known webhook rotates it, so old links stop working. A `disabled`
subscriber that resubscribes (and verifies) returns to `pending`.

Abuse limits (`rate_limits` table and `subscribers.verify_*`): 10 `POST /subscribe` per IP per
hour (key = HMAC of the IP from `X-Forwarded-For`, which Vercel sets), and 3 channel messages per
webhook per 24 h. Over a limit the same generic page is returned and nothing is posted; a webhook
over its limit is not rotated either.

Every response carries `Content-Security-Policy: default-src 'self'`, `X-Frame-Options: DENY`,
`Referrer-Policy: no-referrer`, `X-Content-Type-Options: nosniff`. The webhook URL is never echoed;
the token appears only in the manage page's own hidden fields. Unhandled errors return a generic
500 page and log only the exception class. Note: the token travels in the `GET /manage?t=` query
string, so it can appear in hosting request logs; treat those logs as sensitive.

Web env vars: `DATABASE_URL` (Supabase transaction pooler), `JOBHUNT_SECRET_KEY`,
`PUBLIC_BASE_URL`.

## CLI

`scan`, `heartbeat`, `list-matches`, `db init`, `keygen`,
`subscriber add --webhook-env VAR --countries CA,US --levels internship,coop` (admin: validates,
encrypts, activates now), `subscriber list`, `subscriber remove ID`. `--db` takes a SQLite path or
a postgres URL; default is `DATABASE_URL` if set, else `jobhunt_v2.db`.

## Environment variables

| Var | Used by |
|---|---|
| `JOBHUNT_SECRET_KEY` | encrypt/decrypt/hash webhooks (required for non-dry-run scan, `subscriber add`) |
| `DATABASE_URL` | Postgres connection; unset = SQLite. Web: Supabase transaction pooler URL (6543) |
| `PUBLIC_BASE_URL` | web only: origin used in the manage links |
| `ADMIN_WEBHOOK_URL` | heartbeat destination (admin channel) |
| `JOBHUNT_CONTACT` | optional contact in the scraper User-Agent |
| `JOBHUNT_TEST_DATABASE_URL` | tests only; enables the Postgres test runs |

## Tests

`.venv/bin/pytest -q` runs on SQLite. With `JOBHUNT_TEST_DATABASE_URL=postgresql://localhost:54329/jobhunt_test`
the db/pipeline tests also run on Postgres (local server in `.pgdata/`, gitignored).
