"""Concurrent company fetching with per-host politeness, rate-limit adaptation, a circuit
breaker and a scan deadline.

The MAIN thread owns all scheduling state and (in pipeline.ingest) all DB writes; worker
threads only run `fetch(company, errors)`. The main thread dispatches the first pending
company (priority order) whose host has a free slot and is past its pause, so a long run of
same-host companies never starves the other hosts.
"""

from __future__ import annotations

import random
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Callable, Iterator

from .adapters import base
from .models import PolitenessConfig

TOP_SLOWEST = 20  # companies kept in last_run.json

_FIXED_HOSTS = {
    "greenhouse": "boards-api.greenhouse.io",
    "lever": "api.lever.co",
    "ashby": "api.ashbyhq.com",
    "amazon": "www.amazon.jobs",
    "smartrecruiters": "api.smartrecruiters.com",
    "workable": "apply.workable.com",
    "apple": "jobs.apple.com",
    "ibm": "www-api.ibm.com",
    "rippling": "ats.rippling.com",
    "gem": "api.gem.com",
    "jobvite": "jobs.jobvite.com",
    "deel": "jobs.deel.com",
    "digitalrecruiters": "api.digitalrecruiters.com",
    "epic": "careers.epic.com",
    "rentec": "www.rentec.com",
    "balyasny": "bambusdev.my.site.com",
}

# Per-tenant subdomain ATSes: requests (and so STATS) go to <slug>.<domain>, so the
# politeness / 429 / breaker key must be that same real hostname.
_TENANT_DOMAINS = {
    "bamboohr": "bamboohr.com",
    "jazzhr": "applytojob.com",
    "pinpoint": "pinpointhq.com",
    "recruitee": "recruitee.com",
}


def host_key(company) -> str:
    """The ATS API host this company's requests go to (the politeness unit)."""
    ats = company.ats or ""
    if getattr(company, "host", None):
        return company.host
    if ats == "workday" and company.tenant and company.wd:
        return f"{company.tenant}.{company.wd}.myworkdayjobs.com"
    if ats in _TENANT_DOMAINS:
        slug = (company.slug or "").strip()
        return f"{slug.lower()}.{_TENANT_DOMAINS[ats]}" if slug else _TENANT_DOMAINS[ats]
    return _FIXED_HOSTS.get(ats, ats or "unknown")


class FetchError(str):
    """An error message that also carries a short class label (e.g. 'HTTP 429')."""

    error_class: str = "error"


def error_class_of(exc: BaseException) -> str:
    resp = getattr(exc, "response", None)
    status = getattr(resp, "status_code", None)
    return f"HTTP {status}" if status else type(exc).__name__


@dataclass
class Outcome:
    idx: int
    company: object
    host: str
    jobs: list
    seconds: float
    errors: list[str] = field(default_factory=list)  # messages as the fetcher produced them
    error_class: str | None = None


@dataclass
class ScanReport:
    """Everything one scan measured; to_dict() is what lands in logs/last_run.json."""

    total_seconds: float = 0.0
    ingest_seconds: float = 0.0
    scanned: int = 0
    errored: int = 0
    deadline_hit: bool = False
    deferred: list[str] = field(default_factory=list)
    breaker_hosts: list[str] = field(default_factory=list)
    companies: list[dict] = field(default_factory=list)  # name, host, seconds, jobs, error
    hosts: dict[str, dict[str, int]] = field(default_factory=dict)

    def to_dict(self) -> dict:
        slowest = sorted(self.companies, key=lambda c: c["seconds"], reverse=True)[:TOP_SLOWEST]
        return {
            "total_seconds": round(self.total_seconds, 2),
            "ingest_seconds": round(self.ingest_seconds, 2),
            "companies_scanned": self.scanned,
            "companies_errored": self.errored,
            "companies_deferred": len(self.deferred),
            "deadline_hit": self.deadline_hit,
            "deferred": self.deferred,
            "breaker_hosts": self.breaker_hosts,
            "slowest_companies": slowest,
            "hosts": self.hosts,
        }


@dataclass
class _Host:
    cap: int
    mult: float = 1.0
    inflight: int = 0
    next_ok: float = 0.0
    adapted: bool = False
    tripped: bool = False


def fetch_concurrently(
    companies: list,
    pol: PolitenessConfig,
    fetch: Callable[[object, list[str]], list],
    report: ScanReport,
    *,
    deadline_seconds: float | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    uniform: Callable[[float, float], float] = random.uniform,
) -> Iterator[Outcome]:
    """Yield an Outcome per company as fetches finish. Fills report.deferred / breaker_hosts.

    Per host: at most pol.per_host_concurrency in flight (1 after its first 429), and
    successive starts at least uniform(delay_min, delay_max) apart (x2 after a 429).
    A host whose consecutive 429/403 count reaches pol.breaker_threshold is skipped for the
    rest of the scan. After the deadline nothing new starts; in-flight fetches finish.
    """
    stats = base.STATS
    workers = max(1, pol.max_workers)
    base_cap = max(1, pol.per_host_concurrency)
    dmin, dmax = (pol.company_delay_min, pol.company_delay_max) if pol.enabled else (0.0, 0.0)
    deadline_at = None if not deadline_seconds else clock() + deadline_seconds
    hosts: dict[str, _Host] = {}
    pending = list(enumerate(companies))
    running: dict = {}

    def work(company):
        t0 = clock()
        local: list[str] = []
        try:
            jobs = fetch(company, local)
            return jobs, local, clock() - t0, None
        except Exception as exc:  # noqa: BLE001 - never abort the run
            return [], local, clock() - t0, exc

    def defer(items, host=None):
        for _, c in items:
            report.deferred.append(c.name)
            if host:
                stats.add(host, "skipped_by_breaker")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        while pending or running:
            now = clock()
            # adapt on 429, trip breaker
            for h, st in hosts.items():
                if not st.adapted and stats.count(h, "429") > 0:
                    st.adapted, st.cap, st.mult = True, 1, 2.0
                if not st.tripped and stats.consecutive(h) >= pol.breaker_threshold:
                    st.tripped = True
                    report.breaker_hosts.append(h)
            if deadline_at is not None and now >= deadline_at and pending:
                report.deadline_hit = True
                defer(pending)
                pending = []
            if any(st.tripped for st in hosts.values()):
                keep = []
                for item in pending:
                    h = host_key(item[1])
                    if hosts.get(h) and hosts[h].tripped:
                        defer([item], host=h)
                    else:
                        keep.append(item)
                pending = keep

            # dispatch
            soonest = None
            i = 0
            while len(running) < workers and i < len(pending):
                idx, company = pending[i]
                h = host_key(company)
                st = hosts.setdefault(h, _Host(cap=base_cap))
                if st.inflight >= st.cap:
                    i += 1
                elif now < st.next_ok:
                    soonest = st.next_ok if soonest is None else min(soonest, st.next_ok)
                    i += 1
                else:
                    pending.pop(i)
                    st.inflight += 1
                    st.next_ok = now + uniform(dmin, dmax) * st.mult
                    running[pool.submit(work, company)] = (idx, company, h)

            if running:
                timeout = None
                if pending:
                    timeout = 0.05 if soonest is None else max(0.0, min(0.05, soonest - now))
                done, _ = wait(running, timeout=timeout, return_when=FIRST_COMPLETED)
            elif pending:
                sleep(min(0.05, max(0.001, (soonest or now) - now)))
                done = set()
            else:
                break

            for fut in done:
                idx, company, h = running.pop(fut)
                hosts[h].inflight -= 1
                jobs, local, secs, exc = fut.result()
                errs = list(local)
                if exc is not None:
                    errs.append(FetchError(f"{company.name} ({company.ats}): {exc}"))
                    errs[-1].error_class = error_class_of(exc)
                cls = getattr(errs[0], "error_class", "error") if errs else None
                yield Outcome(idx, company, h, jobs, secs, errs, cls)
