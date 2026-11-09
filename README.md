# jobhunt v2

A continuation of [jobhunt v1](https://github.com/avi-kwok/jobhunt), my personal internship scanner.
v2 turns it into a free service: paste a Discord webhook URL on the website and get software
engineering (and adjacent) internship and co-op alerts for Canada and the US in that channel.
No LLM, no accounts, $0 to run.

## Subscribe

1. Create a webhook in a Discord channel you control (Channel settings, Integrations, Webhooks).
2. Paste the URL on the site, pick countries (CA, US) and levels (internship, co-op).
3. The bot posts a private manage link into that channel. Open it and press **Activate**. Only
   someone who can read the channel can do this.

Pause, change preferences, or delete yourself from the same link. The webhook URL is stored
encrypted and never shown again.

## Architecture

- Web: FastAPI on Vercel Hobby (`app.py`, `src/jobhunt/web/`); no cookies, no JavaScript.
- Worker: cron on an Oracle Always Free VM runs `jobhunt scan` every 30 minutes (fetch company ATS boards, classify with deterministic rules, match, deliver).
- Database: one Supabase free Postgres shared by both; SQLite for local dev and tests.
- Secrets: one `JOBHUNT_SECRET_KEY` encrypts webhooks (Fernet) and hashes them (HMAC).
- Details: [docs/architecture.md](docs/architecture.md).

## Local development

```
python3.11 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q                                # SQLite
JOBHUNT_TEST_DATABASE_URL=postgresql://localhost:54329/jobhunt_test .venv/bin/pytest -q   # also Postgres
.venv/bin/jobhunt keygen                           # JOBHUNT_SECRET_KEY
.venv/bin/jobhunt scan --seed --no-jitter          # SQLite jobhunt_v2.db, no alerts
.venv/bin/uvicorn app:app --reload                 # website
.venv/bin/jobhunt doctor                           # env, DB, schema, key checks
```

Copy `.env.example` to `.env` first. Tests never contact Discord or job boards.

## Deploying

Follow [docs/RUNBOOK.md](docs/RUNBOOK.md): Supabase, Vercel, Oracle worker, smoke checks, operations.
