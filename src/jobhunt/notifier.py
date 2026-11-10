"""Discord webhook sink: alert embeds, status embeds, redacted HTTP errors."""

from __future__ import annotations

import re
from datetime import timezone
from urllib.parse import urlsplit

import httpx

from .models import Job
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
# Discord caps a message at 6000 embed chars; 10 embeds -> keep each under ~600.
# Budget: title 180 ("<company> — <position>") + priority ~20 + location 100 + posted 60
# + type 100 + names ~30 (~490 worst case).
TITLE_MAX, COMPANY_MAX, LOCATION_MAX, POSTED_MAX, TYPE_MAX = 180, 60, 100, 60, 100
TITLE_SEP = " — "
MIN_POSITION = 40  # the position is never clipped below this (COMPANY_MAX keeps it true)
_MD_SPECIAL = re.compile(r"([\\*_~`|>\[\]()#<])")


def _clip(text: str, limit: int) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _md_escape(text: str) -> str:
    """Backslash-escape Discord markdown so board text can't render links/formatting."""
    return _MD_SPECIAL.sub(r"\\\1", str(text))


def _safe_url(url: str | None) -> str | None:
    """Only absolute https URLs with a host (Discord 400s on the rest); else None."""
    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return None
    u = url.strip()
    if parts.scheme != "https" or not parts.hostname or len(u) > 2000:
        return None
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in u):
        return None
    return u


class DiscordHTTPError(RuntimeError):
    """A Discord webhook call failed with an HTTP status (message is redacted)."""

    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


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


def alert_embed(
    job: Job, company_priority: int, location_tier: int, level: str | None, categories: list[str]
) -> dict:
    """v2 alert embed: no score / LLM reason, just the deterministic tags."""
    prio_label, color = PRIORITY_LABELS.get(company_priority, PRIORITY_LABELS[4])
    # "llm" is an internal marker (llm_tiebreak.LLM_TAG), not shown to subscribers.
    why = " · ".join([x for x in [level, *categories] if x and x != "llm"]) or "-"
    company = _clip(_md_escape(job.company), COMPANY_MAX)
    room = max(TITLE_MAX - len(company) - len(TITLE_SEP), MIN_POSITION)
    embed = {
        "title": f"{company}{TITLE_SEP}{_clip(job.title, room)}",
        "color": color,
        "fields": [
            {"name": "Priority", "value": prio_label, "inline": True},
            {
                "name": "Location",
                "value": _clip(
                    f"{_md_escape(job.location or 'unspecified')} · "
                    f"{LOCATION_LABELS.get(location_tier, 'Other')}",
                    LOCATION_MAX,
                ),
                "inline": True,
            },
            {"name": "Posted", "value": _clip(_posted_display(job), POSTED_MAX), "inline": True},
            {"name": "Type", "value": _clip(why, TYPE_MAX), "inline": False},
        ],
    }
    url = _safe_url(job.url)
    if url:
        embed["url"] = url
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
            resp = client.post(self.webhook_url, json={"embeds": embeds, "allowed_mentions": {"parse": []}})
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

    def send_embeds(self, embeds: list[dict]) -> None:
        """Post pre-built embeds, at most MAX_EMBEDS_PER_MESSAGE per HTTP call."""
        for i in range(0, len(embeds), MAX_EMBEDS_PER_MESSAGE):
            self._post(embeds[i : i + MAX_EMBEDS_PER_MESSAGE])

    def send_status(
        self, title: str, fields: list[tuple[str, str]], color: int
    ) -> None:
        """Post one informational status embed (title + name/value fields).

        No-ops if there are no fields. Raises via `_post` (same as `send_embeds`) when
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
