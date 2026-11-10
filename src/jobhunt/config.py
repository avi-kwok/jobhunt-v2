"""Load config.yaml into AppConfig and invert the priority lists.

The human edits `company_priorities` as grouped lists (tier -> [names]); at load
time we invert that into a `dict[str, int]` (name -> tier) for O(1) lookup and
stamp the resolved priority onto every CompanyConfig. If
`companies.resolved.yaml` exists, its endpoint fields are merged by name.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from .budget import BudgetConfig
from .models import AppConfig, CompanyConfig, Criteria, PolitenessConfig, ScheduleConfig

DEFAULT_PRIORITY = 4
CONFIG_PATH = Path("config.yaml")
RESOLVED_PATH = Path("companies.resolved.yaml")


def invert_priorities(company_priorities: dict[int, list[str]]) -> dict[str, int]:
    """Flatten {tier: [names]} into {name: tier}.

    Lower tiers win when a name is (accidentally) listed twice.
    """
    mapping: dict[str, int] = {}
    for tier in sorted(company_priorities):
        for name in company_priorities[tier]:
            # first (lowest) tier seen for a name wins
            mapping.setdefault(name, tier)
    return mapping


def _merge_resolved(
    companies: list[CompanyConfig], resolved_path: Path
) -> None:
    """Merge endpoint fields from companies.resolved.yaml onto companies by name."""
    if not resolved_path.exists():
        return
    data = yaml.safe_load(resolved_path.read_text()) or {}
    resolved_entries = data.get("companies", data if isinstance(data, list) else [])
    by_name = {c.name: c for c in companies}
    for entry in resolved_entries:
        name = entry.get("name")
        target = by_name.get(name)
        if target is None:
            continue
        for field in ("ats", "slug", "tenant", "wd", "site", "host", "workday_facets",
                      "workday_search_terms", "workday_applied_facets", "eightfold_mode"):
            if entry.get(field) is not None:
                setattr(target, field, entry[field])


def _append_resolved_only(
    companies: list[CompanyConfig], resolved_path: Path, priority_map: dict[str, int]
) -> None:
    """Add companies that live only in companies.resolved.yaml (not in config.yaml).

    Lets `companies.resolved.yaml` introduce a company on its own — priority comes
    from company_priorities if listed, else the resolved entry's own `priority`.
    """
    if not resolved_path.exists():
        return
    data = yaml.safe_load(resolved_path.read_text()) or {}
    entries = data.get("companies", data if isinstance(data, list) else [])
    existing = {c.name for c in companies}
    fields = set(CompanyConfig.model_fields)
    for entry in entries:
        name = entry.get("name")
        if not name or name in existing:
            continue
        cc = CompanyConfig(**{k: v for k, v in entry.items() if k in fields})
        cc.priority = priority_map.get(name, entry.get("priority", DEFAULT_PRIORITY))
        companies.append(cc)
        existing.add(name)


def load_config(
    config_path: Path | str = CONFIG_PATH,
    resolved_path: Path | str = RESOLVED_PATH,
) -> AppConfig:
    """Read and validate config.yaml, applying priorities and resolved endpoints."""
    config_path = Path(config_path)
    resolved_path = Path(resolved_path)
    raw = yaml.safe_load(config_path.read_text())

    criteria = Criteria(**raw["criteria"])
    location_tiers: dict[str, list[str]] = raw["location_tiers"]
    company_priorities: dict[int, list[str]] = {
        int(k): v for k, v in raw.get("company_priorities", {}).items()
    }
    priority_map = invert_priorities(company_priorities)

    companies: list[CompanyConfig] = []
    for entry in raw.get("companies", []):
        # entries may be bare {name: ...} or already carry resolved fields
        cc = CompanyConfig(**entry)
        cc.priority = priority_map.get(cc.name, DEFAULT_PRIORITY)
        companies.append(cc)

    _merge_resolved(companies, resolved_path)
    _append_resolved_only(companies, resolved_path, priority_map)

    budget = BudgetConfig(**raw.get("budget", {}))
    politeness = PolitenessConfig(**raw.get("politeness", {}))
    schedule = ScheduleConfig(**raw.get("schedule", {}))

    return AppConfig(
        criteria=criteria,
        location_tiers=location_tiers,
        company_priorities=company_priorities,
        companies=companies,
        company_priority=priority_map,
        budget=budget,
        politeness=politeness,
        schedule=schedule,
    )
