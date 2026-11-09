"""Notification sinks. Discord webhook is the default; the protocol is swappable."""

from __future__ import annotations

from datetime import timezone
from typing import Protocol, runtime_checkable

import httpx

from .matcher import composite_key
from .models import Job, MatchResult
from .security import redact

# tier -> (label, embed color)
PRIORITY_LABELS = {
    1: ("Top priority", 0x2ECC71),
    2: ("Strong", 0x3498DB),
    3: ("Tracking", 0xF1C40F),
    4: ("Default", 0x95A5A6),
}
LOCATION_LABELS = {
    1: "Local (Van/Tor)",
    2: "US metro",
    3: "Rest of Canada",
    4: "Remote",
    5: "Other",
}
MAX_EMBEDS_PER_MESSAGE = 10


class DiscordHTTPError(RuntimeError):
    """A Discord webhook call failed with an HTTP status (message is redacted)."""

    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


@runtime_checkable
class Notifier(Protocol):
    def send(self, matches: list[tuple[Job, MatchResult]]) -> None: ...


def _posted_display(job: Job) -> str:
    """When the posting was put up, for the embed.

    Prefers an exact timestamp (rendered as Discord dynamic markdown that shows
    the reader's local date/time plus a live "x days ago"); falls back to the
    relative note some sources give (Workday), else "unknown".
    """
    if job.posted_at is not None:
        posted = job.posted_at
        if posted.tzinfo is None:  # naive timestamps are treated as UTC
            posted = posted.replace(tzinfo=timezone.utc)
        epoch = int(posted.timestamp())
        return f"<t:{epoch}:f> (<t:{epoch}:R>)"
    if job.posted_note:
        return job.posted_note
    return "unknown"


def _embed(job: Job, match: MatchResult) -> dict:
    prio_label, color = PRIORITY_LABELS.get(
        match.company_priority, PRIORITY_LABELS[4]
    )
    loc_label = LOCATION_LABELS.get(match.location_tier, "Other")
    embed = {
        "title": job.title[:250],
        "url": job.url,
        "color": color,
        "fields": [
            {"name": "Company", "value": f"{job.company} ({prio_label})", "inline": True},
            {
                "name": "Location",
                "value": f"{job.location or 'unspecified'} · {loc_label}",
                "inline": True,
            },
            {"name": "Score", "value": str(match.score), "inline": True},
            {"name": "Posted", "value": _posted_display(job), "inline": True},
            {"name": "Why", "value": (match.reason or "-")[:1000], "inline": False},
        ],
    }
    # An exact timestamp also drives the embed footer time.
    if job.posted_at is not None:
        posted = job.posted_at
        if posted.tzinfo is None:
            posted = posted.replace(tzinfo=timezone.utc)
        embed["timestamp"] = posted.isoformat()
    return embed


def alert_embed(
    job: Job, company_priority: int, location_tier: int, level: str | None, categories: list[str]
) -> dict:
    """v2 alert embed: no score / LLM reason, just the deterministic tags."""
    prio_label, color = PRIORITY_LABELS.get(company_priority, PRIORITY_LABELS[4])
    why = " · ".join([x for x in [level, *categories] if x]) or "-"
    embed = {
        "title": job.title[:250],
        "url": job.url,
        "color": color,
        "fields": [
            {"name": "Company", "value": f"{job.company} ({prio_label})", "inline": True},
            {
                "name": "Location",
                "value": f"{job.location or 'unspecified'} · "
                f"{LOCATION_LABELS.get(location_tier, 'Other')}",
                "inline": True,
            },
            {"name": "Posted", "value": _posted_display(job), "inline": True},
            {"name": "Type", "value": why, "inline": False},
        ],
    }
    if job.posted_at is not None:
        posted = job.posted_at
        if posted.tzinfo is None:
            posted = posted.replace(tzinfo=timezone.utc)
        embed["timestamp"] = posted.isoformat()
    return embed


class DiscordNotifier:
    """Posts matches as rich embeds to a Discord webhook, best-sorted and batched.

    `webhook_url` is always passed explicitly (built from a parsed webhook); there is
    deliberately no environment fallback, so alerts can never reach the wrong channel.
    """

    def __init__(self, webhook_url: str | None = None, client: httpx.Client | None = None):
        self.webhook_url = webhook_url
        self._client = client

    def _post(self, embeds: list[dict]) -> None:
        if not self.webhook_url:
            raise RuntimeError("webhook URL is not set")  # no secret to leak
        client = self._client or httpx.Client(timeout=20.0)
        try:
            resp = client.post(self.webhook_url, json={"embeds": embeds})
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                # httpx puts the full request URL in the message, and the
                # webhook URL is itself a credential. Re-raise redacted so the
                # secret can never reach stdout / logs / a crash report.
                raise DiscordHTTPError(redact(str(exc)), resp.status_code) from None
        except httpx.RequestError as exc:
            raise RuntimeError(redact(str(exc))) from None
        finally:
            if self._client is None:
                client.close()

    def send(self, matches: list[tuple[Job, MatchResult]]) -> None:
        if not matches:
            return
        ordered = sorted(matches, key=lambda pair: composite_key(pair[1]))
        embeds = [_embed(job, match) for job, match in ordered]
        for i in range(0, len(embeds), MAX_EMBEDS_PER_MESSAGE):
            self._post(embeds[i : i + MAX_EMBEDS_PER_MESSAGE])

    def send_embeds(self, embeds: list[dict]) -> None:
        """Post pre-built embeds, at most MAX_EMBEDS_PER_MESSAGE per HTTP call."""
        for i in range(0, len(embeds), MAX_EMBEDS_PER_MESSAGE):
            self._post(embeds[i : i + MAX_EMBEDS_PER_MESSAGE])

    def send_status(
        self, title: str, fields: list[tuple[str, str]], color: int
    ) -> None:
        """Post one informational status embed (title + name/value fields).

        No-ops if there are no fields. Raises via `_post` (same as `send`) when
        the webhook URL is unset, so callers get consistent behaviour.
        """
        if not fields:
            return
        embed = {
            "title": title,
            "color": color,
            "fields": [
                # Short values render nicely inline; long ones get their own row.
                {"name": name, "value": value or "-", "inline": len(str(value)) <= 24}
                for name, value in fields
            ],
        }
        self._post([embed])
