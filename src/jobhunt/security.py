"""Filesystem, git, and logging safeguards for the API key.

Goals:
  * The key lives only in `.env`, is loaded into the process env, and is never
    written to disk elsewhere, printed, or committed.
  * `.env` is owner-read/write only (chmod 600) so no other user account on the
    machine can read it.
  * The key is never staged/committed to git.
  * Any accidental echo of the key is redacted.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

from dotenv import find_dotenv, load_dotenv


def resolve_env_path() -> Path:
    """The ONE `.env` path used for loading, auditing and preflight.

    Walks up from the current working directory (``find_dotenv(usecwd=True)``),
    falling back to ``./.env`` when none exists yet. Previously the CLI loaded
    via ``load_dotenv()`` (which walks up from the installed script) while the
    audit checked ``Path(".env")`` in the cwd, so they could disagree.
    """
    found = find_dotenv(usecwd=True)
    return Path(found) if found else Path.cwd() / ".env"


def load_env() -> Path:
    """Load the resolved `.env` into os.environ (existing vars win). Returns the path."""
    path = resolve_env_path()
    if path.exists():
        load_dotenv(path)
    return path


# Anthropic keys look like: sk-ant-api03-....  Redact anything key-shaped.
_KEY_RE = re.compile(r"sk-ant-[A-Za-z0-9\-_]{8,}")

# A webhook URL *is* a credential: anyone holding it can post to the channel.
# httpx embeds the full request URL in HTTPStatusError, so an error message can
# carry the secret into stdout -> logs/cron.log. Mask the id/token, keep the
# host so the message stays diagnosable ("which sink failed?").
# Covers Discord (/api/webhooks/<id>/<token>), Slack (hooks.slack.com/services/
# T../B../x), and self-hosted /webhook/<token> sinks. The `/services/` form is
# pinned to Slack's host so ordinary posting URLs are never mangled.
_WEBHOOK_RE = re.compile(
    r"(https?://\S*?/webhooks?/|https?://hooks\.slack\.com/services/)"
    r"[A-Za-z0-9_\-./]+",
    re.IGNORECASE,
)


class SecurityError(RuntimeError):
    """Raised when a hard security precondition fails."""


def redact(text: str) -> str:
    """Mask every known secret shape before text can be shown or logged.

    This is the single chokepoint: anything printed that might have touched a
    key or a webhook URL should pass through here first.
    """
    out = _KEY_RE.sub("sk-ant-***REDACTED***", text or "")
    return _WEBHOOK_RE.sub(r"\1***REDACTED***", out)


def get_api_key(*, required: bool = True) -> str | None:
    """Read the key from the environment only. Never from a file or argument.

    Returns None (or raises, if required) when unset. The value is never logged.
    """
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        if required:
            raise SecurityError(
                "ANTHROPIC_API_KEY is not set. Put it in .env "
                "(never in code or on the command line)."
            )
        return None
    return key


def harden_env_permissions(path: Path | None = None) -> list[str]:
    """Force `.env` to 0600 (owner-only). Returns human-readable notes."""
    path = path or resolve_env_path()
    notes: list[str] = []
    if not path.exists():
        return notes
    mode = stat.S_IMODE(path.stat().st_mode)
    group_other = mode & (stat.S_IRWXG | stat.S_IRWXO)
    if group_other:
        os.chmod(path, 0o600)
        notes.append(f"tightened {path} permissions to 600 (was {oct(mode)})")
    return notes


def env_is_git_ignored(path: Path | None = None) -> bool:
    """True if git would ignore `.env` (so it can never be accidentally added)."""
    path = path or resolve_env_path()
    try:
        result = subprocess.run(
            ["git", "check-ignore", str(path)],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0
    except Exception:  # noqa: BLE001 - not a git repo / git missing: treat as unknown
        return False


def env_is_tracked(path: Path | None = None) -> bool:
    """True if `.env` is already tracked by git (a serious problem)."""
    path = path or resolve_env_path()
    try:
        result = subprocess.run(
            ["git", "ls-files", "--error-unmatch", str(path)],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def audit(path: Path | None = None) -> tuple[bool, list[tuple[str, bool, str]]]:
    """Run every check. Returns (all_ok, [(label, ok, detail), ...])."""
    path = path or resolve_env_path()
    checks: list[tuple[str, bool, str]] = []

    # 1. key present (not printed)
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    checks.append(
        ("API key present in env", bool(key), "set" if key else "missing — add to .env")
    )
    if key:
        checks.append(
            (
                "API key format looks valid",
                key.startswith("sk-ant-"),
                "ok" if key.startswith("sk-ant-") else "does not start with sk-ant-",
            )
        )

    # 2. .env exists and is 0600
    if path.exists():
        mode = stat.S_IMODE(path.stat().st_mode)
        owner_only = not (mode & (stat.S_IRWXG | stat.S_IRWXO))
        checks.append(
            (".env is owner-only (600)", owner_only, oct(mode))
        )
    else:
        checks.append((".env exists", False, "not found"))

    # 3. gitignored and not tracked
    checks.append((".env is git-ignored", env_is_git_ignored(path), ""))
    tracked = env_is_tracked(path)
    checks.append((".env is NOT tracked by git", not tracked, "TRACKED!" if tracked else "ok"))

    all_ok = all(ok for _, ok, _ in checks)
    return all_ok, checks


def preflight(path: Path | None = None) -> list[str]:
    """Run the cheap protections before any API use. Returns warnings to show.

    Hardens `.env` permissions and refuses to run if the key is somehow tracked
    by git (which would mean it's about to be, or already is, committed).
    """
    path = path or resolve_env_path()
    notes = harden_env_permissions(path)
    if env_is_tracked(path):
        raise SecurityError(
            f"{path} is tracked by git — your key could be committed. "
            f"Run: git rm --cached {path}  (it is git-ignored going forward)."
        )
    return notes
