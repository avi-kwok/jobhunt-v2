"""v2 pipeline stages as plain functions: ingest -> classify_new -> match_subscribers -> deliver.

No LLM anywhere. Every stage takes an open db.Conn (SQLite or Postgres, see db.connect)
so tests can drive them with temp databases and fake adapters.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from . import crypto, db, scheduler, webhooks
from .adapters import base
from .adapters.base import get_adapter
from .classify import RULES_VERSION, Classification, classify
from .match import matches, sort_key
from .matcher import dedupe_by_uid, is_fresh, location_tier
from .models import AppConfig, Job, PolitenessConfig
from .notifier import DiscordHTTPError, DiscordNotifier, MAX_EMBEDS_PER_MESSAGE, alert_embed
from .security import redact

MAX_ALERTS_PER_SUBSCRIBER_PER_CYCLE = 30
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
        err = scheduler.FetchError(f"{company.name} ({company.ats}): {exc}")
        err.error_class = scheduler.error_class_of(exc)
        errors.append(err)
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


def deferred_first(companies: list, deferred: list[str]) -> list:
    """Companies the previous scan did not get to (deferred) go first; order is otherwise kept."""
    names = set(deferred)
    return [c for c in companies if c.name in names] + [c for c in companies if c.name not in names]


# ---- stage 1: ingest ------------------------------------------------------


def ingest(
    cfg: AppConfig,
    conn: db.Conn,
    errors: list[str],
    companies: list,
    *,
    fetch: Callable[[object, list[str]], list[Job]] = fetch_company,
    now: datetime | None = None,
    report: scheduler.ScanReport | None = None,
    deadline_seconds: float | None = None,
) -> list[Job]:
    """Fetch every company concurrently, store postings not seen before, return the new FRESH ones.

    Fetching is scheduled per API host (see scheduler.py); this (main) thread does every DB
    write, one commit batch per company as it finishes. Stale new postings (outside
    criteria.freshness_minutes) are stored and tagged ineligible ("stale") immediately, so
    they are never delivered; only fresh new Jobs are returned for classification (in
    `companies` order, whatever order the fetches finished in).
    `deadline_seconds` defaults to politeness.scan_deadline_seconds. `report`, if given, is
    filled with timings, host stats and the deferred company names.
    """
    now = now or datetime.now(timezone.utc)
    pol = cfg.politeness or PolitenessConfig(enabled=False)
    report = report if report is not None else scheduler.ScanReport()
    if deadline_seconds is None:
        deadline_seconds = pol.scan_deadline_seconds
    stats = base.begin_scan()
    t0 = time.monotonic()
    fresh_by_idx: dict[int, list[Job]] = {}
    for out in scheduler.fetch_concurrently(
        companies, pol, fetch, report, deadline_seconds=deadline_seconds
    ):
        errors.extend(out.errors)
        report.scanned += 1
        report.errored += bool(out.errors)
        jobs = dedupe_by_uid(out.jobs)
        report.companies.append({
            "name": out.company.name, "host": out.host, "seconds": round(out.seconds, 2),
            "jobs": len(jobs), "error": out.error_class,
        })
        if not jobs:
            continue
        db.touch_postings(conn, [j.uid for j in jobs], now=now)
        stale: list[tuple[str, Classification]] = []
        fresh: list[Job] = []
        for job in db.insert_new_postings(conn, jobs, seen_at=db.to_iso(now)):
            if is_fresh(job, cfg.criteria.freshness_minutes, now=now):
                fresh.append(job)
            else:
                stale.append((
                    job.uid,
                    Classification(eligible=False, reject_reason=STALE_REASON,
                                   rules_version=RULES_VERSION),
                ))
        db.upsert_tags_many(conn, stale)
        fresh_by_idx[out.idx] = fresh
    report.ingest_seconds = time.monotonic() - t0
    report.hosts = stats.snapshot()
    for h in report.breaker_hosts:
        skipped = report.hosts.get(h, {}).get("skipped_by_breaker", 0)
        errors.append(
            f"RATE LIMIT: circuit breaker tripped for host {h} (consecutive 429/403): "
            f"{skipped} company(ies) skipped, deferred to the next scan"
        )
    if report.deadline_hit:
        errors.append(
            f"SCAN DEADLINE ({deadline_seconds:.0f}s) reached: {len(report.deferred)} "
            "company(ies) not scanned, deferred to the next scan"
        )
    return [j for idx in sorted(fresh_by_idx) for j in fresh_by_idx[idx]]


# ---- stage 2: classify ----------------------------------------------------


def classify_new(
    cfg: AppConfig, conn: db.Conn, jobs: list[Job]
) -> list[tuple[Job, Classification]]:
    out = [(job, classify(job, cfg.criteria, cfg.location_tiers)) for job in jobs]
    db.upsert_tags_many(conn, [(job.uid, cls) for job, cls in out])
    return out


UNTAGGED_PER_SCAN = 500


def classify_untagged(
    cfg: AppConfig, conn: db.Conn, *, limit: int = UNTAGGED_PER_SCAN, now: datetime | None = None
) -> int:
    """Tag postings that have no tags yet (pushed via POST /ingest/posting). Same freshness
    gate as ingest: stale -> STALE_REASON, fresh -> classify. Returns how many were tagged."""
    now = now or datetime.now(timezone.utc)
    items: list[tuple[str, Classification]] = []
    for job in db.untagged_postings(conn, limit):
        if is_fresh(job, cfg.criteria.freshness_minutes, now=now):
            items.append((job.uid, classify(job, cfg.criteria, cfg.location_tiers)))
        else:
            items.append((job.uid, Classification(
                eligible=False, reject_reason=STALE_REASON, rules_version=RULES_VERSION)))
    db.upsert_tags_many(conn, items)
    return len(items)


# ---- stage 3: match -------------------------------------------------------


@dataclass
class Candidate:
    job: Job
    tags: Classification
    company_priority: int
    location_tier: int


def match_subscribers(
    cfg: AppConfig,
    conn: db.Conn,
    subs: list[db.Subscriber] | None = None,
) -> dict[int, list[Candidate]]:
    """Per active subscriber: eligible postings first seen at/after activation that match
    their countries/levels and are not already sent or in flight, best-first."""
    subs = db.list_active_subscribers(conn) if subs is None else subs
    result: dict[int, list[Candidate]] = {}
    for sub in subs:
        blocked = db.blocked_uids(conn, sub.id)
        cands: list[Candidate] = []
        for row in db.candidate_rows(conn, sub.activated_at or sub.created_at):
            if row["uid"] in blocked:
                continue
            tags = db.tags_from_row(row)
            if not matches(sub, tags):
                continue
            job = db.job_from_row(row)
            cands.append(
                Candidate(
                    job=job,
                    tags=tags,
                    company_priority=cfg.company_priority.get(job.company, 4),
                    location_tier=location_tier(job.location, cfg.location_tiers),
                )
            )
        cands.sort(key=lambda c: sort_key(c.location_tier, c.company_priority))
        result[sub.id] = cands
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
    conn: db.Conn,
    sub: db.Subscriber,
    candidates: list[Candidate],
    *,
    key: str | None = None,
    notifier_factory: Callable[[str], DiscordNotifier] = lambda url: DiscordNotifier(url),
    dry_run: bool = False,
    cap: int = MAX_ALERTS_PER_SUBSCRIBER_PER_CYCLE,
    log: Callable[[str], None] = print,
) -> DeliverResult:
    """Send one subscriber's alerts (<= `cap` per cycle, 10 embeds per Discord message).

    The stored URL is decrypted, re-parsed and the request URL rebuilt from (id, token).
    dry_run prints alerts and reserves nothing. Discord 404/401 disables the subscriber
    at once; any other failure counts one failed cycle (5 in a row disables).
    A missing/wrong JOBHUNT_SECRET_KEY raises crypto.CryptoError (fail fast).
    """
    res = DeliverResult()
    if not candidates:
        return res
    batch = candidates[:cap]
    res.skipped_over_cap = max(0, len(candidates) - cap)

    if dry_run:
        res.dry_run = True
        log(f"[dry-run] subscriber {sub.id}: {len(batch)} alert(s) not sent")
        for c in batch:
            log(f"  - {c.job.company}: {c.job.title} | {c.job.location or '-'} | {c.job.url}")
        return res

    hook = webhooks.parse(crypto.decrypt_url(sub.webhook_enc, key))
    reserved = [c for c in batch if db.reserve_delivery(conn, sub.id, c.job.uid)]
    if not reserved:
        return res
    notifier = notifier_factory(hook.url)

    for i in range(0, len(reserved), MAX_EMBEDS_PER_MESSAGE):
        chunk = reserved[i : i + MAX_EMBEDS_PER_MESSAGE]
        embeds = [
            alert_embed(c.job, c.company_priority, c.location_tier, c.tags.level, c.tags.categories)
            for c in chunk
        ]
        try:
            notifier.send_embeds(embeds)
        except Exception as exc:  # noqa: BLE001 - classify below, never crash the cycle
            rest = [c.job.uid for c in reserved[i:]]
            db.mark_failed(conn, sub.id, rest)
            res.failed += len(rest)
            msg = redact(str(exc))
            res.notes.append(msg)
            if isinstance(exc, DiscordHTTPError) and exc.status_code in (401, 404):
                db.disable_subscriber(
                    conn, sub.id, f"discord webhook returned {exc.status_code} (deleted/invalid)"
                )
                res.disabled = True
                log(f"subscriber {sub.id}: webhook {exc.status_code} -> disabled")
            else:
                res.disabled = db.subscriber_failed(conn, sub.id)
                log(f"subscriber {sub.id}: delivery failed: {msg}")
                if res.disabled:
                    log(f"subscriber {sub.id}: too many failed cycles -> disabled")
            return res
        db.mark_sent(conn, sub.id, [c.job.uid for c in chunk])
        db.subscriber_ok(conn, sub.id)
        res.sent += len(chunk)
    return res
