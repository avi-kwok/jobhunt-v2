"""Pydantic data models shared across the scanner and tailor."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class Job(BaseModel):
    """A single posting normalized across every ATS adapter."""

    source: str  # "greenhouse" | "lever" | "ashby" | "workday" | ...
    company: str
    external_id: str
    title: str
    location: str | None = None
    department: str | None = None
    employment_type: str | None = None
    url: str
    description: str | None = None  # plaintext, truncated ~2000 chars
    posted_at: datetime | None = None
    # Human-readable relative posting note for sources that expose no exact
    # timestamp (e.g. Workday's "Posted Today" / "Posted 3 Days Ago").
    posted_note: str | None = None

    @property
    def uid(self) -> str:
        """Stable identity used for dedupe in the dedupe."""
        return f"{self.source}:{self.company}:{self.external_id}"


class CompanyConfig(BaseModel):
    """A watchlist entry, enriched by the loader and `discover`."""

    name: str
    priority: int = 4  # populated at load from company_priorities
    ats: str | None = None  # populated by discover
    slug: str | None = None  # greenhouse/lever/ashby board token
    tenant: str | None = None  # workday
    wd: str | None = None  # workday host, e.g. "wd5"
    site: str | None = None  # workday site (or another ATS's site/board id)
    host: str | None = None  # ATS API host for tenant-hosted ATSes (also the politeness unit)
    # workday: None/True = auto-detect an internship facet and fetch with one
    # facet-filtered search; False = force the searchText phrase-search path
    # (escape hatch for boards whose intern facet misses eligible postings).
    workday_facets: bool | None = None
    # eightfold: pin the search endpoint ("pcsx" | "legacy") instead of probing pcsx first;
    # for tenants whose pcsx endpoint is known to answer 403.
    eightfold_mode: str | None = None


class Criteria(BaseModel):
    """Title/location filtering rules from config.yaml."""

    internship_terms: list[str]  # title must contain >=1
    role_terms: list[str]  # title must contain >=1
    exclude_terms: list[str] = []  # title must contain none
    # Phrases removed from the title before exclude_terms is checked, so e.g.
    # "product manager" doesn't trip the "manager" exclude ("Product Manager
    # Intern" passes; "Senior Product Manager" still fails on "senior").
    exclude_exempt_terms: list[str] = []
    location_match_required: bool = True
    # Only notify on postings whose own timestamp is within this many minutes.
    # None/0 disables the timestamp filter (rely on the dedupe alone).
    # Sources without a reliable timestamp (Workday; Greenhouse gives edit-time)
    # are always included — the dedupe guarantees "new since last scan".
    freshness_minutes: float | None = None


class AppConfig(BaseModel):
    """Fully-resolved application configuration."""

    criteria: Criteria
    location_tiers: dict[str, list[str]]  # ordered; index = priority
    company_priorities: dict[int, list[str]]
    companies: list[CompanyConfig]
    # built by the loader: name -> tier (default 4)
    company_priority: dict[str, int] = {}
    # spend/rate safeguards; overridable via a `budget:` block in config.yaml
    budget: "BudgetConfig | None" = None
    # request pacing / politeness; overridable via a `politeness:` block
    politeness: "PolitenessConfig | None" = None
    # nightly quiet window; overridable via a `schedule:` block
    schedule: "ScheduleConfig | None" = None


class ScheduleConfig(BaseModel):
    """A nightly quiet window during which scheduled scans self-skip.

    Times are in `quiet_tz` (an IANA name, so DST is handled), letting you think
    in one timezone regardless of the machine's local time. Wraps past midnight.
    """

    enabled: bool = True
    quiet_start: str = "21:00"  # HH:MM in quiet_tz
    quiet_end: str = "02:00"
    quiet_tz: str = "America/New_York"
    max_run_seconds: int = 1200  # hard watchdog: force-exit a scan that runs longer (catches sleep-stalled network hangs that block the next scheduled run)


class PolitenessConfig(BaseModel):
    """Request pacing so scans spread load and look less machine-like.

    These reduce load spikes on shared hosts (and any throttling risk). They do
    not disguise the tool — the User-Agent stays honest and descriptive.
    """

    enabled: bool = True
    # random sleep between each company fetch (seconds)
    company_delay_min: float = 0.8
    company_delay_max: float = 3.0
    # random sleep at the start of each scheduled scan (seconds); de-syncs runs
    startup_jitter_seconds: float = 60.0
    # shuffle companies within each priority tier each run (tiers still ordered)
    shuffle_within_tier: bool = True
    # concurrent fetch: total worker threads, and the cap on requests in flight per API host
    # (the company_delay_* pause above is applied PER HOST between requests to that host)
    max_workers: int = 6
    per_host_concurrency: int = 2
    # stop starting new companies after this many seconds; the rest are deferred to the next scan
    scan_deadline_seconds: float = 1500.0
    # consecutive 429/403 answers from one host that trip its breaker (rest of its companies deferred)
    breaker_threshold: int = 3


# Imported here (not at top) to avoid a cycle: budget.py has no model deps.
from .budget import BudgetConfig  # noqa: E402

AppConfig.model_rebuild()
