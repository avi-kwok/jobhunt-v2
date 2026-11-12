"""Workday "2 Locations" rollups are resolved via the job detail API before classify."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from jobhunt import db, pipeline
from jobhunt.adapters.workday import detail_url, resolve_locations
from jobhunt.config import load_config
from jobhunt.models import Job

JOB_URL = (
    "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite"
    "/job/Israel-Yokneam/Software-Engineering-Student_JR2028974"
)
DETAIL = (
    "https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite"
    "/job/Israel-Yokneam/Software-Engineering-Student_JR2028974"
)


def _job(location="2 Locations", source="workday", url=JOB_URL, i=1):
    return Job(source=source, company="NVIDIA", external_id=f"JR{i}",
               title="Software Engineering Intern", url=url, location=location)


def test_detail_url():
    assert detail_url(JOB_URL) == DETAIL
    assert detail_url("https://boards.greenhouse.io/x/jobs/1") is None
    assert detail_url(None) is None


@respx.mock
def test_resolve_locations_joins_primary_and_additional():
    respx.get(DETAIL).mock(return_value=httpx.Response(200, json={"jobPostingInfo": {
        "location": "Israel, Yokneam", "additionalLocations": ["Israel, Tel Aviv", "Israel, Yokneam"],
    }}))
    assert resolve_locations(JOB_URL) == "Israel, Yokneam; Israel, Tel Aviv"


@respx.mock
@pytest.mark.parametrize("resp", [
    httpx.Response(404), httpx.Response(200, text="<html>"), httpx.Response(200, json={}),
])
def test_resolve_locations_failure_returns_none(resp):
    respx.get(DETAIL).mock(return_value=resp)
    assert resolve_locations(JOB_URL) is None


@pytest.fixture
def cfg():
    return load_config(Path("config.yaml"), resolved_path=Path("/nonexistent.yaml"))


def _store(conn, jobs):
    return db.insert_new_postings(conn, jobs)


def test_classify_new_rejects_resolved_foreign_rollup(cfg, tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    jobs = _store(conn, [_job()])
    [(job, tags)] = pipeline.classify_new(
        cfg, conn, jobs, resolver=lambda url: "Israel, Yokneam; Israel, Tel Aviv")
    assert not tags.eligible and not tags.location_opaque
    row = conn.execute("SELECT location_raw FROM postings WHERE uid=?", (job.uid,)).fetchone()
    assert row[0] == "Israel, Yokneam; Israel, Tel Aviv"


def test_classify_new_keeps_rollup_with_us_location(cfg, tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    jobs = _store(conn, [_job()])
    [(_, tags)] = pipeline.classify_new(
        cfg, conn, jobs, resolver=lambda url: "Israel, Yokneam; US, CA, Santa Clara")
    assert tags.eligible and tags.countries == ["US"]


def test_resolver_only_called_for_opaque_workday(cfg, tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    calls = []
    jobs = _store(conn, [
        _job(i=1),                                       # opaque workday -> resolved
        _job(i=2, location="US, CA, Santa Clara"),       # readable -> skipped
        _job(i=3, source="greenhouse", url="https://x/3"),  # not workday -> skipped
    ])
    pipeline.classify_new(cfg, conn, jobs, resolver=lambda url: calls.append(url))
    assert calls == [JOB_URL]


def test_resolver_failure_keeps_opaque(cfg, tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    jobs = _store(conn, [_job()])
    [(job, tags)] = pipeline.classify_new(cfg, conn, jobs, resolver=lambda url: None)
    assert job.location == "2 Locations" and tags.location_opaque
