"""Offline gate check: replay v1 seen_jobs titles through the v2 classifier.

Usage:  .venv/bin/python scripts/replay_v1.py jobhunt.db

Reads v1's `seen_jobs` read-only (it stores no location or description, so the
classifier runs on TITLE ONLY: location None -> opaque, description None).
Nothing is written and there is no network access.

  (a) v1 LLM said yes (matched=1): how many does v2 keep? list every reject.
  (b) v1 LLM judged and said no (score set, matched=0, not an llm error): v2 verdict.
  (c) all rows: v1 prefilter (title checks) vs v2 eligible, with samples.
"""

from __future__ import annotations

import random
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jobhunt.classify import classify  # noqa: E402
from jobhunt.config import load_config  # noqa: E402
from jobhunt.matcher import prefilter  # noqa: E402
from jobhunt.models import Job  # noqa: E402

# v1's PM-adjacent role terms + exempt phrases (removed from config.yaml in v2),
# restored here so the "v1 accepted" side of the comparison is faithful.
V1_PM_ROLE_TERMS = [
    "product manager", "product management", "associate product", "product owner",
    "product marketing", "technical program", "program manager", "program management",
    "tpm", "product intern", "product co-op", "product coop",
]
V1_EXEMPT = ["product manager", "product marketing manager", "program manager"]


def main(db_path: str) -> None:
    cfg = load_config(Path("config.yaml"), resolved_path=Path("/nonexistent.yaml"))
    v2c, tiers = cfg.criteria, cfg.location_tiers
    v1c = v2c.model_copy(
        update={
            "role_terms": list(v2c.role_terms) + V1_PM_ROLE_TERMS,
            "exclude_exempt_terms": V1_EXEMPT,
            "location_match_required": False,  # location unknown in seen_jobs
        }
    )

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT uid, company, title, matched, score, reason FROM seen_jobs"
    ).fetchall()

    def job(r) -> Job:
        return Job(source="v1", company=r["company"] or "", external_id=r["uid"],
                   title=r["title"] or "", url="", location=None, description=None)

    v2 = {r["uid"]: classify(job(r), v2c, tiers) for r in rows}
    v1_pass = {r["uid"]: prefilter(job(r), v1c, tiers)[0] for r in rows}

    print(f"v1 rows: {len(rows)}\n")

    # (a)
    matched = [r for r in rows if r["matched"] == 1]
    kept = [r for r in matched if v2[r["uid"]].eligible]
    rej = [r for r in matched if not v2[r["uid"]].eligible]
    print(f"(a) v1 matched=1: {len(matched)}  ->  v2 eligible: {len(kept)}, rejected: {len(rej)}")
    for r in rej:
        print(f"    REJECT  [{r['company']}] {r['title']!r}  ::  {v2[r['uid']].reject_reason}")

    # (b)
    judged_no = [
        r for r in rows
        if r["score"] is not None and r["matched"] == 0
        and not (r["reason"] or "").startswith("llm_match error")
    ]
    print(f"\n(b) v1 LLM-judged and rejected: {len(judged_no)}")
    for r in judged_no:
        c = v2[r["uid"]]
        verdict = "ELIGIBLE" if c.eligible else f"reject ({c.reject_reason})"
        print(f"    [{r['company']}] {r['title']!r}  v1: {r['reason']!r}  ->  v2: {verdict}")

    # (c)
    n_v1 = sum(v1_pass.values())
    n_v2 = sum(c.eligible for c in v2.values())
    both = sum(1 for r in rows if v1_pass[r["uid"]] and v2[r["uid"]].eligible)
    new_acc = [r for r in rows if v2[r["uid"]].eligible and not v1_pass[r["uid"]]]
    lost = [r for r in rows if v1_pass[r["uid"]] and not v2[r["uid"]].eligible]
    print(f"\n(c) all rows: v1 prefilter (title) passes: {n_v1}  |  v2 eligible: {n_v2}  |  both: {both}")
    print(f"    newly accepted by v2: {len(new_acc)}   v1-accepted but v2 rejects: {len(lost)}")
    rng = random.Random(0)
    print("\n    30 sample titles v2 NEWLY accepts:")
    for r in rng.sample(new_acc, min(30, len(new_acc))):
        print(f"      [{r['company']}] {r['title']}")
    print("\n    30 sample titles v1 accepted that v2 REJECTS:")
    for r in rng.sample(lost, min(30, len(lost))):
        print(f"      [{r['company']}] {r['title']}  ::  {v2[r['uid']].reject_reason}")
    from collections import Counter

    print("\n    reject reasons among v1-accepted/v2-rejected:")
    for reason, n in Counter(
        (v2[r["uid"]].reject_reason or "").split(":")[0] for r in lost
    ).most_common():
        print(f"      {n:5d}  {reason}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "jobhunt.db")
