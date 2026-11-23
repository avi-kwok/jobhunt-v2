"""D. E. Shaw careers adapter — server-rendered Next.js page, no auth.

Config mapping: `host` = www.deshaw.com (the page is https://<host>/careers).

The public /careers page embeds its open roles in the <script id="__NEXT_DATA__"> JSON:
props.pageProps.regularJobs and props.pageProps.internships. robots.txt allows /careers and
disallows /careers/open-roles, so only /careers is requested. internalJobs is not public and
is skipped. The page carries the whole list, so there is no pagination.
"""

from __future__ import annotations

import json
import re

from ..models import CompanyConfig, Job
from .base import Adapter, register, request_with_retry

PAGE = "https://{host}/careers"
NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
LISTS = ("regularJobs", "internships")


@register
class DEShawAdapter(Adapter):
    ats = "deshaw"

    def fetch(self, company: CompanyConfig) -> list[Job]:
        if not company.host:
            raise ValueError(f"{company.name}: deshaw adapter needs a host")
        resp = request_with_retry(self.client, "GET", PAGE.format(host=company.host))
        resp.raise_for_status()
        m = NEXT_DATA_RE.search(resp.text)
        if not m:
            raise ValueError(f"{company.name}: deshaw careers page has no __NEXT_DATA__")
        page = ((json.loads(m.group(1)).get("props") or {}).get("pageProps") or {})
        if not any(k in page for k in LISTS):
            raise ValueError(f"{company.name}: deshaw __NEXT_DATA__ has no job lists")

        jobs: list[Job] = []
        seen: set[str] = set()
        for key in LISTS:
            for item in page.get(key) or []:
                data = item.get("data") or {}
                jid = str(data.get("id"))
                if jid in seen or data.get("id") is None or not data.get("jobUrl"):
                    continue
                seen.add(jid)
                locations = (data.get("jobMetadata") or {}).get("jobLocations") or []
                jobs.append(
                    Job(
                        source=self.ats,
                        company=company.name,
                        external_id=jid,
                        title=data.get("displayName") or "",
                        url=f"https://{company.host}/careers/{data.get('jobUrl')}",
                        location=" | ".join(loc["name"] for loc in locations if loc.get("name"))
                        or None,
                        department=(data.get("department") or {}).get("name"),
                    )
                )
        return jobs
