"""Epic Systems adapter — careers.epic.com's own Next.js jobs page, one GET, no auth.

Config: none required (single-company board). Set ``host`` = ``careers.epic.com`` so
the scheduler's politeness key matches the real hostname.

``GET https://careers.epic.com/jobs/`` server-renders the whole board into the React
Server Components flight payload (``self.__next_f.push([1, "<chunk>"])`` scripts).
Concatenating the decoded chunks yields text containing

* ``"allOpenJobs":[{"id":"740"}, ...]`` — the open + published postings, and
* ``"avaturePositions":{"740":{"externalName", "shortSummary", "background",
  "isOpen", "isPublished", "referenceNumber"}, ...}`` — every position, open or not.

The page carries no search, pagination, location or date fields, so this is one
request returning the full list; description = shortSummary + background. Missing
markers (redesign, challenge page) raise instead of silently returning no jobs.
"""

from __future__ import annotations

import json
import re

from ..models import CompanyConfig, Job
from .base import Adapter, register, request_with_retry

JOBS_URL = "https://careers.epic.com/jobs/"
JOB_URL = "https://careers.epic.com/jobs/{id}/"
MAX_JOBS = 2000  # hard cap on postings emitted from one page

_PUSH_RE = re.compile(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)')
_DECODER = json.JSONDecoder()


def _flight_text(html: str) -> str:
    """Concatenate the decoded RSC flight chunks embedded in the page."""
    return "".join(json.loads(c) for c in _PUSH_RE.findall(html))


def _value_after(text: str, key: str, opener: str):
    """JSON-decode the value that follows `"key":` (must start with `opener`)."""
    m = re.search(rf'"{re.escape(key)}"\s*:\s*', text)
    if not m or text[m.end():m.end() + 1] != opener:
        return None
    start = m.end()
    try:
        value, _ = _DECODER.raw_decode(text, start)
    except ValueError:
        return None
    return value


@register
class EpicAdapter(Adapter):
    ats = "epic"

    def fetch(self, company: CompanyConfig) -> list[Job]:
        resp = request_with_retry(
            self.client, "GET", JOBS_URL, headers={"Accept": "text/html"}
        )
        resp.raise_for_status()
        flight = _flight_text(resp.text)
        open_jobs = _value_after(flight, "allOpenJobs", "[")
        positions = _value_after(flight, "avaturePositions", "{")
        if not isinstance(open_jobs, list) or not isinstance(positions, dict):
            raise ValueError(
                f"{company.name}: epic page lacks allOpenJobs/avaturePositions payload"
            )

        seen: set[str] = set()
        jobs: list[Job] = []
        for entry in open_jobs[:MAX_JOBS]:
            jid = str((entry or {}).get("id") or "") if isinstance(entry, dict) else ""
            pos = positions.get(jid)
            if not jid or jid in seen or not isinstance(pos, dict):
                continue
            if pos.get("isOpen") is False or pos.get("isPublished") is False:
                continue
            title = (pos.get("externalName") or "").strip()
            if not title:
                continue
            seen.add(jid)
            summary = (pos.get("shortSummary") or "").strip()
            background = (pos.get("background") or "").strip()
            parts = [summary] if summary else []
            if background:
                parts.append(f"Background: {background}")
            jobs.append(
                Job(
                    source=self.ats,
                    company=company.name,
                    external_id=jid,
                    title=title,
                    url=JOB_URL.format(id=jid),
                    description="\n\n".join(parts) or None,
                )
            )
        return jobs
