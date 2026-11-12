"""Deterministic posting classifier (v2). No network unless a tie-breaker is passed.

`classify(job, criteria, location_tiers)` turns a Job into a `Classification`
that mirrors the `posting_tags` table. Everything is title/location/description
regex work with WHOLE-WORD matching, so "Internal", "International" and
"Cooperative" never count as intern / co-op.

Optional `tiebreak` (llm_tiebreak.TieBreaker): consulted ONLY when the sole role match
is an ambiguous term and every other gate passes; its verdict replaces the rules'
verdict and is marked with an "llm" category (plus an "llm: " reject_reason prefix).

Bump RULES_VERSION whenever the rules change so stored tags can be re-run.
"""

from __future__ import annotations

import re
from pathlib import Path
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Callable

from .llm_tiebreak import LLM_TAG, REJECT_PREFIX, Verdict
from .matcher import _is_remote, _is_unresolved_location
from .models import Criteria, Job

RULES_VERSION = 3
REJECT_MAX = 160

# ---- term lists -----------------------------------------------------------

# Added on top of config role_terms (config terms minus the PM group).
EXTRA_SWE_TERMS = [
    "mobile developer", "mobile engineer", "mobile software", "mobile app",
    "ios", "android", "embedded", "firmware",
    "machine learning engineer", "ml engineer", "data engineer", "security engineer",
    "front end engineer", "frontend engineer",
]

# Role terms that are also hardware / industrial discipline names ("Systems
# Engineering Co-Op" at a defense prime, "Production Engineer (Starship)",
# "Front-End Materials Procurement"). One counts as SWE only when the title has no
# hardware-discipline veto word (_HW_VETO_RE) AND either carries a software anchor
# (_ANCHOR_RE) or the company is not in criteria.hardware_heavy_companies.
AMBIGUOUS_ROLE_TERMS = frozenset({
    "systems engineer", "integration engineer", "automation engineer", "build engineer",
    "production engineer", "platform engineer", "infrastructure engineer",
    "front end", "front-end", "frontend", "mobile app",
})
_ANCHOR_RE = re.compile(
    r"\b(?:software|firmware|devops|cloud|kubernetes|web|developers?|data|ml|ai|(?-i:IT)|"
    r"network|cyber|test\s+automation|sre|computer\s+science|applications?)\b",
    re.IGNORECASE,
)
# Hardware / physical-discipline words that veto an ambiguous role term at ANY
# company ("Radar Systems Engineer", "Industrial Automation Engineer", "Display
# Systems Engineering Co-Op"). "vehicle" / "controls" / "data center" don't veto
# when the title also says "software" ("Vehicle Software Integration Engineer");
# "Data Center Infrastructure Engineer Intern" is facilities work.
_HW_VETO_RE = re.compile(
    r"\b(?:rf|radar|acoustics?|mechanical|flight\s+controls?|guidance|avionics|hydro|"
    r"aircraft|civil|composites?|nuclear|grid|scada|manufacturing|assembly|propulsion|"
    r"structures?|electrical|hardware|industrial|facilit(?:y|ies)|process|"
    r"display\s+systems?|specialty|mbse|model[\s\-]+based\s+systems)\b",
    re.IGNORECASE,
)
_HW_VETO_UNLESS_SOFTWARE_RE = re.compile(
    r"\b(?:vehicles?|controls?|data[\s\-]*cent(?:er|re)s?)\b", re.IGNORECASE
)
_SOFTWARE_RE = re.compile(r"\bsoftware\b", re.IGNORECASE)
# Phrases blanked before role matching: non-software "developer" titles ("Model
# Developer" in bank model-risk, "Business Developer"), embedded HARDWARE and
# machine-tool programming ("CNC Programmer Co-op", "PLC Programmer").
_ROLE_VETO_RE = re.compile(
    r"\b(?:(?:business|model|curriculum|content|product|publisher|real\s+estate)[\s\-]+developers?"
    r"|embedded[\s\-]+hardware|(?:cnc|plc)[\s\-]+programm(?:er|ers|ing))\b",
    re.IGNORECASE,
)
# French "stage" (internship) only in intern-shaped positions, so "Stage 0
# Propellant", "Stage Systems" and "Late Stage Clinical" never count.
_STAGE_RE = re.compile(r"^\s*stage\b|\bstage\s*[-:–(]|\ben\s+stage\b|\bstage\s+de\b", re.IGNORECASE)

# A config role term containing one of these WORDS belongs to the product /
# program-management group, which is out of launch scope. ("production engineer"
# stays: word match, not substring.)
_PM_WORDS = {"product", "program", "programme", "tpm", "apm", "pm"}

# Senior-level title words, always excluded in addition to config exclude_terms.
SENIOR_TERMS = ["senior", "sr", "staff", "principal", "lead", "manager", "director", "head of"]

COOP_TERMS = ["co-op", "coop", "co op"]
# Used when criteria.internship_terms is empty.
_DEFAULT_INTERN_TERMS = ["intern", "internship", "student", "campus"]

# ---- regex builders -------------------------------------------------------


def _term_re(term: str, suffix: str = "") -> str:
    """Whole-word pattern for a term; spaces/hyphens inside it are interchangeable."""
    parts = [re.escape(p) for p in re.split(r"[\s\-]+", term.strip().lower()) if p]
    body = r"[\s\-]*".join(parts)
    return rf"\b{body}{suffix}\b"


def _any_re(terms: list[str], suffix: str = "") -> re.Pattern[str] | None:
    pats = [_term_re(t, suffix) for t in terms if t and t.strip()]
    return re.compile("|".join(pats), re.IGNORECASE) if pats else None


def _is_pm_term(term: str) -> bool:
    return bool(_PM_WORDS & set(re.split(r"[\s\-]+", term.lower())))


@dataclass(frozen=True)
class _Rules:
    coop: re.Pattern
    intern: re.Pattern | None
    role: re.Pattern | None
    amb_role: re.Pattern | None
    exclude: re.Pattern | None
    exempt: re.Pattern | None
    ca_terms: tuple[str, ...]
    us_terms: tuple[str, ...]
    ca_re: re.Pattern | None
    us_re: re.Pattern | None
    hw_companies: frozenset[str]


def _tier_country(key: str) -> str | None:
    k = key.lower()
    if "canada" in k or "local" in k:
        return "CA"
    if "us" in k.split("_") or "us_" in k or "_us" in k:
        return "US"
    return None


@lru_cache(maxsize=8)
def _build_rules(
    intern_terms: tuple[str, ...],
    role_terms: tuple[str, ...],
    exclude_terms: tuple[str, ...],
    exempt_terms: tuple[str, ...],
    tiers: tuple[tuple[str, tuple[str, ...]], ...],
    hardware_heavy: tuple[str, ...] = (),
) -> _Rules:
    coop = _any_re(COOP_TERMS, suffix="s?")
    assert coop is not None
    non_coop = [t for t in (intern_terms or tuple(_DEFAULT_INTERN_TERMS)) if not coop.search(t)]
    roles = [t for t in role_terms if not _is_pm_term(t)] + EXTRA_SWE_TERMS
    amb = [t for t in roles if t.strip().lower() in AMBIGUOUS_ROLE_TERMS]
    strong = [t for t in roles if t.strip().lower() not in AMBIGUOUS_ROLE_TERMS]
    excl = [t.strip() for t in exclude_terms if t.strip()] + SENIOR_TERMS
    ca: list[str] = []
    us: list[str] = []
    for key, terms in tiers:
        country = _tier_country(key)
        bucket = ca if country == "CA" else us if country == "US" else None
        if bucket is not None:
            bucket.extend(t.lower() for t in terms)
    return _Rules(
        coop=coop,
        intern=_any_re(non_coop, suffix="s?"),
        role=_any_re(strong, suffix="(?:s|ing)?"),
        amb_role=_any_re(amb, suffix="(?:s|ing)?"),
        exclude=_any_re(excl, suffix="s?"),
        exempt=_any_re(list(exempt_terms)),
        ca_terms=tuple(ca),
        us_terms=tuple(us),
        ca_re=_any_re(ca),
        us_re=_any_re(us),
        hw_companies=frozenset(c.strip().lower() for c in hardware_heavy if c.strip()),
    )


def _rules(criteria: Criteria, location_tiers: dict[str, list[str]]) -> _Rules:
    return _build_rules(
        tuple(criteria.internship_terms),
        tuple(criteria.role_terms),
        tuple(criteria.exclude_terms),
        tuple(criteria.exclude_exempt_terms),
        tuple((k, tuple(v)) for k, v in location_tiers.items()),
        tuple(criteria.hardware_heavy_companies),
    )


# ---- years of experience --------------------------------------------------

_YEARS_RE = re.compile(
    r"(?<![\d.])(\d{1,2})\s*(?:(?:-|–|—|to)\s*(\d{1,2}))?\s*(\+)?\s*(?:years?|yrs?)\b",
    re.IGNORECASE,
)
_NEG_REST_RE = re.compile(
    r"\s*(?:of\s+|in\s+|at\s+)?(?:degree|coursework|course\s+work|post-?secondary|university|"
    r"college|school|undergrad|academic|stud(?:y|ies|ying)|old\b|of\s+age|remaining|left\b|"
    r"towards?\b|programme?\b|apart\b|ago\b)",
    re.IGNORECASE,
)
_NEG_PREFIX_RE = re.compile(
    r"(?:graduat\w*|complet\w*|finish\w*|within|remaining|in\s+the\s+next|enrolled|pursuing|"
    r"studying|currently\s+in|expected|up\s+to|less\s+than|fewer\s+than|under|no\s+more\s+than|"
    r"at\s+most|max(?:imum)?(?:\s+of)?|over\s+the\s+(?:past|last)|in\s+(?:your|the))\s*$",
    re.IGNORECASE,
)
_EXP_REST_RE = re.compile(
    r"experience|professional|work(?:ing)?\b|industry|hands-on|employment|relevant",
    re.IGNORECASE,
)
_MIN_PREFIX_RE = re.compile(
    r"(?:minimum(?:\s+of)?|min\.?|at\s+least|requires?|required|must\s+have|need)\s*$",
    re.IGNORECASE,
)


def years_required(description: str | None) -> int | None:
    """Largest minimum-years-of-experience stated in `description` that is >= 2, else None.

    Matches "6+ years", "2-5 years of experience", "minimum 3 years",
    "at least 2 years of professional experience". Ignores degree/graduation
    phrasing ("4-year degree", "graduating in 2 years", "1+ years of coursework")
    and anything with a minimum below 2 ("0-1 years").
    """
    if not description:
        return None
    best: int | None = None
    for m in _YEARS_RE.finditer(description):
        lo = int(m.group(1))
        if lo < 2:
            continue
        prefix = description[max(0, m.start() - 40) : m.start()]
        rest = description[m.end() : m.end() + 80]
        if _NEG_REST_RE.match(rest) or _NEG_PREFIX_RE.search(prefix):
            continue
        explicit = bool(m.group(2) or m.group(3))  # "N+" or "N-M" forms
        if explicit or _EXP_REST_RE.search(rest) or _MIN_PREFIX_RE.search(prefix):
            best = max(best or 0, lo)
    return best


# ---- location -------------------------------------------------------------

_CA_NAMES = [
    "alberta", "british columbia", "manitoba", "new brunswick", "newfoundland", "labrador",
    "nova scotia", "ontario", "prince edward island", "quebec", "québec", "saskatchewan",
    "yukon", "northwest territories", "nunavut", "canada",
]
# NL (Netherlands) and PE (Peru) collide with country codes -> omitted.
_CA_CODES = {"AB", "BC", "MB", "NB", "NS", "ON", "QC", "SK", "YT", "NT", "NU"}

_US_NAMES = [
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut",
    "delaware", "florida", "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas",
    "kentucky", "louisiana", "maine", "maryland", "massachusetts", "michigan", "minnesota",
    "mississippi", "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey",
    "new mexico", "new york", "north carolina", "north dakota", "ohio", "oklahoma", "oregon",
    "pennsylvania", "rhode island", "south carolina", "south dakota", "tennessee", "texas",
    "utah", "vermont", "virginia", "washington", "west virginia", "wisconsin", "wyoming",
    "district of columbia", "united states of america", "united states", "usa", "puerto rico",
]
# IN/DE/CO/AR/OR/ME/HI/OK are common words or other countries' codes -> omitted.
_US_CODES = {
    "AL", "AK", "AZ", "CT", "DC", "FL", "GA", "IL", "IA", "KS", "KY", "LA", "MD", "MA", "MI",
    "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "PA", "RI",
    "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "US",
}
_BOTH_NAMES = ["north america"]
_FOREIGN_NAMES = [
    "united kingdom", "england", "scotland", "india", "germany", "france", "china", "japan",
    "singapore", "ireland", "israel", "australia", "brazil", "mexico", "netherlands",
    "poland", "spain", "italy", "sweden", "switzerland", "norway", "denmark", "finland",
    "emea", "apac", "latam", "europe", "asia", "korea", "taiwan", "hong kong", "philippines",
    "vietnam", "pakistan", "argentina", "colombia", "chile", "portugal", "romania", "ukraine",
    "turkey", "egypt", "nigeria", "kenya", "south africa", "new zealand", "dubai", "uae",
]
_CA_NAME_RE = _any_re(_CA_NAMES)
_US_NAME_RE = re.compile(
    "|".join(_term_re(n) for n in _US_NAMES), re.IGNORECASE
)
_BOTH_RE = _any_re(_BOTH_NAMES)
_FOREIGN_RE = _any_re(_FOREIGN_NAMES)
_UK_TOKEN_RE = re.compile(r"\bUK\b")
_CODE_RE = re.compile(r"\b[A-Z]{2}\b")
# A "City, ST[ ZIP]" segment (whole location or one ';'-separated part) ending in one
# of the omitted codes above is a US state ("Littleton, CO", "Longmont, CO 80501",
# "Hillsboro,OR"). Multi-comma forms ("Bogota, DC, CO") don't qualify.
_US_STATE_SEG_RE = re.compile(
    r"(?:^|;)\s*[^,;]+,\s*(?:CO|IN|DE|OR|OK|ME|HI|AR)(?:\s+\d{5}(?:-\d{4})?)?\s*(?=;|$)"
)
# Terms in the tier lists that are countries/provinces/regions, not cities.
_NOT_CITIES = {"canada", "ontario", "british columbia", "quebec", "remote"}


# City gazetteer (GeoNames cities1000, CC BY 4.0; built by scripts/build_cities.py).
# Loaded on first use, never at import.
_CITIES_PATH = Path(__file__).parent / "data" / "cities_us_ca.tsv"
_CITY_SEG_RE = re.compile(
    r"^\s*(?P<city>[^,]+?)\s*,\s*(?P<st>[A-Z]{2})"
    r"(?:\s+(?:\d{5}(?:-\d{4})?|[A-Za-z]\d[A-Za-z]\s?\d[A-Za-z]\d))?"
    r"(?:\s*,\s*(?i:usa|us|united\s+states(?:\s+of\s+america)?|canada))?\s*$"
)


def _norm_city(name: str) -> str:
    s = re.sub(r"[.'’`]", "", name.lower())
    s = re.sub(r"[\s\-]+", " ", s).strip()
    return re.sub(r"^st ", "saint ", s)


@lru_cache(maxsize=1)
def _city_index() -> dict[str, frozenset[tuple[str, str]]]:
    idx: dict[str, set[tuple[str, str]]] = {}
    try:
        text = _CITIES_PATH.read_text(encoding="utf-8")
    except OSError:
        return {}
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            idx.setdefault(parts[0], set()).add((parts[1], parts[2]))
    return {k: frozenset(v) for k, v in idx.items()}


def _city_country(city: str, st: str) -> str | None:
    """'US' / 'CA' for a "City, ST" segment per the gazetteer, else None (unknown/ambiguous)."""
    entries = _city_index().get(_norm_city(city))
    if not entries:
        return None
    if st == "CA":  # California or Canada
        in_ca_state = ("US", "CA") in entries
        in_canada = any(c == "CA" for c, _ in entries)
        if in_ca_state and not in_canada:
            return "US"
        if in_canada and not in_ca_state:
            return "CA"
        return None
    us, ca = ("US", st) in entries, ("CA", st) in entries
    return "US" if us and not ca else "CA" if ca and not us else None


@dataclass
class _Loc:
    countries: list[str]
    cities: list[str]
    foreign: bool


def _detect_location(location: str, rules: _Rules) -> _Loc:
    low = location.lower()
    ca = us = False
    cities: list[str] = []

    for terms, pat, is_ca in ((rules.ca_terms, rules.ca_re, True), (rules.us_terms, rules.us_re, False)):
        if pat is None:
            continue
        for term in terms:
            if re.search(_term_re(term), low):
                if is_ca:
                    ca = True
                else:
                    us = True
                if term not in _NOT_CITIES and term not in cities:
                    cities.append(term)

    if _CA_NAME_RE and _CA_NAME_RE.search(low):
        ca = True
    if _US_NAME_RE.search(low):
        us = True
    if _BOTH_RE and _BOTH_RE.search(low):
        ca = us = True

    if re.search(r"\bU\.S\.", location):
        us = True
    # 2-letter codes only from segments with no foreign name: in "United Kingdom,
    # Edinburgh, SC" the SC is Scotland, not South Carolina.
    codes = {
        code
        for seg in re.split(r"[;|]", location)
        if not ((_FOREIGN_RE and _FOREIGN_RE.search(seg.lower())) or _UK_TOKEN_RE.search(seg))
        for code in _CODE_RE.findall(seg)
    }
    if codes & _CA_CODES:
        ca = True
    if codes & _US_CODES:
        us = True
    # Gazetteer: "City, ST" segments (country-less or "City, CA"). Only adds a country.
    for seg in re.split(r"[;|]", location):
        if (_FOREIGN_RE and _FOREIGN_RE.search(seg.lower())) or _UK_TOKEN_RE.search(seg):
            continue
        m = _CITY_SEG_RE.match(seg)
        if not m:
            continue
        found = _city_country(m.group("city"), m.group("st"))
        if found == "US":
            us = True
        elif found == "CA":
            ca = True
    # "CA" is both Canada's and California's code: only a tie-breaker when nothing
    # else identified the country ("Remote - CA" -> Canada; "San Jose, CA" -> US via city).
    if "CA" in codes and not (ca or us):
        ca = True

    foreign = bool((_FOREIGN_RE and _FOREIGN_RE.search(low)) or _UK_TOKEN_RE.search(location))
    if not foreign and _US_STATE_SEG_RE.search(location):
        us = True
    countries = [c for c, on in (("CA", ca), ("US", us)) if on]
    return _Loc(countries, cities, foreign)


# ---- term tags ("Summer 2027") ----------------------------------------------

_SEASON = r"(?:winter|spring|summer|fall|autumn)"
_SEASONS_RE = rf"{_SEASON}(?:\s*(?:/|&|,|-|\band\b|\bor\b)\s*{_SEASON})*"
_TERM_A_RE = re.compile(rf"\b({_SEASONS_RE})\s*,?\s*(?:term\s+)?(20\d\d)\b", re.IGNORECASE)
_TERM_B_RE = re.compile(rf"\b(20\d\d)\s+({_SEASONS_RE})\b", re.IGNORECASE)
_SEASON_FIND_RE = re.compile(_SEASON, re.IGNORECASE)


def parse_terms(text: str | None) -> list[str]:
    """Canonical "Season YYYY" tags found in `text`, in order, deduped ("Autumn" -> Fall;
    "Winter/Spring 2027" -> Winter 2027 + Spring 2027)."""
    if not text:
        return []
    found: list[tuple[int, str, str]] = []
    for m in _TERM_A_RE.finditer(text):
        found.append((m.start(), m.group(1), m.group(2)))
    for m in _TERM_B_RE.finditer(text):
        found.append((m.start(), m.group(2), m.group(1)))
    out: list[str] = []
    for _, seasons, year in sorted(found):
        for sea in _SEASON_FIND_RE.findall(seasons):
            sea = sea.lower()
            tag = f"{'Fall' if sea == 'autumn' else sea.capitalize()} {year}"
            if tag not in out:
                out.append(tag)
    return out


def merge_terms(*groups) -> list[str]:
    """Merge term lists (free-form strings are normalized through parse_terms), deduped."""
    out: list[str] = []
    for g in groups:
        for t in g or ():
            for tag in parse_terms(str(t)):
                if tag not in out:
                    out.append(tag)
    return out


# ---- result ---------------------------------------------------------------


@dataclass
class Classification:
    """Mirrors the `posting_tags` columns (minus uid / classified_at)."""

    eligible: bool = False
    reject_reason: str | None = None
    categories: list[str] = field(default_factory=list)
    level: str | None = None  # 'internship' | 'coop'
    countries: list[str] = field(default_factory=list)
    cities: list[str] = field(default_factory=list)
    remote: bool = False
    location_opaque: bool = False
    rules_version: int = RULES_VERSION
    terms: list[str] = field(default_factory=list)  # ["Summer 2027", ...]


def _ambiguous_is_swe(title: str, company: str | None, rules: _Rules) -> bool:
    """An ambiguous role term counts as SWE: no hardware veto word, and a software
    anchor in the title or a company outside the hardware-heavy set."""
    if _HW_VETO_RE.search(title) or (
        _HW_VETO_UNLESS_SOFTWARE_RE.search(title) and not _SOFTWARE_RE.search(title)
    ):
        return False
    return bool(_ANCHOR_RE.search(title)) or (company or "").strip().lower() not in rules.hw_companies


def classify(
    job: Job,
    criteria: Criteria,
    location_tiers: dict[str, list[str]],
    tiebreak: Callable[[Job], Verdict | None] | None = None,
) -> Classification:
    """Deterministic eligibility + tags for one posting (see module doc for `tiebreak`)."""
    rules = _rules(criteria, location_tiers)
    # Curly apostrophes ("Master’s") must match the straight ones used in config terms.
    title = (job.title or "").replace("’", "'").replace("‘", "'")

    # level: co-op wins, else internship terms
    level: str | None = None
    if rules.coop.search(title):
        level = "coop"
    elif (rules.intern and rules.intern.search(title)) or _STAGE_RE.search(title):
        level = "internship"

    role_title = _ROLE_VETO_RE.sub(" ", title)
    strong = bool(rules.role and rules.role.search(role_title))
    ambiguous_only = not strong and bool(rules.amb_role and rules.amb_role.search(role_title))
    swe = strong or (ambiguous_only and _ambiguous_is_swe(role_title, job.company, rules))
    categories = ["swe"] if swe else []

    # exclusions run on the title with exempt phrases blanked out
    excl_title = rules.exempt.sub(" ", title) if rules.exempt else title
    excl_match = rules.exclude.search(excl_title) if rules.exclude else None

    years = years_required(job.description)

    # location
    opaque = _is_unresolved_location(job.location)
    remote = _is_remote(job.location)
    loc = _Loc([], [], False) if opaque else _detect_location(job.location or "", rules)
    # "Remote" is not a city/country; a foreign hint vetoes remote-only eligibility.
    loc_ok = opaque or bool(loc.countries) or (remote and not loc.foreign)

    reason: str | None = None
    if level is None:
        reason = "no intern/co-op term in title"
    elif not categories:
        reason = "no swe-category term in title"
    elif excl_match:
        reason = f"excluded title term: {excl_match.group(0).strip().lower()}"
    elif years is not None:
        reason = f"requires {years}+ years of experience"
    elif not loc_ok:
        reason = "outside CA/US"

    # LLM review: any title with a role match (strong or ambiguous) that passes every other
    # gate gets one call. Ambiguous-only: the verdict replaces the rules' SWE decision.
    # Otherwise it can only reject (non-software role, or not open to undergrads).
    llm_terms: list[str] = []
    if (
        tiebreak is not None
        and (strong or ambiguous_only)
        and level is not None
        and not excl_match
        and years is None
        and loc_ok
    ):
        verdict = tiebreak(job)
        if verdict is not None:
            llm_terms = list(verdict.terms)
            if verdict.is_swe and verdict.undergrad_ok:
                if ambiguous_only:
                    categories, reason = ["swe", LLM_TAG], None
            else:
                why = verdict.reason if not verdict.is_swe else f"not open to undergrads: {verdict.reason}"
                reason = f"{REJECT_PREFIX}{why}"[:REJECT_MAX]
                categories = (["swe"] if verdict.is_swe else []) + [LLM_TAG]

    return Classification(
        eligible=reason is None,
        reject_reason=reason,
        categories=categories,
        level=level,
        countries=loc.countries,
        cities=loc.cities,
        remote=remote,
        location_opaque=opaque,
        terms=merge_terms(parse_terms(title), job.terms, llm_terms),
    )
