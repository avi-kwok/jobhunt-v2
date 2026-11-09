"""jobhunt command-line interface."""

from __future__ import annotations

import json
import random
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from . import adapters  # noqa: F401 - registers adapters
from . import db as v2db
from . import pipeline, security
from .budget import BudgetExceeded, BudgetGuard  # tailor + `budget` command only
from .config import load_config
from .matcher import location_tier
from .notifier import LOCATION_LABELS, PRIORITY_LABELS, DiscordNotifier
from .watchdog import start_watchdog

app = typer.Typer(
    help="Personal SWE-internship scanner + resume tailor.", no_args_is_help=True
)
console = Console()

# One resolved .env (cwd-based) shared with security.audit()/preflight().
security.load_env()

LOGS_DIR = Path("logs")
LAST_RUN_PATH = LOGS_DIR / "last_run.json"
CRON_LOG_PATH = LOGS_DIR / "cron.log"
# cron.log is appended on EVERY cron fire (incl. quiet-hour skips), so a stale
# mtime is the reliable "the box/scheduler is dead" signal.
CRON_STALE_MINUTES = 90


def _write_last_run_marker(companies: int, new_postings: int, alerts_sent: int) -> None:
    """Write logs/last_run.json describing the just-completed real scan."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "companies": companies,
        "new_postings": new_postings,
        "alerts_sent": alerts_sent,
    }
    LAST_RUN_PATH.write_text(json.dumps(payload))


# ---- init -----------------------------------------------------------------

_CONFIG_STUB = """\
criteria:
  internship_terms: [intern, internship, "co-op", coop]
  role_terms: [software engineer, software developer, swe, sde, full stack, backend, frontend]
  exclude_terms: [senior, staff, principal, manager, director, new grad, phd]
  location_match_required: true
  freshness_minutes: 60
budget:
  daily_usd: 0.50
  monthly_usd: 5.00
  max_calls_per_run: 60
  min_seconds_between_calls: 0.4
  max_consecutive_errors: 3
location_tiers:
  tier_1_local: [vancouver, toronto]
  tier_2_us_metros: [san francisco, "new york", seattle, boston]
  tier_3_rest_of_canada: [canada, montreal, ottawa, waterloo]
  tier_4_remote: [remote]
company_priorities:
  1: [Stripe]
companies:
  - name: Stripe
"""

_MASTER_STUB = """\
contact:
  name: "Your Name"
  email: "you@example.com"
summary: >
  One-paragraph summary of who you are.
skills:
  languages: [Python]
  frameworks: []
projects: []
experience: []
"""

_ENV_STUB = (
    "ANTHROPIC_API_KEY=\n"
    "DISCORD_WEBHOOK_URL=\n"
    "V2_DISCORD_WEBHOOK_URL=\n"
    "# Optional: contact address sent in the scraper User-Agent.\n"
    "JOBHUNT_CONTACT=\n"
)


def _write_if_missing(path: Path, content: str) -> bool:
    if path.exists():
        console.print(f"[yellow]exists, skipped[/] {path}")
        return False
    path.write_text(content)
    console.print(f"[green]created[/] {path}")
    return True


@app.command()
def init() -> None:
    """Scaffold config.yaml, .env, master_resume.yaml if missing."""
    _write_if_missing(Path("config.yaml"), _CONFIG_STUB)
    _write_if_missing(Path("master_resume.yaml"), _MASTER_STUB)
    _write_if_missing(Path(".env"), _ENV_STUB)
    _write_if_missing(Path(".env.example"), _ENV_STUB)


# ---- discover -------------------------------------------------------------

@app.command()
def discover(
    name: str = typer.Argument(None, help="Company name to resolve."),
    url: str = typer.Option(None, "--url", help="Careers URL to parse directly."),
    all_: bool = typer.Option(False, "--all", help="Resolve every company in config.yaml."),
    concurrency: int = typer.Option(
        8, "--concurrency", help="Companies resolved in parallel (--all)."
    ),
) -> None:
    """Resolve companies to concrete ATS endpoints."""
    from . import discover as disco

    cfg = load_config()

    if all_:
        console.print(
            f"Resolving {len(cfg.companies)} companies "
            f"({concurrency} in parallel)…"
        )

        def _progress(company, hit):
            status = f"[green]{hit.ats}:{hit.slug or hit.tenant}[/]" if hit else "[red]unresolved[/]"
            console.print(f"  P{company.priority} {company.name} -> {status}")

        resolved, unresolved = disco.discover_all(
            cfg.companies, progress=_progress, concurrency=concurrency
        )
        console.print(
            f"\n[green]{len(resolved)} resolved[/] -> companies.resolved.yaml, "
            f"[yellow]{len(unresolved)} unresolved[/] -> unresolved.txt"
        )
        return

    if not name:
        raise typer.BadParameter("provide a NAME, or use --all")

    result = disco.resolve(name, url=url)
    if result is None:
        console.print(f"[red]Could not resolve {name}[/]. Try passing --url <careers URL>.")
        raise typer.Exit(1)
    result.priority = cfg.company_priority.get(name, 4)
    console.print(f"[green]{name}[/] -> {result.model_dump(exclude_none=True)}")
    console.print("Add this to companies.resolved.yaml, or re-run `discover --all`.")


# ---- scan -----------------------------------------------------------------

# Kept as module names for callers/tests; the implementations live in pipeline.py.
_fetch_company = pipeline.fetch_company
_order_companies = pipeline.order_companies
_pace = pipeline.pace


def _in_quiet_hours(schedule, now=None) -> bool:
    """Whether the current time falls in the configured nightly quiet window."""
    if not (schedule and schedule.enabled):
        return False
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(schedule.quiet_tz)
    now = (now or datetime.now(tz)).astimezone(tz)
    cur = now.hour * 60 + now.minute
    sh, sm = map(int, schedule.quiet_start.split(":"))
    eh, em = map(int, schedule.quiet_end.split(":"))
    start, end = sh * 60 + sm, eh * 60 + em
    if start <= end:
        return start <= cur < end
    return cur >= start or cur < end  # window wraps past midnight


@app.command()
def scan(
    max_priority: int = typer.Option(
        None, "--max-priority", help="Only scan companies at tier <= N."
    ),
    no_notify: bool = typer.Option(
        False, "--no-notify", help="Ingest + classify + match, but print instead of sending."
    ),
    seed: bool = typer.Option(
        False,
        "--seed",
        help="Alias mode: ingest + classify postings WITHOUT matching or delivering. "
        "Run it before `user seed-owner` so existing postings predate the owner's "
        "activated_at (users only receive postings first seen after activation).",
    ),
    no_jitter: bool = typer.Option(
        False, "--no-jitter", help="Skip the random startup delay (for interactive runs)."
    ),
    force: bool = typer.Option(
        False, "--force", help="Run even during the configured nightly quiet hours."
    ),
    db_path: str = typer.Option("jobhunt_v2.db", "--db", help="SQLite path (v2 schema)."),
) -> None:
    """v2 pipeline: ingest -> classify -> match users -> deliver. No LLM."""
    cfg = load_config()

    # Nightly quiet window: scheduled runs self-skip (seed/--force bypass it).
    if not seed and not force and _in_quiet_hours(cfg.schedule):
        s = cfg.schedule
        console.print(
            f"[dim]Quiet hours ({s.quiet_start}–{s.quiet_end} {s.quiet_tz}) — "
            "skipping. Use --force to override.[/]"
        )
        return

    companies = _order_companies(cfg.companies, cfg.politeness)
    if max_priority is not None:
        companies = [c for c in companies if c.priority <= max_priority]
    resolved = [c for c in companies if c.ats]

    # Cheap .env protections (perms, not git-tracked). No API key is needed.
    if not seed:
        try:
            for note in security.preflight():
                console.print(f"[dim]security: {note}[/]")
        except security.SecurityError as exc:
            console.print(f"[red]Security check failed:[/] {exc}")
            raise typer.Exit(2)

    console.print(
        f"Scanning {len(resolved)} resolved companies (of {len(companies)} in scope)…"
    )

    # Hard watchdog + startup jitter (unchanged from v1).
    _watchdog = start_watchdog(cfg.schedule.max_run_seconds)
    pol = cfg.politeness
    if pol and pol.enabled and not no_jitter and not seed and pol.startup_jitter_seconds > 0:
        delay = random.uniform(0, pol.startup_jitter_seconds)
        console.print(f"[dim]startup jitter: sleeping {delay:.0f}s[/]")
        time.sleep(delay)

    errors: list[str] = []
    log = lambda msg: console.print(msg, markup=False, highlight=False)  # noqa: E731
    totals = {"sent": 0, "failed": 0, "dry": 0, "disabled": 0}

    conn = v2db.connect(db_path)
    try:
        new_jobs = pipeline.ingest(cfg, conn, errors, resolved)
        classified = pipeline.classify_new(cfg, conn, new_jobs)
        eligible = [j for j, c in classified if c.eligible]
        console.print(
            f"New fresh postings: {len(new_jobs)} · eligible: {len(eligible)}"
        )

        if not seed:
            users = v2db.list_active_users(conn)
            if not users:
                console.print("[yellow]No active users — run `jobhunt user seed-owner`.[/]")
            plan = pipeline.match_users(cfg, conn, users)
            for user in users:
                res = pipeline.deliver(
                    conn, user, plan.get(user.id, []), dry_run=no_notify, log=log
                )
                totals["sent"] += res.sent
                totals["failed"] += res.failed
                totals["dry"] += int(res.dry_run)
                totals["disabled"] += int(res.disabled)
                if res.sent:
                    console.print(f"[green]User {user.id}: sent {res.sent} alert(s).[/]")
                if res.skipped_over_cap:
                    console.print(
                        f"[dim]User {user.id}: {res.skipped_over_cap} more queued "
                        "(per-cycle cap).[/]"
                    )
    finally:
        conn.close()

    # Real fetching/notifying is done — stand the watchdog down.
    _watchdog.cancel()

    if not seed:
        try:
            _write_last_run_marker(len(resolved), len(new_jobs), totals["sent"])
        except Exception:  # noqa: BLE001 - liveness marker is best-effort only
            pass

    if errors:
        console.print(f"\n[yellow]{len(errors)} company error(s):[/]")
        for e in errors:
            console.print(f"  [red]•[/] {security.redact(e)}", markup=True)


# ---- list-matches ---------------------------------------------------------

@app.command("list-matches")
def list_matches(
    since: str = typer.Option(
        None, "--since", help="Only postings first seen since e.g. '7d' or ISO date."
    ),
    max_priority: int = typer.Option(None, "--max-priority"),
    db_path: str = typer.Option("jobhunt_v2.db", "--db"),
) -> None:
    """Print eligible stored postings ordered by company priority then location tier."""
    cfg = load_config()
    since_dt = _parse_since(since)
    conn = v2db.connect(db_path)
    try:
        sql = (
            "SELECT p.*, t.level FROM postings p JOIN posting_tags t ON t.uid = p.uid "
            "WHERE t.eligible = 1"
        )
        params: list = []
        if since_dt is not None:
            sql += " AND p.first_seen_at >= ?"
            params.append(v2db.to_iso(since_dt))
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    items = []
    for r in rows:
        prio = cfg.company_priority.get(r["company"], 4)
        if max_priority is not None and prio > max_priority:
            continue
        items.append((prio, location_tier(r["location_raw"], cfg.location_tiers), r))
    items.sort(key=lambda x: (x[0], x[1]))

    table = Table(title="Eligible postings")
    for col in ("Prio", "Company", "Location", "Tier", "Level", "Title"):
        table.add_column(col)
    for prio, tier, r in items:
        prio_label = PRIORITY_LABELS.get(prio, PRIORITY_LABELS[4])[0]
        table.add_row(
            f"P{prio} {prio_label}",
            r["company"],
            r["location_raw"] or "-",
            LOCATION_LABELS.get(tier, "Other"),
            r["level"] or "-",
            f"[link={r['url']}]{r['title']}[/link]",
        )
    console.print(table)


# ---- user admin -----------------------------------------------------------

user_app = typer.Typer(help="Manage v2 users.", no_args_is_help=True)
app.add_typer(user_app, name="user")

OWNER_WEBHOOK_ENV = "V2_DISCORD_WEBHOOK_URL"


@user_app.command("seed-owner")
def user_seed_owner(
    db_path: str = typer.Option("jobhunt_v2.db", "--db"),
) -> None:
    """Create the owner (user #1) with prefs derived from config.yaml. Idempotent."""
    cfg = load_config()
    conn = v2db.connect(db_path)
    try:
        existing = v2db.find_user_by_destination(conn, "webhook_env", OWNER_WEBHOOK_ENV)
        if existing is not None:
            console.print(f"[dim]Owner already exists (user {existing}); nothing to do.[/]")
            return
        # cities: [] = no city filter. v1 delivered every Canadian/US/remote match
        # (tiers 1-4) and used tiers only for ORDERING; restricting to the tier 1-2
        # city lists would silently drop Montreal/Ottawa/Waterloo. Tiers still
        # rank results via sort_key.
        prefs = v2db.Prefs(
            levels=["internship", "coop"],
            countries=["CA", "US"],
            cities=[],
            remote_ok=True,
            favorite_companies=list(cfg.company_priorities.get(1, [])),
            muted_companies=[],
        )
        uid = v2db.create_user(conn, prefs, "webhook_env", OWNER_WEBHOOK_ENV)
    finally:
        conn.close()
    console.print(
        f"[green]Created owner as user {uid}[/] (activated now; only postings first seen "
        f"after this moment are delivered). Webhook env var: {OWNER_WEBHOOK_ENV}"
    )


@user_app.command("list")
def user_list(db_path: str = typer.Option("jobhunt_v2.db", "--db")) -> None:
    """List users with status, destination ref (never a URL) and prefs summary."""
    conn = v2db.connect(db_path)
    try:
        users = v2db.list_users(conn)
    finally:
        conn.close()
    table = Table(title="Users")
    for col in ("ID", "Status", "Activated", "Destination", "Levels", "Countries", "Cities",
                "Favs", "Sent/Fail"):
        table.add_column(col)
    for u in users:
        status = u.status + (f" ({u.disabled_reason})" if u.disabled_reason else "")
        table.add_row(
            str(u.id), status, u.activated_at[:19],
            f"{u.destination.kind}:{u.destination.ref}",
            ",".join(u.prefs.levels), ",".join(u.prefs.countries),
            ",".join(u.prefs.cities) or "(any)", str(len(u.prefs.favorite_companies)),
            f"ok={u.destination.last_ok_at or '-'} fails={u.destination.fail_count}",
        )
    console.print(table)


# ---- tailor ---------------------------------------------------------------

@app.command()
def tailor(
    url_or_path: str = typer.Argument(..., help="Job URL, file path, or raw JD text."),
    cover_letter: bool = typer.Option(False, "--cover-letter"),
    company: str = typer.Option(None, "--company"),
    role: str = typer.Option(None, "--role"),
) -> None:
    """Produce a tailored resume, gap report, and optional cover letter."""
    from .tailor.tailor import tailor as run_tailor

    cfg = load_config()

    # Same security preflight + spend guard as the scanner.
    try:
        for note in security.preflight():
            console.print(f"[dim]security: {note}[/]")
    except security.SecurityError as exc:
        console.print(f"[red]Security check failed:[/] {exc}")
        raise typer.Exit(2)
    if security.get_api_key(required=False) is None:
        console.print(
            "[red]ANTHROPIC_API_KEY is not set.[/] The tailor needs it — add it to .env."
        )
        raise typer.Exit(2)

    console.print("Tailoring (facts limited to master_resume.yaml)…")
    with BudgetGuard(cfg.budget) as guard:
        try:
            written = run_tailor(
                url_or_path,
                cover_letter=cover_letter,
                company=company,
                role=role,
                guard=guard,
                max_tokens=cfg.budget.sonnet_max_tokens,
            )
        except BudgetExceeded as exc:
            console.print(f"[red]Refused to spend:[/] {exc}")
            raise typer.Exit(2)
        spend = guard.status()

    for label, path in written.items():
        console.print(f"[green]{label}[/] -> {path}")
    console.print(
        f"[dim]spend: ${spend['spent_today']:.4f}/${spend['daily_cap']:.2f} today · "
        f"${spend['spent_month']:.4f}/${spend['monthly_cap']:.2f} this month[/]"
    )


# ---- security-check -------------------------------------------------------

@app.command("security-check")
def security_check() -> None:
    """Audit API-key protections: file permissions, git-ignore, tracking, format."""
    # Harden first, then report.
    for note in security.harden_env_permissions():
        console.print(f"[dim]{note}[/]")
    all_ok, checks = security.audit()

    table = Table(title="API key security audit")
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Detail")
    for label, ok, detail in checks:
        table.add_row(
            label,
            "[green]PASS[/]" if ok else "[red]FAIL[/]",
            security.redact(detail),
        )
    console.print(table)
    if all_ok:
        console.print("[green]All safeguards in place.[/]")
    else:
        console.print(
            "[yellow]Some checks failed — fix before relying on the key.[/] "
            "Common fixes: `cp .env.example .env` and add the key; "
            "ensure `.env` is listed in .gitignore."
        )
        raise typer.Exit(1)


# ---- budget ---------------------------------------------------------------

@app.command()
def budget() -> None:
    """Show configured spend caps and current usage against them."""
    cfg = load_config()
    with BudgetGuard(cfg.budget) as guard:
        s = guard.status()
    c = cfg.budget
    table = Table(title="Spend safeguards")
    table.add_column("Limit")
    table.add_column("Value")
    table.add_row("Daily cap", f"${c.daily_usd:.2f}  (spent today: ${s['spent_today']:.4f})")
    table.add_row("Monthly cap", f"${c.monthly_usd:.2f}  (spent this month: ${s['spent_month']:.4f})")
    table.add_row("Max calls per run", str(c.max_calls_per_run))
    table.add_row("Min seconds between calls", str(c.min_seconds_between_calls))
    table.add_row("Circuit breaker (consecutive errors)", str(c.max_consecutive_errors))
    table.add_row("Haiku max output tokens", str(c.haiku_max_tokens))
    table.add_row("Sonnet max output tokens", str(c.sonnet_max_tokens))
    table.add_row("Guard enabled", str(c.enabled))
    console.print(table)


# ---- heartbeat ------------------------------------------------------------

def _relative_time(then: datetime | None, now: datetime | None = None) -> str:
    """Human-relative age like '12 min ago' / '3 hr ago' / 'just now'."""
    if then is None:
        return "unknown"
    now = now or datetime.now(timezone.utc)
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    secs = (now - then).total_seconds()
    if secs < 0:
        return "just now"
    mins = int(secs // 60)
    if mins < 1:
        return "just now"
    if mins < 60:
        return f"{mins} min ago"
    hours = mins // 60
    if hours < 24:
        return f"{hours} hr ago"
    days = hours // 24
    return f"{days} day ago" if days == 1 else f"{days} days ago"


def _read_last_run(path: Path | None = None) -> dict | None:
    """Read logs/last_run.json, tolerating a missing or corrupt file."""
    path = path if path is not None else LAST_RUN_PATH
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _mtime(path: Path) -> datetime | None:
    """UTC mtime of a file, or None if it's missing/unreadable."""
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def _heartbeat_db_counts(db_path: str) -> dict:
    """Watchlist-independent v2 DB stats: totals + last-24h ingested/eligible/sent."""
    conn = v2db.connect(db_path)
    try:
        return v2db.stats_24h(conn)
    finally:
        conn.close()


@app.command()
def heartbeat(
    no_notify: bool = typer.Option(
        False, "--no-notify", help="Print the summary to the console instead of posting."
    ),
    db_path: str = typer.Option("jobhunt_v2.db", "--db", help="SQLite path (v2 schema)."),
) -> None:
    """Post a periodic liveness/status summary to Discord (silence = broken)."""
    cfg = load_config()
    watchlist = len([c for c in cfg.companies if c.ats])
    counts = _heartbeat_db_counts(db_path)

    last_run = _read_last_run()
    last_scan_ts = None
    if last_run and last_run.get("ts"):
        try:
            last_scan_ts = datetime.fromisoformat(last_run["ts"])
        except ValueError:
            last_scan_ts = None

    cron_mtime = _mtime(CRON_LOG_PATH)
    now = datetime.now(timezone.utc)

    # Health verdict: cron.log fires on EVERY scheduled run, so a fresh mtime is
    # the liveness proof. Missing file -> unknown/unhealthy.
    if cron_mtime is None:
        healthy = False
        status_text = "no cron.log found — scheduler may never have run"
    else:
        age_min = int((now - cron_mtime).total_seconds() // 60)
        healthy = age_min <= CRON_STALE_MINUTES
        status_text = (
            "healthy" if healthy
            else f"no cron activity in {age_min} min — check the box"
        )

    fields = [
        ("Status", status_text),
        ("Last cron fire", _relative_time(cron_mtime, now)),
        ("Last completed scan", _relative_time(last_scan_ts, now)),
        ("Watchlist", str(watchlist)),
        ("Postings ingested (24h)", str(counts["ingested_24h"])),
        ("Eligible (24h)", str(counts["eligible_24h"])),
        ("Alerts sent (24h)", str(counts["sent_24h"])),
    ]

    if healthy:
        title, color = "🟢 jobhunt heartbeat", 0x2ECC71
    else:
        title, color = "🔴 jobhunt — no recent activity", 0xE74C3C

    if no_notify:
        table = Table(title=title)
        table.add_column("Field")
        table.add_column("Value")
        for name, value in fields:
            table.add_row(name, value)
        console.print(table)
        return

    try:
        DiscordNotifier().send_status(title, fields, color)
        console.print(f"[green]Posted heartbeat to Discord[/] ({status_text}).")
    except Exception as exc:  # noqa: BLE001
        console.print(f"[yellow]Heartbeat not posted:[/] {security.redact(str(exc))}")


# ---- helpers --------------------------------------------------------------

def _parse_since(since: str | None) -> datetime | None:
    if not since:
        return None
    if since.endswith("d") and since[:-1].isdigit():
        return datetime.now(timezone.utc) - timedelta(days=int(since[:-1]))
    try:
        return datetime.fromisoformat(since)
    except ValueError:
        return None


if __name__ == "__main__":
    app()
