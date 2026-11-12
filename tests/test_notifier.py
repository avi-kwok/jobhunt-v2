"""Notifier embed formatting — in particular the Posted field."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from jobhunt.notifier import _posted_display, alert_embed
from jobhunt.models import Job


def _embed(job: Job) -> dict:
    return alert_embed(job, ["CA"], "internship", ["swe"])


def _posted_field(job: Job) -> str:
    fields = {f["name"]: f["value"] for f in _embed(job)["fields"]}
    return fields["Posted"]


def test_posted_display_exact_timestamp_uses_discord_markdown():
    job = Job(
        source="lever", company="X", external_id="1", title="SWE Intern", url="u",
        posted_at=datetime(2026, 7, 3, 14, 21, tzinfo=timezone.utc),
    )
    epoch = int(datetime(2026, 7, 3, 14, 21, tzinfo=timezone.utc).timestamp())
    assert _posted_display(job) == f"<t:{epoch}:f> (<t:{epoch}:R>)"
    # exact timestamp also drives the embed footer time
    assert _embed(job)["timestamp"].startswith("2026-07-03T14:21")


def test_posted_display_naive_timestamp_treated_as_utc():
    naive = Job(
        source="greenhouse", company="X", external_id="1", title="SWE Intern", url="u",
        posted_at=datetime(2026, 7, 3, 14, 21),  # no tzinfo
    )
    epoch = int(datetime(2026, 7, 3, 14, 21, tzinfo=timezone.utc).timestamp())
    assert _posted_display(naive) == f"<t:{epoch}:f> (<t:{epoch}:R>)"


def test_posted_display_falls_back_to_relative_note():
    job = Job(
        source="workday", company="TD", external_id="R1", title="SWE Intern", url="u",
        posted_at=None, posted_note="Posted Today",
    )
    assert _posted_field(job) == "Posted Today"
    assert "timestamp" not in _embed(job)  # no exact time -> no footer time


def test_posted_display_unknown_when_nothing_available():
    job = Job(source="workday", company="TD", external_id="R1", title="X", url="u")
    assert _posted_field(job) == "unknown"


# ---- webhook URL must never escape in an error message --------------------

def test_http_error_does_not_leak_webhook_url():
    """A Discord 4xx must not carry the webhook (a credential) into the message."""
    import httpx

    from jobhunt.notifier import DiscordNotifier

    secret = "https://discord.com/api/webhooks/1234567890/SUPERSECRETTOKEN"
    client = httpx.Client(
        transport=httpx.MockTransport(lambda req: httpx.Response(404, json={}))
    )
    with pytest.raises(RuntimeError) as err:
        DiscordNotifier(webhook_url=secret, client=client).send_status(
            "t", [("a", "b")], 0
        )
    assert "SUPERSECRETTOKEN" not in str(err.value)
    assert "REDACTED" in str(err.value)


def test_connect_error_does_not_leak_webhook_url():
    import httpx

    from jobhunt.notifier import DiscordNotifier

    secret = "https://discord.com/api/webhooks/1234567890/SUPERSECRETTOKEN"

    def boom(request):
        raise httpx.ConnectError(f"failed connecting to {request.url}", request=request)

    client = httpx.Client(transport=httpx.MockTransport(boom))
    with pytest.raises(RuntimeError) as err:
        DiscordNotifier(webhook_url=secret, client=client).send_status(
            "t", [("a", "b")], 0
        )
    assert "SUPERSECRETTOKEN" not in str(err.value)
