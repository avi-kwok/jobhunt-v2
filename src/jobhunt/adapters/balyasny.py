"""Balyasny (BAM) adapter — Salesforce Experience Cloud careers site, guest Apex action.

bamfunds.com/careers "Open Roles" links to ``https://bambusdev.my.site.com/s/``. The page's
``c:bamSearch`` LWC loads its list through the guest-accessible Apex method
``BamJobRequisitionInfoDataService.searchJobRequisitions``, which the site calls through
the standard Aura endpoint ``POST /s/sfsites/aura`` (``aura://ApexActionController/
ACTION$execute``). We send the same call. robots.txt is the Salesforce default (Allow: /).

Config (no new fields):

* ``host`` = site host (default ``bambusdev.my.site.com``); also the scheduler's
  politeness key, so set it explicitly in the resolved entry.
* ``site`` = the Apex ``site`` argument (default ``"BAM Website"``, what the page sends).

One call returns every matching requisition (no pagination). We ask twice and union
the results by record Id: the ``Internships`` experience-level filter, plus a keyword
search for ``intern`` (catches a posting mis-tagged as "Experience Professionals").
Each response is capped at ``MAX_RESULTS`` records. The list payload has no description
and only a relative "Posted N Days Ago" note, which goes to ``posted_note``.

The Aura context carries no ``fwuid``: guest Apex actions answer without it (the server
appends a harmless "client out of sync" notice), so a Salesforce release does not break
the call. A body that is not an Aura actions payload, or whose action did not succeed
(redesign, renamed Apex class, challenge page), raises instead of returning no jobs.
"""

from __future__ import annotations

import json
import time
from urllib.parse import quote

from ..models import CompanyConfig, Job
from .base import Adapter, register, request_with_retry

DEFAULT_HOST = "bambusdev.my.site.com"
DEFAULT_SITE = "BAM Website"
APEX_CLASS = "BamJobRequisitionInfoDataService"
APEX_METHOD = "searchJobRequisitions"
ACTION_ID = "1;a"
# Server-side intern filters; results are unioned by record Id.
SEARCHES: tuple[dict, ...] = (
    {"experienceLevelFilter": ["Internships"]},
    {"searchKey": "intern"},
)
MAX_RESULTS = 500  # hard cap per response (the whole board is ~130 records)
REQUEST_DELAY = 1.0  # seconds between the calls (politeness: ~1 req/s per host)
CONTEXT = json.dumps({"mode": "PROD", "app": "siteforce:communityApp"})


def _locations(rec: dict) -> str | None:
    names: list[str] = []
    for pos in rec.get("Job_Requisition_Positions__r") or []:
        locs = [pos.get("Location__r")] + [
            x.get("Location__r") for x in pos.get("Job_Requisition_Position_Locations__r") or []
        ]
        for loc in locs:
            name = ((loc or {}).get("External_Name__c") or "").strip()
            if name and name not in names:
                names.append(name)
    return "; ".join(names) or None


@register
class BalyasnyAdapter(Adapter):
    ats = "balyasny"

    def _search(self, company: CompanyConfig, host: str, extra: dict) -> list[dict]:
        params = {
            "isVendorPortal": False,
            "site": company.site or DEFAULT_SITE,
            "searchKey": "",
            "locationFilters": [],
            "departmentFilter": [],
            "availableLocations": [],
            "experienceLevelFilter": [],
            **extra,
        }
        message = {"actions": [{
            "id": ACTION_ID,
            "descriptor": "aura://ApexActionController/ACTION$execute",
            "callingDescriptor": "UNKNOWN",
            "params": {
                "namespace": "", "classname": APEX_CLASS, "method": APEX_METHOD,
                "params": params, "cacheable": True, "isContinuation": False,
            },
        }]}
        resp = request_with_retry(
            self.client, "POST",
            f"https://{host}/s/sfsites/aura?aura.ApexAction.execute=1",
            data={
                "message": json.dumps(message),
                "aura.context": CONTEXT,
                "aura.pageURI": "/s/",
                "aura.token": "null",
            },
        )
        resp.raise_for_status()
        try:
            actions = resp.json().get("actions")
        except (ValueError, AttributeError):
            actions = None
        action = next(
            (a for a in actions or [] if isinstance(a, dict) and a.get("id") == ACTION_ID), None
        )
        if action is None:
            raise ValueError(f"{company.name}: balyasny response is not an Aura actions payload")
        if action.get("state") != "SUCCESS":
            raise ValueError(
                f"{company.name}: balyasny Apex action {action.get('state')}: "
                f"{str(action.get('error'))[:200]}"
            )
        records = (action.get("returnValue") or {}).get("returnValue")
        if not isinstance(records, list):
            raise ValueError(f"{company.name}: balyasny Apex action returned no record list")
        return records[:MAX_RESULTS]

    def fetch(self, company: CompanyConfig) -> list[Job]:
        host = company.host or DEFAULT_HOST
        seen: set[str] = set()
        jobs: list[Job] = []
        for i, extra in enumerate(SEARCHES):
            if i:
                time.sleep(REQUEST_DELAY)
            for rec in self._search(company, host, extra):
                rid = str(rec.get("Id") or "")
                title = (rec.get("Publish_Title__c") or rec.get("Name") or "").strip()
                if not rid or not title or rid in seen:
                    continue
                seen.add(rid)
                slug = f"{rec.get('Job_Req_Title_in_URL__c') or ''}_{rec.get('Requisition_Number__c') or ''}"
                jobs.append(
                    Job(
                        source=self.ats,
                        company=company.name,
                        external_id=rid,
                        title=title,
                        location=_locations(rec),
                        department=rec.get("Department__c") or None,
                        employment_type=rec.get("Experience_Level__c") or None,
                        url=f"https://{host}/s/details?jobReq={quote(slug, safe='')}",
                        posted_note=rec.get("Posted_Ago__c") or None,
                    )
                )
        return jobs
