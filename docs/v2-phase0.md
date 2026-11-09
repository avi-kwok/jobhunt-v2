# jobhunt v2 - Phase 0

Refactor of the v1 scanner into discrete stages, with the LLM removed from the alert path
and a new multi-user SQLite schema. The owner is user #1. OAuth, web UI and encryption are
later phases and are not built here.

Scope locked for launch: software engineering and adjacent internships and co-ops only
(no PM track, no data analyst or data science, no new grad or full-time); Canada and US only;
Discord-only delivery; SQLite (WAL) with portable SQL.

## What changed

- `scan` runs `ingest -> classify_new -> match_users -> deliver` against `jobhunt_v2.db`.
  No `llm_match`, no `BudgetGuard`, no `use_llm_filter`, no Anthropic key check in `scan`.
- `matcher.llm_match` and its prompt code are deleted. `prefilter`, `is_fresh`, `dedupe_by_uid`,
  `location_tier`, `composite_key` stay (the first four are still used; `composite_key` and
  `MatchResult` only remain for the old `DiscordNotifier.send`).
- `config.yaml`: the PM-adjacent group (product manager, APM, TPM, product owner, product
  marketing, program manager, ...) was removed from `criteria.role_terms`;
  `exclude_exempt_terms` is now `[]`; `use_llm_filter` is gone (also from `Criteria`).
- The `tailor` and `budget` commands, `tailor/` and `budget.py` are untouched.
- dotenv fix: `security.resolve_env_path()` (cwd-based `find_dotenv(usecwd=True)`) is the single
  `.env` path. `security.load_env()` loads it, and `audit()` / `preflight()` / `harden_*` default to
  it. The CLI no longer calls bare `load_dotenv()`.

## New modules

| Module | Role |
|---|---|
| `db.py` | v2 schema, `connect()` (WAL, foreign keys ON, 30 s busy timeout), data-access helpers |
| `classify.py` | deterministic `classify(job, criteria, location_tiers) -> Classification`, `RULES_VERSION = 1` |
| `match.py` | `matches(prefs, tags, company=None)` and `sort_key(...)` |
| `pipeline.py` | `ingest`, `classify_new`, `match_users`, `deliver` as plain functions |
| `notifier.py` | adds `DiscordHTTPError` (carries status), `alert_embed`, `send_embeds` (10 embeds per message) |

## Schema (version 1)

`postings`, `posting_tags` (1:1, cascade), `users`, `destinations`, `prefs`, `deliveries`
(PK `(user_id, uid)`), `schema_version`. TEXT/INTEGER only, arrays are JSON text, timestamps
are fixed-width ISO-8601 UTC strings. Phase 0 destinations are `kind='webhook_env'` and `ref`
holds the NAME of an env var, never the URL.

## Classifier rules

- Word-boundary, case-insensitive matching with `-`/space interchangeable. "Internal",
  "International", "Cooperative" are not intern/co-op. Level: co-op terms -> `coop`, else
  intern/internship/student/campus -> `internship`.
- Category `swe`: config `role_terms` minus any term containing the words product/program/tpm/apm,
  plus mobile, ios, android, embedded, firmware, machine learning engineer, ml engineer,
  data engineer, security engineer. Role terms also match a trailing `s`/`ing`.
- Excludes: config `exclude_terms` + senior, sr, staff, principal, lead, manager, director,
  head of (whole word), after stripping `exclude_exempt_terms`.
- Years rule: description minimum >= 2 years (`6+ years`, `2-5 years of experience`,
  `minimum 3 years`, `at least 2 years of professional experience`) rejects. Degree and
  graduation phrasing (`4-year degree`, `graduating in 2 years`, `N years of coursework`),
  `up to N years`, and minimums below 2 do not.
- Location: CA/US from the config tier lists (tier keys containing `canada`/`local` -> CA, `us` -> US),
  province/state names and codes, country names. `CA` alone counts as Canada only when nothing else
  identifies a country. Opaque (empty, "Multiple Locations", "N Locations") is accepted. Remote is
  accepted unless a foreign country/region is named ("Remote - Germany" is rejected). Anything
  readable that is not CA/US/remote is `outside CA/US`.
- `eligible` = level AND swe AND not excluded AND not years-rejected AND location ok.

## Pipeline behaviour

- `ingest`: fetch (existing adapters + politeness), dedupe, insert new postings. Stale new postings
  (outside `freshness_minutes`) are stored and tagged ineligible ("stale"); only fresh ones are classified.
- `match_users`: per active user, eligible postings with `first_seen_at >= activated_at`, matching prefs,
  with no pending/sent delivery; sorted by favorites, then v1 location tier, then company priority.
- `deliver`: webhook URL = `os.environ[destination.ref]`. Missing env var or `--no-notify` is a dry run:
  alerts are printed and no delivery rows are reserved. Otherwise rows are reserved (`INSERT OR IGNORE`
  pending), sent 10 embeds per message, then marked sent. Cap: 30 alerts per user per cycle (the rest
  go out on later cycles). Discord 404 disables the user. Other errors mark the rows failed
  (retried next cycle) and bump `destinations.fail_count`.

## CLI

- `jobhunt scan [--db jobhunt_v2.db] [--max-priority N] [--no-notify] [--force] [--no-jitter] [--seed]`.
  `--seed` is kept as an alias: ingest and classify without matching or delivering.
- `jobhunt user seed-owner`: creates user #1 (active, `activated_at = now`), levels internship+coop,
  countries CA+US, remote_ok, favorites = `company_priorities` tier 1, destination
  `webhook_env:V2_DISCORD_WEBHOOK_URL`. Idempotent (looks the owner up by that destination).
  `cities` is left empty (no city filter): v1 delivered every CA/US/remote match and only used the
  tiers to ORDER results, so a tier 1-2 city filter would drop Montreal/Ottawa/Waterloo postings.
- `jobhunt user list`.
- `heartbeat` and `list-matches` read the v2 DB. Heartbeat shows postings ingested, eligible, and
  alerts sent in the last 24h (no spend field).

## How to run

```
jobhunt scan --seed --no-jitter            # optional: record today's postings first, no alerts
jobhunt user seed-owner                    # activated_at = now, so only newer postings are sent
V2_DISCORD_WEBHOOK_URL=<test webhook> jobhunt scan --db jobhunt_v2.db
```

Without `--seed` first, undated sources (Workday) would deliver their whole current backlog to the
owner at 30 alerts per cycle. Put `V2_DISCORD_WEBHOOK_URL` in `.env` (separate from the v1
`DISCORD_WEBHOOK_URL`, which is still used by `heartbeat`).

## Replay (`scripts/replay_v1.py`, title only)

`.venv/bin/python scripts/replay_v1.py jobhunt.db` on 57,092 v1 rows:

- (a) v1 `matched=1`: 101 of 101 are v2-eligible (0 rejected).
- (b) 17 LLM-rejected rows: 9 rejected by v2 (5 "Internal"/"Cooperative" SWE titles with no intern term,
  3 product-intern titles, 1 "Internal Product Engineer"), 8 still eligible (the LLM rejected them for
  reasons a title cannot show: veteran-only, Poland/France programs, trading-ops focus, a vague title,
  and 3 titles the owner wants through: Ciena embedded, Sun Life student, Lyft data engineer).
- (c) v1 prefilter title pass 480, v2 eligible 405 (379 in both): 26 newly accepted (ML engineer,
  firmware, embedded, iOS), 101 v1-accepted now rejected (69 PM/program/product marketing, 30 where
  "Internal"/"International" falsely matched "intern", 2 excluded terms).

## Known limitations

- No LLM means no judgement on description content beyond the years rule: veteran-only, citizen-only,
  region-restricted programs, and non-SWE roles with a SWE word in the title (e.g. "Mobile Marketing
  Intern", since `mobile` is a bare term) will pass.
- Title-only replay cannot check location or description; foreign roles in the title text ("- France")
  pass there but would be rejected with a real location.
- Deliveries are at-most-once: a crash between reserving rows and sending leaves them `pending`
  forever (they are never retried automatically).
- Location detection is heuristic; ambiguous codes (IN, DE, CO, AR, OR, ME, HI, OK, NL, PE) are ignored.
- `store.py` (v1 seen-store) was removed in Phase 3; `--seed` replaces its role.
- The v1 live project is unaffected; migrating data from `jobhunt.db` is not implemented.
