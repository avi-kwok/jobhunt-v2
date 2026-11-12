# jobhunt v2 architecture (current state)

Goal: a user gives a Discord webhook URL; jobhunt sends them SWE-and-adjacent internship/co-op
alerts for Canada and the US. No LLM anywhere. Running cost target: $0.

Components: the website (FastAPI on Vercel, `src/jobhunt/web/`), the worker (cron on an Oracle
Always Free VM, `deploy/v2/`) and one Supabase free Postgres they share. First deploy and
operations: `docs/RUNBOOK.md`. History: `docs/v2-phase0.md`.

## Pipeline (`jobhunt scan`)

1. `ingest` - fetch every company board, store postings not seen before. Stale ones are stored
   and tagged ineligible.
2. `classify_new` - deterministic rules (`classify.py`): level, SWE category, seniority and years
   excludes (incl. graduate-degree titles), CA/US location (city gazetteer resolves "City, CA"
   to California vs Canada), term tags ("Summer 2027"). Then, when `ANTHROPIC_API_KEY` is set,
   one Haiku review call per rule-eligible (or ambiguous-title) posting (`llm_tiebreak.py`): it can
   reject non-software or grad-only roles and adds term tags. It never changes country detection;
   on any failure or cap the rules decide.
3. `match_subscribers` - per active subscriber: eligible postings first seen at/after activation
   whose level is in `levels` and country in `countries`. Opaque locations ("Multiple Locations")
   and country-less remote roles pass. Best first: location tier, then company priority.
4. `deliver` - decrypt the stored URL, re-parse it, build the request URL from (id, token),
   post 10 embeds per message, at most 30 alerts per subscriber per cycle.
5. Retention - postings first seen more than 90 days ago AND not seen on the board for 90 days are deleted (tags and deliveries cascade). Postings still open stay, so they are never re-alerted. Subscribers still `pending` (never activated) after 7 days are deleted in the same step.

Delivery is at-least-once: a `pending` delivery older than 1 hour is retried (a crash between
reserve and send cannot lose alerts; it can at worst duplicate one). Discord 404 or 401 disables
the subscriber at once. Any other failure counts one failed cycle; 5 in a row disable. A success
resets `fail_count` and sets `last_ok_at`.

`jobhunt heartbeat` posts to `ADMIN_WEBHOOK_URL` only: active subscribers, postings in 24h,
deliveries sent and failed in 24h, plus cron liveness (mtime of `logs/worker.log`, which cron
appends to on every fire; stale after 90 min).

## Coverage: subsidiary aliases and the Simplify fallback

- `covered_by: <Parent>` on a `config.yaml` company marks a subsidiary whose postings already
  arrive under the parent's board (e.g. Slack -> Salesforce). It is counted as covered, never fetched.
- Companies with no `ats` and no `covered_by` are served by the `simplify` adapter, attached to
  one pseudo-company (`SimplifyJobs` in `companies.resolved.yaml`). The config loader computes its
  allowlist (`simplify_companies`). A company with an `ats` is never on it, so once a company is
  resolved it leaves the fallback automatically: no source overlaps.
- Listings come from the SimplifyJobs `Summer2027-Internships` `listings.json` (thanks to
  SimplifyJobs for maintaining it). Names match after lowercasing and stripping non-alphanumerics;
  `Job.company` is the canonical config name. Only active, visible rows posted in the last
  `SIMPLIFY_MAX_AGE_DAYS = 2` days are emitted, so a first run cannot flood the backlog. Dates are
  day-granular, so `posted_at` is None and freshness comes from first-seen.
- The body and ETag are cached in `logs/simplify_cache.json` / `.etag`; the fetch is conditional
  (`If-None-Match`, 304 reuses the cache). A failed fetch raises; stale data is never served silently.

## Database (`db.py`, schema version 4)

SQLite by default (local dev, tests). Postgres when `DATABASE_URL` is set (psycopg 3,
`prepare_threshold=None` for Supabase's pooler). One API; `?` placeholders are translated for
Postgres; only DDL differs. JSON is TEXT, booleans are INTEGER, timestamps are fixed-width
ISO-8601 UTC TEXT. `jobhunt db init` creates the schema (idempotent).

| Table | Purpose |
|---|---|
| `postings` | one row per job; PK `uid` |
| `posting_tags` | 1:1 classification (eligible, level, countries, `terms` JSON list of "Season YYYY", ...) |
| `subscribers` | id, `webhook_hash` (UNIQUE), `webhook_enc`, `manage_token_hash` (UNIQUE, nullable), `status` (pending/active/paused/disabled), `disabled_reason`, `countries`, `levels`, `created_at`, `activated_at`, `last_ok_at`, `fail_count`, `verify_count`, `verify_window_start`, `grad_date` ("YYYY-MM", NULL = no filter) |
| `deliveries` | PK (`subscriber_id`, `uid`), `status` (pending/sent/failed), `attempts`, `updated_at`, `sent_at`; cascades from both parents |
| `rate_limits` | `key_hash`, `window_start`, `count` (per-IP subscribe limit; rows older than 2 h are pruned on write) |

### Graduation-date filter (optional)

A subscriber may set an expected graduation month (`subscribers.grad_date`, "YYYY-MM"; set on the
signup form or `/manage` with month/year selects, year = current UTC year .. +6). `match.matches`
applies it last: a posting passes if `grad_date` is NULL or malformed, the posting has no `terms`,
or ANY term starts strictly before the first day of the grad month. Term start months: Winter 01,
Spring 03, Summer 05, Fall 09 (of the term's year). Example, grad 2028-04: Summer 2027, Fall 2027,
Winter 2028 pass; Summer 2028 fails. Postings with no stated term are always sent.

Migration v3 -> v4 is additive (`ALTER TABLE subscribers ADD COLUMN IF NOT EXISTS grad_date TEXT` on
Postgres; PRAGMA-guarded `ADD COLUMN` on SQLite) and runs inside `init_schema` on the first
`db.connect()` of any process (web cold start, worker, `jobhunt db init`) whose DB is below the code's
`SCHEMA_VERSION`. Deploy order is therefore safe either way: whichever side connects first migrates;
an old worker against a v4 DB is unaffected (it never selects `grad_date`).

## Data attribution

`src/jobhunt/data/cities_us_ca.tsv` is derived from GeoNames `cities1000` (https://www.geonames.org/),
licensed CC BY 4.0. Rebuild with `python scripts/build_cities.py`. It is loaded lazily on first
location lookup.

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

## External ingest (`POST /ingest/posting`)

An external pipeline can push postings. Off unless `INGEST_ENABLED=true` and `INGEST_SECRET` (>= 32
chars) are set (else 503); auth is a constant-time bearer compare (401). Body <= 4 KB, JSON, field
caps, https-only URL without userinfo (`ingest.py`); 60 requests/hour through `db.rate_limit_hit`.
The web request only inserts the posting untagged with `source = muse-pipeline` and uid
`muse-pipeline:<company>:<sha256(normalized url incl. query)[:32]>`; the client's `country`/`level` are validated
but never used for delivery. The worker's `pipeline.classify_untagged` (each scan, after ingest) tags
postings without a `posting_tags` row with the same freshness gate and rules as scanner postings, so
matching then delivers them normally. Dedup: same uid, or a stored posting whose `url` equals the
original or a normalized variant (http/https, trailing slash). Limitation: a job under a genuinely
different URL form (other host, reordered or extra query params) is not caught. The query string is
kept in the identity. A bare-date `posted_at` is stored as a note only (undated, so fresh); a full
datetime is stored and gated by freshness. `deadline` is accepted but not stored.

## Worker (`deploy/v2/`)

`worker-setup.sh` is idempotent and targets a new Oracle Always Free A1.Flex VM (Ubuntu 22.04 or
24.04, aarch64, IPv4 only): installs Python 3.11+ (deadsnakes on 22.04), `postgresql-client-17`
from the PGDG apt repo (`PG_CLIENT_VERSION` overrides; its major must be >= the server's, check
`SHOW server_version`), clones or pulls the repo to `~/jobhunt-v2`, builds `.venv`, creates `.env`
(mode 600) only if missing, sets the system clock to UTC, and replaces a marker-delimited crontab block:

| Job | Schedule (UTC) | Log |
|---|---|---|
| `flock -n logs/scan.lock timeout 1800 jobhunt scan` | every 30 min | `logs/worker.log` |
| `jobhunt heartbeat` | 16:00 daily | `logs/heartbeat.log` |
| `deploy/v2/backup.sh` (`pg_dump -Fc`, keep 7 newest in `~/backups`) | 09:00 daily | `logs/backup.log` |

The worker connects through the Supabase **session pooler** (port 5432): the direct host can be
IPv6-only, the shared pooler is IPv4. `jobhunt doctor` checks the three required env vars,
database reachability, schema version, and a key encrypt/decrypt round-trip; it prints no secret
values and exits non-zero on any failure. v2 shares no VM, database or webhook with v1.

## CLI

`scan`, `heartbeat`, `list-matches`, `db init`, `keygen`, `doctor`,
`subscriber add --webhook-env VAR --countries CA,US --levels internship,coop` (admin: validates,
encrypts, activates now), `subscriber list`, `subscriber remove ID`. `--db` takes a SQLite path or
a postgres URL; default is `DATABASE_URL` if set, else `jobhunt_v2.db`.

## Environment variables

| Var | Used by |
|---|---|
| `JOBHUNT_SECRET_KEY` | encrypt/decrypt/hash webhooks (web, non-dry-run scan, `subscriber add`, `doctor`); identical on Vercel and the worker |
| `DATABASE_URL` | Postgres connection; unset = SQLite. Web: transaction pooler (6543). Worker: session pooler (5432) |
| `PUBLIC_BASE_URL` | web only: origin used in the manage links |
| `INGEST_SECRET`, `INGEST_ENABLED` | web only: bearer secret (>= 32 chars) and on-switch (`true`) for `POST /ingest/posting`; off by default |
| `ADMIN_WEBHOOK_URL` | heartbeat destination (admin channel), `doctor` |
| `JOBHUNT_CONTACT` | optional contact in the scraper User-Agent |
| `JOBHUNT_TEST_DATABASE_URL` | tests only; enables the Postgres test runs |

## Tests

`.venv/bin/pytest -q` runs on SQLite (includes `doctor`, crontab rendering and backup-retention tests; the `shellcheck` test runs if installed). With `JOBHUNT_TEST_DATABASE_URL=postgresql://localhost:54329/jobhunt_test`
the db/pipeline tests also run on Postgres (local server in `.pgdata/`, gitignored).
