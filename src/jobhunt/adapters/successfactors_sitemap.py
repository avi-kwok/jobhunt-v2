"""SAP SuccessFactors RMK "Unify" career sites — public ``/sitemap.xml`` (+ capped detail pages).

Unify sites (jobs.bombardier.com, jobs.opg.com, careers.deloitte.ca, ...) render
``/search/`` client-side from ``/services/recruiting/...``, which robots.txt disallows,
so the HTML-search ``successfactors`` adapter finds no rows there. ``/sitemap.xml`` is
allowed and comes in one of two shapes, both handled from a single request:

* **RSS** (``<rss><channel><item>``): every posting with ``<title>`` ("Title (Location)"),
  ``<link>``, ``<g:id>``, ``<g:location>`` and an HTML ``<description>``. Everything we
  need is in the feed, so no further requests are made.
* **urlset**: only ``/job/<City>-<Title>-<Region>-<Postal>/<id>/`` URLs. Postings whose
  slug looks like an internship are kept; for up to ``MAX_DETAILS`` of them (English
  terms first, then newest id first) we GET the posting page (``DETAIL_DELAY`` apart;
  each is a 302 + 200) for the exact title (``itemprop="title"``), description and
  "Posting Start Date". The rest, or all of them once a detail request fails or is
  not a 200, fall back to a slug-derived title — we never retry around a block.
  Slug fallback strips the region/postal tail (or a bare US/CA state code) and a
  leading city: a first token (plus the second, when every posting starting with
  the first shares it, e.g. "Nakhon-Ratchasima") shared by >=3 postings on the board.

Both branches keep only intern / co-op / student / campus / stagiaire / etudiant titles;
the worker's classifier then applies the SWE-role rules. Postings carry no list-level
date, so ``posted_at`` comes from detail pages only.

Config mapping: ``host`` = career-site hostname (e.g. ``jobs.bombardier.com``); it is
also the per-host politeness key. ``slug``, ``site`` and ``tenant`` are unused.
"""

from __future__ import annotations

import html
import re
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import unquote
from xml.etree import ElementTree

import httpx

from ..models import CompanyConfig, Job
from .base import Adapter, html_to_text, register, request_with_retry

MAX_DETAILS = 20  # posting pages per scan (urlset sites only)
DETAIL_DELAY = 2.0  # seconds between posting pages; each is a 302 + 200, so ~1 req/s
_HEADERS = {"Accept": "application/xml,text/xml,text/html"}

# Whole-word-ish internship terms. Boundaries are lowercase letters only, so a slug's
# run-together "InternCo-op" still matches while "Internal"/"Cooperative" do not.
_TERMS_EN = r"intern(?:s|ships?)?|co-?ops?|students?|campus"
_TERMS_FR = r"stagiaires?|[ée]tudiant(?:e|es|s)?"
_WORD = r"(?<![a-zà-ÿ])(?i:{})(?![a-zà-ÿ])"
INTERN_RE = re.compile(_WORD.format(f"{_TERMS_EN}|{_TERMS_FR}"))
_INTERN_EN_RE = re.compile(_WORD.format(_TERMS_EN))

_JOB_PATH_RE = re.compile(r"/job/([^/]+)/(\d+)/?$")
_ID_RE = re.compile(r"/(\d+)(?:-[A-Za-z_]+)?/?$")
# Trailing "<Region>-<Postal>" of a urlset slug: Canadian, US ZIP(+4), UK, or plain digits.
_POSTAL = r"[A-Z]\d[A-Z]-?\d[A-Z]\d|\d{5}(?:-\d{4})?|[A-Z]{1,2}\d[A-Z\d]?-\d[A-Z]{2}|\d{4,6}"
_TAIL_RE = re.compile(rf"(?:-(?P<region>[^\W\d_]{{2,4}}))?-(?P<postal>{_POSTAL})$")
_CA_POSTAL_RE = re.compile(r"^[A-Z]\d[A-Z] ?\d[A-Z]\d$")
# Bare trailing state/province code with no postal ("...-Intern-Summer-2027-MN").
_REGION_TAIL_RE = re.compile(r"-([A-Z]{2})$")
_REGIONS = frozenset(
    "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE "
    "NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY "
    "AB BC MB NB NL NS NT NU ON PE QC SK YT".split()
)
_DATE_RE = re.compile(
    r"(?:Posting Start Date|Posting Date|Date Posted)\s*:?\s*</span>\s*<span[^>]*>\s*([^<]+?)\s*</span>",
    re.I,
)
_TITLE_TAG_RE = re.compile(r"<title>\s*(.*?)\s+Job Details\s*\|", re.S | re.I)


class _ItempropParser(HTMLParser):
    """Collect the inner HTML of every ``itemprop="title"`` / ``itemprop="description"``."""

    def __init__(self) -> None:
        super().__init__()
        self.props: dict[str, list[str]] = {}
        self._prop: str | None = None
        self._tag = ""
        self._depth = 0
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs) -> None:
        if self._prop is None:
            prop = dict(attrs).get("itemprop")
            if prop in ("title", "description"):
                self._prop, self._tag, self._depth, self._buf = prop, tag, 1, []
            return
        if tag == self._tag:
            self._depth += 1
        self._buf.append(self.get_starttag_text() or "")

    def handle_endtag(self, tag) -> None:
        if self._prop is None:
            return
        if tag == self._tag:
            self._depth -= 1
            if self._depth == 0:
                self.props.setdefault(self._prop, []).append("".join(self._buf))
                self._prop = None
                return
        self._buf.append(f"</{tag}>")

    def handle_data(self, data) -> None:
        if self._prop is not None:
            self._buf.append(html.escape(data, quote=False))


def _clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", html.unescape(text or "")).strip()


def _parse_date(text: str | None) -> datetime | None:
    for fmt in ("%m/%d/%y", "%m/%d/%Y", "%Y-%m-%d", "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime((text or "").strip(), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def parse_detail(page: str) -> dict | None:
    """Title / description / posted_at from a posting page; None if it has no title."""
    p = _ItempropParser()
    p.feed(page)
    title = _clean(re.sub(r"<[^>]+>", " ", (p.props.get("title") or [""])[0]))
    if not title:
        m = _TITLE_TAG_RE.search(page)
        title = _clean(m.group(1)) if m else ""
    if not title:
        return None
    desc = "\n".join(p.props.get("description") or [])
    d = _DATE_RE.search(page)
    return {
        "title": title,
        "description": html_to_text(desc),
        "posted_at": _parse_date(d.group(1)) if d else None,
    }


def _alnum_index(s: str) -> tuple[str, list[int]]:
    keep = [(c.lower(), i) for i, c in enumerate(s) if c.isalnum()]
    return "".join(c for c, _ in keep), [i for _, i in keep]


def _location(city: str, tail: str) -> str | None:
    """'St-Laurent' + 'Québ-H4S-1Y9' -> 'St Laurent, Québ H4S 1Y9, CA'."""
    tail = re.sub(r"^[\W_]+", "", tail)  # leftover ")" etc. after the title
    m = _TAIL_RE.search("-" + tail) if tail else None
    region = postal = ""
    if m:
        region = m.group("region") or ""
        postal = m.group("postal").replace("-", " ")
    elif tail:
        region = tail.replace("-", " ")
    place = " ".join(p for p in (region, postal) if p)
    parts = [city.replace("-", " ").strip(), place]
    if _CA_POSTAL_RE.match(postal):
        parts.append("CA")  # Canadian postal codes are unambiguous; helps location rules
    return ", ".join(p for p in parts if p) or None


def city_tokens(slugs: list[str]) -> dict[str, int]:
    """First slug token -> how many leading tokens are the city (1 or 2).

    A first token counts as a city only when >=3 postings start with it; the second
    token joins it when every one of those postings shares it ("Nakhon-Ratchasima").
    """
    toks = [s.split("-") for s in slugs]
    first: dict[str, int] = {}
    pairs: dict[tuple[str, str], int] = {}
    for t in toks:
        first[t[0]] = first.get(t[0], 0) + 1
        if len(t) > 1:
            pairs[(t[0], t[1])] = pairs.get((t[0], t[1]), 0) + 1
    out: dict[str, int] = {}
    for (a, b), n in pairs.items():
        if first[a] >= 3:
            out[a] = 2 if n == first[a] else 1
    return out


def split_slug(slug: str, title: str | None = None, cities: dict[str, int] | None = None) -> tuple[str, str]:
    """Split a urlset slug into (title, location).

    With the real title (from the posting page) we locate it inside the slug, so the
    prefix is the city and the suffix the region/postal. Without it we strip the
    region/postal tail (or a bare US/CA state code) and, using ``cities`` from
    ``city_tokens``, a leading city.
    """
    if title:
        norm_slug, idx = _alnum_index(slug)
        norm_title, _ = _alnum_index(title)
        pos = norm_slug.find(norm_title) if norm_title else -1
        if pos >= 0:
            start, end = idx[pos], idx[pos + len(norm_title) - 1] + 1
            return title, _location(slug[:start].strip("-"), slug[end:].strip("-")) or ""
    m = _TAIL_RE.search(slug)
    r = None if m else _REGION_TAIL_RE.search(slug)
    if r and r.group(1) not in _REGIONS:
        r = None
    cut = m or r
    head, tail = (slug[: cut.start()], slug[cut.start() + 1:]) if cut else (slug, "")
    toks = head.split("-")
    k = (cities or {}).get(toks[0], 0)
    k = k if len(toks) > k + 1 else 0  # never eat the whole title
    city, head = "-".join(toks[:k]), "-".join(toks[k:])
    fallback = re.sub(r"\s+", " ", head.replace("-", " ")).strip()
    return title or fallback, _location(city, tail) or ""


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


@register
class SuccessFactorsSitemapAdapter(Adapter):
    ats = "successfactors_sitemap"

    def fetch(self, company: CompanyConfig) -> list[Job]:
        if not company.host:
            raise ValueError(f"{company.name}: successfactors_sitemap adapter needs host")
        base = f"https://{company.host}"
        resp = request_with_retry(self.client, "GET", f"{base}/sitemap.xml", headers=_HEADERS)
        resp.raise_for_status()
        try:
            root = ElementTree.fromstring(resp.content)
        except ElementTree.ParseError as exc:
            raise ValueError(f"{company.name}: sitemap.xml is not XML (challenge/redesign?)") from exc
        tag = _local(root.tag)
        if tag == "rss":
            return self._from_rss(company, root)
        if tag == "urlset":
            return self._from_urlset(company, root)
        raise ValueError(f"{company.name}: unexpected sitemap root <{tag}>")

    # ---- RSS feed (everything in one response) ----------------------------

    def _from_rss(self, company: CompanyConfig, root) -> list[Job]:
        channel = root.find("channel")
        if channel is None:
            raise ValueError(f"{company.name}: RSS sitemap has no <channel>")
        seen: dict[str, Job] = {}
        for item in channel.iter("item"):
            f = {_local(el.tag): (el.text or "").strip() for el in item}
            raw_title, link = _clean(f.get("title")), f.get("link") or ""
            if not raw_title or not link or not INTERN_RE.search(raw_title):
                continue
            loc = _clean(f.get("location")) or None
            title = raw_title
            if loc and title.endswith(f"({loc})"):
                title = title[: -len(loc) - 2].strip()
            m = _ID_RE.search(link)
            ext = f.get("id") or f.get("guid") or (m.group(1) if m else link)
            seen.setdefault(ext, Job(
                source=self.ats,
                company=company.name,
                external_id=ext,
                title=title,
                location=loc,
                url=link,
                # CDATA holds entity-escaped HTML
                description=html_to_text(html.unescape(f.get("description") or "")),
            ))
        return list(seen.values())

    # ---- urlset sitemap (+ capped posting pages) ---------------------------

    def _from_urlset(self, company: CompanyConfig, root) -> list[Job]:
        cands: dict[str, tuple[str, str]] = {}  # id -> (url, decoded slug)
        all_slugs: dict[str, str] = {}  # id -> decoded slug, every posting (city stats)
        locs = job_urls = 0
        for el in root.iter():
            if _local(el.tag) != "loc":
                continue
            locs += 1
            url = (el.text or "").strip()
            m = _JOB_PATH_RE.search(url)
            if not m:
                continue
            job_urls += 1
            slug = unquote(m.group(1))
            all_slugs.setdefault(m.group(2), slug)
            if INTERN_RE.search(slug):
                cands.setdefault(m.group(2), (url, slug))
        if locs and not job_urls:
            raise ValueError(f"{company.name}: sitemap has URLs but no /job/ postings (layout changed?)")
        # English terms first (the classifier's vocabulary), then newest id first.
        order = sorted(
            cands.items(),
            key=lambda kv: (bool(_INTERN_EN_RE.search(kv[1][1])), int(kv[0])),
            reverse=True,
        )
        cities = city_tokens(list(all_slugs.values()))
        jobs: list[Job] = []
        fetch_details = True
        for n, (ext, (url, slug)) in enumerate(order):
            detail = None
            if fetch_details and n < MAX_DETAILS:
                if n:
                    time.sleep(DETAIL_DELAY)
                try:
                    r = request_with_retry(
                        self.client, "GET", url, headers=_HEADERS, follow_redirects=True
                    )
                except httpx.HTTPError:
                    fetch_details = False
                else:
                    if r.status_code == 200:
                        detail = parse_detail(r.text)
                    if detail is None:  # non-200 or not a posting page: stop, no workaround
                        fetch_details = False
            title, location = split_slug(slug, detail["title"] if detail else None, cities)
            jobs.append(Job(
                source=self.ats,
                company=company.name,
                external_id=ext,
                title=title,
                location=location or None,
                url=url,
                description=detail["description"] if detail else None,
                posted_at=detail["posted_at"] if detail else None,
            ))
        return jobs
