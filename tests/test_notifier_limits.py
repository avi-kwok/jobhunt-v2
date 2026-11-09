"""Discord size limits, url validity, markdown escaping, mention suppression."""

from __future__ import annotations

import json

import httpx

from jobhunt.models import Job
from jobhunt.notifier import DiscordNotifier, alert_embed


def _job(**kw) -> Job:
    base = dict(source="lever", company="X", external_id="1", title="SWE Intern",
                url="https://jobs.example.com/1")
    base.update(kw)
    return Job(**base)


def _embed(job: Job) -> dict:
    return alert_embed(job, 1, 1, "internship", ["swe"])


def _fields(embed: dict) -> dict:
    return {f["name"]: f["value"] for f in embed["fields"]}


def _total(embed: dict) -> int:
    return len(embed["title"]) + sum(len(f["name"]) + len(f["value"]) for f in embed["fields"])


def test_oversize_strings_are_clamped():
    e = _embed(_job(title="T" * 5000, company="C" * 5000, location="L" * 5000))
    f = _fields(e)
    assert len(e["title"]) <= 256 and e["title"].endswith("…")
    assert len(f["Company"]) <= 1024 and len(f["Location"]) <= 1024
    assert f["Location"].endswith("…") or "…" in f["Location"]


def test_bad_urls_omitted():
    for bad in ["", "javascript:alert(1)", "http://jobs.example.com/1", "https://", "/relative",
                "https://a.com/x y", "https://a.com/\n)[x](y)", "https://@/x"]:
        assert "url" not in _embed(_job(url=bad)), bad


def test_https_url_kept():
    assert _embed(_job())["url"] == "https://jobs.example.com/1"


def test_markdown_link_in_location_escaped():
    f = _fields(_embed(_job(location="[click](https://evil.example)")))
    assert "[click](" not in f["Location"]
    assert "\\[click\\]\\(" in f["Location"]


def test_allowed_mentions_sent():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(204)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    DiscordNotifier(webhook_url="https://discord.com/api/webhooks/1/x", client=client).send_embeds(
        [_embed(_job())]
    )
    assert seen["allowed_mentions"] == {"parse": []}


def test_ten_worst_case_embeds_under_6000():
    worst = _job(title="\\" * 5000, company="*" * 5000, location="[" * 5000,
                 posted_note="p" * 5000)
    embeds = [alert_embed(worst, 1, 1, "x" * 500, ["y" * 500, "z" * 500]) for _ in range(10)]
    assert sum(_total(e) for e in embeds) <= 6000
