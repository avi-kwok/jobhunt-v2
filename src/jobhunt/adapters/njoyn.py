"""Njoyn adapter — server-rendered xweb.asp job-listing form, no auth.

Config (generic fields only):

- ``host``   = the Njoyn host, e.g. ``cgi.njoyn.com`` (also the politeness key).
- ``slug``   = the client path segment before ``/xweb/``, e.g. ``CORP``.
- ``site``   = the client id (``clid``), e.g. ``21001``.
- ``tenant`` = optional comma-separated job-type codes for the board's student/intern
  employment type (the value the careers page puts in ``Inp_Jobtype``). These codes are
  client-specific; CGI's "Student Internship" option is
  ``APP,CCFT,CCPT,INT,TBD,TRD,TRN,VIE,WS,DSPB,DSPM,TSBM,CDAP,CDPR,``.

Strategy: POST the listing form once per search and union the results by job id:
the job-type search (when ``tenant`` is set) plus a keyword search for each of
``KEYWORDS`` (catches interns filed under another job type). Each search pages
through "Page N of M" (50 rows per page), capped at ``MAX_PAGES``. Requests are
spaced ``REQUEST_DELAY`` seconds apart (~1 req/s per host).

The list has job id, title, category, city and country, but no description or
posting date (the job id's MMYY prefix is too coarse to be a timestamp), so both
stay empty. A page without the ``filterform`` search form (redesign, challenge
page) raises instead of silently returning no jobs.
"""

from __future__ import annotations

import re
import time
from html import unescape
from urllib.parse import urljoin

from ..models import CompanyConfig, Job
from .base import Adapter, register, request_with_retry

LIST_URL = "https://{host}/{slug}/xweb/xweb.asp?NTKN=c&clid={site}&Page=JobListing&lang=1"
KEYWORDS = ("intern", "internship", "co-op")
MAX_PAGES = 5
REQUEST_DELAY = 1.0
MARKER = 'id="filterform"'

_ROW_RE = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.S | re.I)
_CELL_RE = re.compile(r"<td\b[^>]*>(.*?)</td>", re.S | re.I)
_HREF_RE = re.compile(r"""href=['"]([^'"]*Page=JobDetails[^'"]*)['"]""", re.I)
_JOBID_RE = re.compile(r"[?&]Jobid=([^&]+)", re.I)
_PAGES_RE = re.compile(r"Page\s+(\d+)\s+of\s+(\d+)", re.I)
_TAG_RE = re.compile(r"<[^>]+>")


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", unescape(_TAG_RE.sub(" ", fragment))).strip()


def _parse_rows(html: str, base_url: str) -> list[dict]:
    rows = []
    for m in _ROW_RE.finditer(html):
        row = m.group(1)
        href = _HREF_RE.search(row)
        if not href:
            continue
        url = urljoin(base_url, unescape(href.group(1)))
        jid = _JOBID_RE.search(url)
        cells = [_text(c) for c in _CELL_RE.findall(row)]
        if not jid or len(cells) < 2 or not cells[1]:
            continue
        rows.append({
            "id": jid.group(1),
            "url": url,
            "title": cells[1],
            "category": cells[2] if len(cells) > 2 else "",
            "location": ", ".join(c for c in cells[3:5] if c) or None,
        })
    return rows


@register
class NjoynAdapter(Adapter):
    ats = "njoyn"

    def fetch(self, company: CompanyConfig) -> list[Job]:
        if not (company.host and company.slug and company.site):
            raise ValueError(f"{company.name}: njoyn adapter needs host, slug and site (clid)")
        url = LIST_URL.format(host=company.host, slug=company.slug, site=company.site)
        searches = [{"Inp_Keywords": kw, "Inp_Jobtype": ""} for kw in KEYWORDS]
        if company.tenant:
            searches.insert(0, {"Inp_Keywords": "", "Inp_Jobtype": company.tenant})

        jobs: dict[str, Job] = {}
        first = True
        for search in searches:
            page, last = 1, 1
            while page <= min(last, MAX_PAGES):
                if not first:
                    time.sleep(REQUEST_DELAY)
                first = False
                form = {**search, "s": " EnterDate Desc ", "pn": str(page), "searchFilled": "1"}
                resp = request_with_retry(self.client, "POST", url, data=form)
                resp.raise_for_status()
                html = resp.text
                if MARKER not in html:
                    raise ValueError(f"{company.name}: njoyn response has no job-listing form")
                rows = _parse_rows(html, url)
                pages = _PAGES_RE.search(html)
                if pages and not rows:
                    raise ValueError(f"{company.name}: njoyn pager present but no job rows parsed")
                for r in rows:
                    if r["id"] in jobs:
                        continue
                    jobs[r["id"]] = Job(
                        source=self.ats,
                        company=company.name,
                        external_id=r["id"],
                        title=r["title"],
                        location=r["location"],
                        department=r["category"] or None,
                        url=r["url"],
                    )
                last = int(pages.group(2)) if pages else 1
                page += 1
        return list(jobs.values())
