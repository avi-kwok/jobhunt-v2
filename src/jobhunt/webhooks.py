"""Discord webhook URL parsing. Outbound calls are built from (id, token) only."""

from __future__ import annotations

import re
from typing import NamedTuple
from urllib.parse import urlsplit

ALLOWED_HOSTS = {"discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com"}
_PATH_RE = re.compile(r"^/api/webhooks/([0-9]{17,20})/([A-Za-z0-9_-]{60,80})/?$", re.ASCII)


class WebhookError(ValueError):
    """Not an acceptable Discord webhook URL (message never contains the URL)."""


class Webhook(NamedTuple):
    id: str
    token: str

    @property
    def url(self) -> str:
        return f"https://discord.com/api/webhooks/{self.id}/{self.token}"

    def __repr__(self) -> str:
        return f"Webhook(id={mask(self.id)})"


def parse(raw: str) -> Webhook:
    bad = WebhookError("not a valid Discord webhook URL")
    if not isinstance(raw, str) or len(raw) > 300:
        raise bad
    raw = raw.strip()
    try:
        u = urlsplit(raw)
        port = u.port
    except ValueError:
        raise bad from None
    if (
        u.scheme != "https" or u.hostname not in ALLOWED_HOSTS or u.username is not None
        or u.password is not None or port is not None or u.query or u.fragment
        or "?" in raw or "#" in raw or "@" in u.netloc
    ):
        raise bad
    m = _PATH_RE.match(u.path)
    if not m:
        raise bad
    return Webhook(m.group(1), m.group(2))


def normalize(raw: str) -> str:
    return parse(raw).url


def mask(webhook_id: str) -> str:
    return f"...{webhook_id[-4:]}"
