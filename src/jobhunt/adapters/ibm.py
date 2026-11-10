"""IBM adapter — careers.ibm.com search API (www-api.ibm.com/search/api/v2).

The same Elasticsearch-backed endpoint the IBM careers page calls; no auth, no
config needed beyond ``ats: ibm``. We run an ``intern`` and a ``co-op`` query
(title/keywords/description, newest first), page by ``from`` up to MAX_PAGES,
keep titles matching an internship-like pattern, and dedupe by job id.
"""

from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import parse_qs, urlsplit

from ..models import CompanyConfig, Job
from .base import Adapter, html_to_text, register, request_with_retry

SEARCH_URL = "https://www-api.ibm.com/search/api/v2"
QUERIES = ("intern", "co-op")
INTERN_TITLE_RE = re.compile(r"intern|co-?op|student|new grad", re.IGNORECASE)
PAGE_SIZE = 100
MAX_PAGES = 5  # per query (500 hits)
SOURCE_FIELDS = ["url", "dcdate", "title", "description", "field_keyword_05",
                 "field_keyword_08", "field_keyword_18", "field_keyword_19"]


def _job_id(url: str, fallback: str) -> str:
    ids = parse_qs(urlsplit(url).query).get("jobId")
    return ids[0] if ids else fallback


@register
class IBMAdapter(Adapter):
    ats = "ibm"

    def fetch(self, company: CompanyConfig) -> list[Job]:
        jobs: dict[str, Job] = {}
        for q in QUERIES:
            for page in range(MAX_PAGES):
                body = {
                    "appId": "careers",
                    "scopes": ["careers2"],
                    "query": {"bool": {"must": [{"simple_query_string": {
                        "query": q,
                        "fields": ["title^3", "description^2", "keywords^1", "field_keyword_05^2"],
                    }}]}},
                    "size": PAGE_SIZE,
                    "from": page * PAGE_SIZE,
                    "sort": [{"dcdate": "desc"}],
                    "lang": "zz",
                    "_source": SOURCE_FIELDS,
                }
                r = request_with_retry(self.client, "POST", SEARCH_URL, json=body)
                r.raise_for_status()
                hits = ((r.json().get("hits") or {}).get("hits")) or []
                for h in hits:
                    s = h.get("_source") or {}
                    if not INTERN_TITLE_RE.search(s.get("title") or ""):
                        continue
                    jid = _job_id(s.get("url") or "", str(h.get("_id")))
                    if jid not in jobs:
                        jobs[jid] = self._to_job(s, jid, company)
                if len(hits) < PAGE_SIZE:
                    break
        return list(jobs.values())

    @staticmethod
    def _to_job(s: dict, jid: str, company: CompanyConfig) -> Job:
        try:
            posted = datetime.fromisoformat(str(s.get("dcdate"))[:10])
        except ValueError:
            posted = None
        return Job(
            source="ibm",
            company=company.name,
            external_id=jid,
            title=s.get("title", ""),
            location=s.get("field_keyword_19") or s.get("field_keyword_05"),
            department=s.get("field_keyword_08"),
            employment_type=s.get("field_keyword_18"),
            url=s.get("url") or f"https://careers.ibm.com/careers/JobDetail?jobId={jid}",
            description=html_to_text(s.get("description")),
            posted_at=posted,
        )
