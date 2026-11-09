# jobhunt v2 architecture (current state)

Goal: a user gives a Discord webhook URL; jobhunt sends them SWE-and-adjacent internship/co-op
alerts for Canada and the US. No LLM anywhere. Running cost target: $0.

Built so far: the multi-user core (this phase). The website (FastAPI on Vercel), the onboarding
flow and the Oracle worker deployment come in later phases. History: `docs/v2-phase0.md`.

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
| `rate_limits` | `key_hash`, `window_start`, `count` (for the website, next phase) |

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

## CLI

`scan`, `heartbeat`, `list-matches`, `db init`, `keygen`,
`subscriber add --webhook-env VAR --countries CA,US --levels internship,coop` (admin: validates,
encrypts, activates now), `subscriber list`, `subscriber remove ID`. `--db` takes a SQLite path or
a postgres URL; default is `DATABASE_URL` if set, else `jobhunt_v2.db`.

## Environment variables

| Var | Used by |
|---|---|
| `JOBHUNT_SECRET_KEY` | encrypt/decrypt/hash webhooks (required for non-dry-run scan, `subscriber add`) |
| `DATABASE_URL` | Postgres connection; unset = SQLite |
| `ADMIN_WEBHOOK_URL` | heartbeat destination (admin channel) |
| `JOBHUNT_CONTACT` | optional contact in the scraper User-Agent |
| `JOBHUNT_TEST_DATABASE_URL` | tests only; enables the Postgres test runs |

## Tests

`.venv/bin/pytest -q` runs on SQLite. With `JOBHUNT_TEST_DATABASE_URL=postgresql://localhost:54329/jobhunt_test`
the db/pipeline tests also run on Postgres (local server in `.pgdata/`, gitignored).
