"""v2 pipeline stages as plain functions: ingest -> classify_new -> match_users -> deliver.

No LLM anywhere. Every stage takes an open sqlite3 connection (see db.connect)
so tests can drive them with in-memory/temp databases and fake adapters.
"""

from __future__ import annotations

import os
import random
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Mapping

from . import db
from .adapters.base import get_adapter
from .classify import RULES_VERSION, Classification, classify
from .match import matches, sort_key
from .matcher import dedupe_by_uid, is_fresh, location_tier
from .models import AppConfig, Job
from .notifier import DiscordHTTPError, DiscordNotifier, MAX_EMBEDS_PER_MESSAGE, alert_embed
from .security import redact

MAX_ALERTS_PER_USER_PER_CYCLE = 30
STALE_REASON = "stale (older than freshness window at ingest)"


# ---- fetch helpers (moved from cli) ---------------------------------------


def fetch_company(company, errors: list[str]) -> list[Job]:
    if not company.ats:
        return []
    try:
        adapter = get_adapter(company.ats)
        try:
            return adapter.fetch(company)
        finally:
            adapter.close()
    except Exception as exc:  # noqa: BLE001 - collect, never abort the run
        errors.append(f"{company.name} ({company.ats}): {exc}")
        return []


def order_companies(companies: list, politeness) -> list:
    """Order by priority tier; optionally shuffle within each tier per run."""
    ordered = sorted(companies, key=lambda c: c.priority)
    if not (politeness and politeness.enabled and politeness.shuffle_within_tier):
        return ordered
    out: list = []
    i = 0
    while i < len(ordered):
        j = i
        while j < len(ordered) and ordered[j].priority == ordered[i].priority:
            j += 1
        group = ordered[i:j]
        random.shuffle(group)
        out.extend(group)
        i = j
    return out


def pace(politeness) -> None:
    """Random pause between company fetches to spread load (if enabled)."""
    if politeness and politeness.enabled and politeness.company_delay_max > 0:
        time.sleep(random.uniform(politeness.company_delay_min, politeness.company_delay_max))


# ---- stage 1: ingest ------------------------------------------------------


def ingest(
    cfg: AppConfig,
    conn: sqlite3.Connection,
    errors: list[str],
    companies: list,
    *,
    fetch: Callable[[object, list[str]], list[Job]] = fetch_company,
    pace_fn: Callable[[object], None] = pace,
    now: datetime | None = None,
) -> list[Job]:
    """Fetch every company, store postings not seen before, return the new FRESH ones.

    Stale new postings (outside criteria.freshness_minutes) are stored and tagged
    ineligible ("stale") immediately, so they are never delivered; only fresh new
    Jobs are returned for classification.
    """
    now = now or datetime.now(timezone.utc)
    fresh_new: list[Job] = []
    for idx, company in enumerate(companies):
        if idx:
            pace_fn(cfg.politeness)
        jobs = dedupe_by_uid(fetch(company, errors))
        if not jobs:
            continue
        for job in db.insert_new_postings(conn, jobs, seen_at=db.to_iso(now)):
            if is_fresh(job, cfg.criteria.freshness_minutes, now=now):
                fresh_new.append(job)
            else:
                db.upsert_tags(
                    conn, job.uid,
                    Classification(eligible=False, reject_reason=STALE_REASON,
                                   rules_version=RULES_VERSION),
                )
    return fresh_new


# ---- stage 2: classify ----------------------------------------------------


def classify_new(
    cfg: AppConfig, conn: sqlite3.Connection, jobs: list[Job]
) -> list[tuple[Job, Classification]]:
    out = []
    for job in jobs:
        cls = classify(job, cfg.criteria, cfg.location_tiers)
        db.upsert_tags(conn, job.uid, cls)
        out.append((job, cls))
    return out


# ---- stage 3: match -------------------------------------------------------


@dataclass
class Candidate:
    job: Job
    tags: Classification
    company_priority: int
    location_tier: int


def match_users(
    cfg: AppConfig,
    conn: sqlite3.Connection,
    users: list[db.UserRecord] | None = None,
) -> dict[int, list[Candidate]]:
    """Per active user: eligible postings first seen at/after activation that match
    their prefs and have no pending/sent delivery yet, best-first."""
    users = db.list_active_users(conn) if users is None else users
    result: dict[int, list[Candidate]] = {}
    for user in users:
        blocked = db.blocked_uids(conn, user.id)
        cands: list[Candidate] = []
        for row in db.candidate_rows(conn, user.activated_at):
            if row["uid"] in blocked:
                continue
            tags = db.tags_from_row(row)
            job = db.job_from_row(row)
            if not matches(user.prefs, tags, company=job.company):
                continue
            cands.append(
                Candidate(
                    job=job,
                    tags=tags,
                    company_priority=cfg.company_priority.get(job.company, 4),
                    location_tier=location_tier(job.location, cfg.location_tiers),
                )
            )
        cands.sort(
            key=lambda c: sort_key(
                user.prefs, c.job, c.tags, c.company_priority, cfg.location_tiers
            )
        )
        result[user.id] = cands
    return result


# ---- stage 4: deliver -----------------------------------------------------


@dataclass
class DeliverResult:
    sent: int = 0
    failed: int = 0
    dry_run: bool = False
    disabled: bool = False
    skipped_over_cap: int = 0
    notes: list[str] = field(default_factory=list)


def deliver(
    conn: sqlite3.Connection,
    user: db.UserRecord,
    candidates: list[Candidate],
    *,
    env: Mapping[str, str] | None = None,
    notifier_factory: Callable[[str], DiscordNotifier] = lambda url: DiscordNotifier(url),
    dry_run: bool = False,
    cap: int = MAX_ALERTS_PER_USER_PER_CYCLE,
    log: Callable[[str], None] = print,
) -> DeliverResult:
    """Send one user's alerts (<= `cap` per cycle, 10 embeds per Discord message).

    The webhook URL is read from env[destination.ref]. If that variable is
    missing (or dry_run is set) this is a DRY RUN: alerts are printed and NO
    delivery rows are reserved, so they will be sent on a later real run.
    A Discord 404 means the webhook is gone: the user is disabled.
    """
    env = os.environ if env is None else env
    res = DeliverResult()
    if not candidates:
        return res
    batch = candidates[:cap]
    res.skipped_over_cap = max(0, len(candidates) - cap)

    url = env.get(user.destination.ref, "").strip() if user.destination.kind == "webhook_env" else ""
    if dry_run or not url:
        res.dry_run = True
        why = "dry-run" if dry_run else f"{user.destination.ref} not set"
        log(f"[{why}] user {user.id}: {len(batch)} alert(s) not sent")
        for c in batch:
            log(f"  - {c.job.company}: {c.job.title} | {c.job.location or '-'} | {c.job.url}")
        return res

    reserved = [c for c in batch if db.reserve_delivery(conn, user.id, c.job.uid)]
    if not reserved:
        return res
    notifier = notifier_factory(url)

    for i in range(0, len(reserved), MAX_EMBEDS_PER_MESSAGE):
        chunk = reserved[i : i + MAX_EMBEDS_PER_MESSAGE]
        uids = [c.job.uid for c in chunk]
        embeds = [
            alert_embed(c.job, c.company_priority, c.location_tier, c.tags.level, c.tags.categories)
            for c in chunk
        ]
        try:
            notifier.send_embeds(embeds)
        except Exception as exc:  # noqa: BLE001 - classify below, never crash the cycle
            rest = [c.job.uid for c in reserved[i:]]
            db.mark_failed(conn, user.id, rest)
            res.failed += len(rest)
            msg = redact(str(exc))
            res.notes.append(msg)
            if isinstance(exc, DiscordHTTPError) and exc.status_code == 404:
                db.disable_user(conn, user.id, "discord webhook returned 404 (deleted/invalid)")
                res.disabled = True
                log(f"user {user.id}: webhook 404 -> user disabled")
            else:
                db.destination_failed(conn, user.destination.id)
                log(f"user {user.id}: delivery failed: {msg}")
            return res
        db.mark_sent(conn, user.id, uids)
        db.destination_ok(conn, user.destination.id)
        res.sent += len(chunk)
    return res
