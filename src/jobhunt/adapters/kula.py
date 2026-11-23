"""Kula adapter — public careers.kula.ai board pages (server-rendered HTML), no auth.

Config mapping: `host` = careers.kula.ai (the Kula careers host, also the politeness key),
`slug` = the board slug in https://<host>/<slug> (e.g. sanctuary-ai, vidyard).

robots.txt for careers.kula.ai allows / with no crawl-delay. One GET per scan: the board page
lists every opening (no search, no pagination, no per-job fetch). Each posting is a block whose
last link is `href="/<slug>/<id>-<title-slug>"` (an optional `?jobs=true` suffix is dropped).
The text lines are the block's <p> elements: the title is the line before the meta line, and the
meta line is either "Department • City, Region, Country • Type • Mode" (4+ parts) or a bare
"Type • Mode" line preceded by a separate location line. The `id="__chakra_env"` app marker must be
present; without it the response is treated as an outage, not an empty board.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

from ..models import CompanyConfig, Job
from .base import Adapter, register, request_with_retry

# Kula's Next.js/Chakra app mounts this span on every board page; an outage or challenge page lacks it.
_APP_MARKER = 'id="__chakra_env"'


class _BlockParser(HTMLParser):
    """Collects (href, [p texts]) for every matching posting link, in document order."""

    def __init__(self, href_re: re.Pattern[str]) -> None:
        super().__init__()
        self._href_re = href_re
        self.blocks: list[tuple[str, list[str]]] = []
        self._lines: list[str] = []
        self._in_p = False
        self._buf = ""

    def handle_starttag(self, tag, attrs):
        if tag == "p":
            self._in_p = True
            self._buf = ""
        elif tag == "a":
            href = dict(attrs).get("href") or ""
            if self._href_re.match(href):
                self.blocks.append((href, self._lines))
                self._lines = []

    def handle_endtag(self, tag):
        if tag == "p" and self._in_p:
            self._in_p = False
            text = " ".join(self._buf.split())
            if text:
                self._lines.append(text)

    def handle_data(self, data):
        if self._in_p:
            self._buf += data


@register
class KulaAdapter(Adapter):
    ats = "kula"

    def fetch(self, company: CompanyConfig) -> list[Job]:
        if not company.host or not company.slug:
            raise ValueError(f"{company.name}: kula adapter needs a host and a slug")
        url = f"https://{company.host}/{company.slug}"
        resp = request_with_retry(self.client, "GET", url, headers={"Accept": "text/html"})
        resp.raise_for_status()
        if _APP_MARKER not in resp.text:
            raise ValueError(f"{company.name}: kula page has no board container")

        href_re = re.compile(rf"/{re.escape(company.slug)}/(\d+)\b")
        parser = _BlockParser(href_re)
        parser.feed(resp.text)

        jobs: list[Job] = []
        seen: set[str] = set()
        for href, lines in parser.blocks:
            m = href_re.match(href)
            if not m or m.group(1) in seen or len(lines) < 2:
                continue
            seen.add(m.group(1))
            meta = lines[-1]
            parts = [p.strip() for p in meta.split("•")] if "•" in meta else []
            if len(parts) >= 4:
                department, location = parts[0], parts[1]
                employment_type = " • ".join(parts[2:])
                title = lines[-2]
            else:
                department = None
                location = lines[-2]
                employment_type = " • ".join(parts) if parts else None
                title = lines[-3] if len(lines) >= 3 else lines[-2]
            path = href.split("?", 1)[0]
            jobs.append(
                Job(
                    source=self.ats,
                    company=company.name,
                    external_id=m.group(1),
                    title=title,
                    url=f"https://{company.host}{path}",
                    location=location or None,
                    department=department or None,
                    employment_type=employment_type or None,
                )
            )
        return jobs
