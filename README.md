# jobhunt

**A personalized internship scanner I built to catch SWE internship and co-op postings the moment they go live.**

With so many internship positions posted every day, and given applying the moment it is posted is a significant advantage, I wanted to optimize my internship hunting process. Unfortunately, all current job boards and scrapers I found either:

1. Required a paid subscription to utilize their app (I am too broke to pay for these. I need an internship first. Sorry LinkedIn Premium!)

OR 

2. Had internships that I was not a good match for, or simply positions I wasn't interested in working in.

So to find internships the moment they're released + filter out all the irrelevant positions specific to me, I decided to make my own "job board".

It's been running unattended on a small cloud box since July 2026.

---

## In practice

Numbers from the live seen-store and spend ledger, **2 July – 9 September 2026 (68 days)**, running unattended on a cloud box scanning every 30 minutes:

| | |
|---|---|
| Companies in scope | **306** (286 watchlist + resolved-only additions) |
| Boards actually scanned each run | **144** |
| Postings ingested | **66,056** |
| Eliminated by free filters before any paid call | **99.77%** |
| Postings that reached the LLM | **153** |
| Accepted as real matches | **137** (16 rejected) |
| Discord alerts sent | **136** — about **2 a day**, 57 in the last 30 days |
| Signal ratio | **1 alert per 485 postings ingested** |
| Total API spend, 68 days | **$0.223** |
| Cost per alert | **$0.0016** (~$0.10/month at this rate) |

The first week ingests in bulk (36,704 postings — everything already open, recorded as seen but deliberately not notified), then settles to roughly 480 new postings a day.

Where the alerts came from: Palantir (27), Manulife (11), RBC (7), TD (6), NVIDIA (6), Capital One (5), BMO (5), Autodesk (5).

**Detection latency** is bounded by the schedule rather than measured: scans fire every 30 minutes, so a posting is picked up within 30 minutes of appearing on the company's endpoint (~15 on average), plus up to 60s of deliberate startup jitter. End-to-end latency from the employer's own publish timestamp isn't instrumented — the seen-store records when *I* first saw a posting, not when it went live.

## How it works

Each scan runs a funnel designed to spend as little money as possible — the expensive LLM call happens last, only for postings that already passed every free filter:

```
watchlist (286 companies)
      │
      ▼
 discover ──▶ resolve each company to its ATS endpoint  ──▶ companies.resolved.yaml (144 boards)
      │
      ▼
  fetch      per-adapter JSON  (Greenhouse · Lever · Ashby · Workday · Amazon + other sites personally given by me)
      │
      ▼
  dedupe     collapse pagination overlap      uid = source:company:external_id
      │
      ▼
 prefilter   title terms, exclusions, location tiers          ← free
      │
      ▼
 freshness   is this posting recent enough to bother?         ← free
      │
      ▼
 LLM match   Claude Haiku scores fit + writes a one-line why  ← the only paid step
      │
      ▼
  record     SQLite seen-store — never notify the same uid twice
      │
      ▼
  notify     ranked Discord embeds
```

**Ranking.** Matches sort by one composite key across the whole batch:

```
(company_priority, location_tier, -score)
```

Company priority first, then location, then match score. Companies are also *fetched* in ascending-priority order, so an interrupted run still covers the best boards first. To make location dominate instead, swap the first two elements in `matcher.composite_key` — a one-line change.

**No duplicates.** Every posting gets a stable `uid = source:company:external_id`, stored in SQLite with `uid` as PRIMARY KEY. `filter_new()` drops any uid already seen and `record()` is `INSERT OR IGNORE`, so a posting is never notified twice. Two consecutive scans give N new, then 0.

**Freshness.** After the title/location prefilter, a posting must also be recent — and this runs *before* the paid LLM call, so stale postings cost nothing. My `config.yaml` uses `freshness_minutes: 360` (6h), comfortably wider than the 30-minute scan cadence so nothing is missed if a run is skipped. Sources with a reliable timestamp (Lever `createdAt`, Ashby `publishedAt`, Amazon `posted_date`) are filtered precisely. **Greenhouse reports edit-time** (a proxy) and **Workday has no absolute timestamp** — those are always let through and rely on the seen-store instead. Set `freshness_minutes: null` to disable and use the seen-store alone.

**Am I still alive?** A daily `jobhunt heartbeat` posts a status embed to the same Discord webhook. If the ping stops arriving, or shows 🔴, the scanner is dead — silence becomes a signal rather than being mistaken for "no new jobs." This is to ensure I don't accidentally forget about **jobhunt** and find I've missed several potential positions

## Tech stack

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.11+ | ecosystem for HTTP + parsing |
| CLI | Typer + Rich | subcommands, and readable tables in a terminal |
| HTTP | httpx | one client, timeouts, retry-with-backoff, connection reuse |
| Models | Pydantic v2 | validates every posting and the whole config at load |
| Storage | SQLite (stdlib) | seen-store + spend ledger; no server to run |
| Config | YAML | the watchlist is hand-edited, so it has to be readable |
| LLM | Anthropic API | Haiku for matching, Sonnet for tailoring |
| Tests | pytest + respx | adapters tested against mocked HTTP, no network |
| Deploy | cron on Ubuntu | plus an optional GitHub Actions workflow |

**Sources.** Five adapters behind one `Adapter` ABC and a registry, so adding an ATS means adding one file: Greenhouse, Lever, and Ashby (single-request JSON boards), Workday (paginated POST search: auto-detects each board's internship facet and fetches intern postings with one facet-filtered query, falling back to role×internship phrase queries at concurrency 2; `workday_facets: false` in `companies.resolved.yaml` forces the fallback), and Amazon's own `amazon.jobs` API. Playwright is an optional extra and a genuine last resort.

## Guardrails

1. **Never scrapes LinkedIn or Indeed.** Only first-party ATS endpoints and company career sites.
2. Prefers ATS JSON endpoints over HTML scraping.
3. **Polite to endpoints:** randomized 0.8–3.0s pause between companies, startup jitter, low concurrency, timeouts, retry-with-backoff, and a descriptive User-Agent.
4. **The tailor never fabricates.** It may only select, reorder, and lightly rephrase facts already in `master_resume.yaml` — enforced in the system prompt (`tailor/tailor.py`). Any `TODO(...)` bullet is ignored.
5. **No secrets in code.** `ANTHROPIC_API_KEY` and `DISCORD_WEBHOOK_URL` come from `.env` (git-ignored). Both are masked by `security.redact()` before anything is printed — a webhook URL is itself a credential, so an HTTP error can't leak it into a log.
6. **No personal data in the repo.** `master_resume.yaml` and the `jobhunt.db` seen-store are git-ignored; the repo ships `master_resume.example.yaml` instead.

## Spend safeguards

Every LLM call passes through a guard that enforces, in order:

1. **Per-run call cap** (`max_calls_per_run`, 60) — no single scan can exceed it.
2. **Daily and monthly USD caps** (`daily_usd` $0.50, `monthly_usd` $5.00) — hard ceilings; the run stops when hit.
3. **Throttle** (`min_seconds_between_calls`, 0.4s) — calls never fire uncontrolled back-to-back.
4. **Circuit breaker** (`max_consecutive_errors`, 3) — repeated API errors stop the run instead of hammering it.
5. **Per-call output caps** (`haiku_max_tokens`, `sonnet_max_tokens`).

Real token cost of every call is written to a local, git-ignored `.jobhunt_usage.db`, so caps persist across runs and reboots. When a cap trips mid-scan the run stops and the remaining postings are picked up next time (caps reset by UTC day/month). Inspect with `jobhunt budget`.

> Belt-and-suspenders: also set a spend limit in the Anthropic Console (Billing → limits). The in-app guard protects against runaway loops and bad keys; the Console limit is the backstop on the account itself.

**Key protection.** The key is read only from `.env` into the process environment — never written elsewhere, never passed on the command line. `.env` is auto-`chmod`ed to 600 on every run, and a preflight refuses to run if `.env` is tracked by git. Audit it with `jobhunt security-check`.

## Setup

Requires Python 3.11+.

```bash
git clone <your-fork-url> jobhunt && cd jobhunt
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"                            # add ",browser" for the Playwright extra

cp .env.example .env                               # fill in ANTHROPIC_API_KEY + DISCORD_WEBHOOK_URL
cp master_resume.example.yaml master_resume.yaml   # replace with your real facts
```

`.env` also takes an optional `JOBHUNT_CONTACT` — an address included in the scraper's User-Agent as a courtesy to the sites you poll. Leave it blank and no contact is sent.

Then resolve your watchlist and take a first pass:

```bash
jobhunt discover --all      # watchlist -> companies.resolved.yaml (+ unresolved.txt)
jobhunt scan --seed         # record what's live today as "seen" — no cost, no notifications
jobhunt scan                # from here on, only genuinely new postings notify
```

`--seed` matters: without it your first real scan treats every currently-open posting as new and floods you.

## Configuration

- **`config.yaml`** — match criteria (title terms + exclusions), location tiers, company priorities, and the watchlist. Priorities are written as grouped lists (`1` = top … `3` = tracking) for readability; the loader inverts them into a `name → tier` dict, and anything unlisted defaults to `4`. This ships with **my** watchlist and my location tiers (Vancouver/Toronto → US metros → rest of Canada → remote) — edit it to your own.
- **`master_resume.yaml`** — an over-complete store of real facts; the tailor selects from it and trims per posting. **Git-ignored**, since it holds your name, email, links and location. Create it from `master_resume.example.yaml`.

## Usage

```bash
jobhunt init                       # scaffold .env / master_resume.yaml if missing
jobhunt discover --all             # resolve the watchlist -> companies.resolved.yaml
jobhunt discover "Stripe"          # resolve one company by name
jobhunt discover "Acme" --url https://acme.wd5.myworkdayjobs.com/en-US/careers
jobhunt scan                       # full pipeline over all resolved companies
jobhunt scan --max-priority 2      # only tiers 1-2 (top + strong)
jobhunt scan --seed                # record current postings as seen, no cost/notify
jobhunt scan --no-notify           # scan without posting to Discord
jobhunt list-matches --since 7d    # print stored matches, best-sorted
jobhunt tailor <URL|path|text> --company Stripe --role "SWE Intern" --cover-letter
jobhunt heartbeat [--no-notify]    # liveness/status embed (or preview to console)
jobhunt security-check             # audit API-key protections
jobhunt budget                     # show spend caps and usage
```

**How discovery works.** Given a URL, the ATS is detected from the host (`greenhouse.io`, `lever.co`, `ashbyhq.com`, `myworkdayjobs.com`) and the slug or Workday tenant is extracted directly. Given only a name, slug candidates are generated and probed against Greenhouse, Lever, and Ashby; the first valid board with jobs wins. Workday usually can't be resolved from a name alone — those land in `unresolved.txt` with a note to supply a careers URL.

## Deployment

I run it on an Oracle Cloud Always Free ARM VM (Ubuntu 22.04) under cron — the laptop was sleeping mid-scan and hanging.

```cron
0,30 * * * * cd ~/jobhunt && flock -n /tmp/jobhunt.lock timeout 1800 .venv/bin/jobhunt scan >> logs/cron.log 2>&1
```

`flock` prevents overlapping runs and `timeout` prevents a hung scan from blocking every later one. **[`deploy/`](deploy/) has the full story** — `setup.sh` is an idempotent Linux bootstrap (installs Python 3.11, venv, editable install, both cron lines), with [`DEPLOY.md`](deploy/DEPLOY.md) for first-time setup and [`MAINTENANCE.md`](deploy/MAINTENANCE.md) for day-to-day operation.

macOS `launchd` works too, and there's an optional GitHub Action at `.github/workflows/scan.yml`. Note the Action deliberately does **not** commit the seen-store back to the repo — that database is your job-search history — so each CI run starts fresh and may re-notify. Local cron keeps the store on your own machine and is the recommended way to run this.

## Tests

```bash
pytest        # 97 tests
```

Adapters and discovery run against mocked HTTP responses via `respx` — no network, no API key required. The LLM stages are pure and defensive enough to test without one.

## Project structure

```
src/jobhunt/
  cli.py         Typer entrypoint — every subcommand
  models.py      Pydantic models: Job, MatchResult, CompanyConfig, AppConfig, ...
  config.py      loads config.yaml + merges companies.resolved.yaml
  discover.py    company name/URL -> concrete ATS endpoint
  matcher.py     prefilter -> freshness -> Haiku match; composite ranking key
  store.py       SQLite seen-store (uid PRIMARY KEY, dedupe)
  notifier.py    Discord embeds + status posts
  budget.py      BudgetGuard: spend/rate caps, .jobhunt_usage.db ledger
  security.py    key protections, redaction, git/permission audit
  watchdog.py    optional hung-run killer (off by default)
  adapters/      greenhouse · lever · ashby · workday · amazon (+ base ABC/registry)
  tailor/        master.py (load résumé) · tailor.py (prompt + anti-fabrication)
tests/           97 tests, mocked HTTP
deploy/          setup.sh · DEPLOY.md · MAINTENANCE.md
```

## Models

- Relevance matching, per new posting: `claude-haiku-4-5-20251001`
- Tailoring and cover letters: `claude-sonnet-4-6`

## Limitations

- **Four companies can't be linked.** Apple's ToS forbids automated access; Microsoft's Eightfold endpoint needs OAuth; Google has had no public API since 2021 and is bot-protected; Meta uses GraphQL with bot detection. Use their native email alerts instead.
- **Workday facets depend on how each employer tags postings.** Boards are fetched via their auto-detected internship facet (seconds per board instead of minutes), but a board that mis-tags some interns (e.g. Manulife's Asia co-ops) needs `workday_facets: false` to use the slower phrase-search path.
- **Known bug:** `load_dotenv()` resolves `.env` by walking up from the installed console script, while `security.audit()` checks the working-directory `.env`. The two can disagree about which file is in use, so `security-check` can report a key as present while saying `.env` wasn't found.

## License

MIT.
