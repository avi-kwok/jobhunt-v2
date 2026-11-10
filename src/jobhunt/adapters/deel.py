"""Deel (jobs.deel.com) adapter — one HTML request per board.

Deel's hosted job boards are Next.js pages that embed the *complete* posting list
(titles, locations, employment types, departments, createdAt, descriptions) in the
streamed React Server Component payload (``self.__next_f.push([1,"..."])``). The
public JSON API under /api/ is disallowed by robots.txt, so we read the embedded
payload of the board page (allowed) instead. There is no server-side search or
pagination: one GET returns every posting, so no extra requests are made.

Config mapping: ``slug`` = Deel org slug (``https://jobs.deel.com/<slug>``).
Set ``host: jobs.deel.com`` so the politeness key is shared across Deel boards.
"""

from __future__ import annotations

import json
import re
from datetime import datetime

from ..models import CompanyConfig, Job
from .base import Adapter, html_to_text, register, request_with_retry

BOARD = "https://jobs.deel.com/{slug}"
JOB_URL = "https://jobs.deel.com/{slug}/job-details/{id}/overview"
_HEADERS = {"Accept": "text/html"}

_PUSH_RE = re.compile(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)')


def _flight_text(html: str) -> str:
    return "".join(json.loads('"' + m.group(1) + '"') for m in _PUSH_RE.finditer(html))


def _parse_postings(html: str) -> tuple[list[dict], str]:
    stream = _flight_text(html)
    key = '"jobPostings":'
    i = stream.find(key)
    if i < 0:
        # Redesign or a WAF/challenge page served with 200: not an empty board.
        raise ValueError("deel: 'jobPostings' key missing from page payload")
    arr, _ = json.JSONDecoder().raw_decode(stream[i + len(key):])
    if not isinstance(arr, list):
        raise ValueError("deel: 'jobPostings' is not a list")
    return arr, stream


def _text_chunks(stream: str) -> dict[str, str]:
    """Map RSC text-chunk id -> text. Lengths are UTF-8 byte counts."""
    raw = stream.encode()
    out: dict[str, str] = {}
    for m in re.finditer(rb"(?<![0-9a-zA-Z])([0-9a-f]{1,4}):T([0-9a-f]+),", raw):
        n = int(m.group(2), 16)
        out[m.group(1).decode()] = raw[m.end():m.end() + n].decode("utf-8", "replace")
    return out


def _dt(value) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


@register
class DeelAdapter(Adapter):
    ats = "deel"

    def fetch(self, company: CompanyConfig) -> list[Job]:
        if not company.slug:
            raise ValueError(f"{company.name}: deel adapter needs a slug")
        resp = request_with_retry(
            self.client, "GET", BOARD.format(slug=company.slug), headers=_HEADERS
        )
        resp.raise_for_status()
        postings, stream = _parse_postings(resp.text)
        chunks = _text_chunks(stream) if postings else {}

        jobs: list[Job] = []
        seen: set[str] = set()
        for p in postings:
            pid = str(p.get("id") or "")
            if not pid or pid in seen:
                continue
            seen.add(pid)
            job = p.get("job") or {}
            locs = [
                (x.get("location") or {}).get("name")
                for x in job.get("jobLocations") or []
            ]
            locs = [x for x in locs if x]
            etypes = job.get("jobEmploymentTypes") or []
            depts = job.get("jobDepartments") or []
            desc = p.get("richtextDescription") or ""
            if desc.startswith("$"):  # reference to a text chunk in the stream
                desc = chunks.get(desc[1:], "")
            jobs.append(
                Job(
                    source=self.ats,
                    company=company.name,
                    external_id=pid,
                    title=(p.get("title") or "").strip(),
                    location="; ".join(locs) or None,
                    department=((depts[0].get("department") or {}).get("name") if depts else None),
                    employment_type=((etypes[0].get("employmentType") or {}).get("name") if etypes else None),
                    url=JOB_URL.format(slug=company.slug, id=pid),
                    description=html_to_text(desc),
                    posted_at=_dt(p.get("createdAt")),
                )
            )
        return jobs
