"""Client for Anthropic's OAuth usage endpoint.

This is the authoritative source for limit utilisation and, unlike the
statusline payload, it works with no Claude Code session running.
"""
import json
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timezone

from . import config

_version_cache = None


class UsageError(Exception):
    """Fetch failed. `.kind` distinguishes causes the caller must act on."""

    def __init__(self, message, kind="unknown"):
        super().__init__(message)
        self.kind = kind  # auth | throttled | network | unknown


def claude_version() -> str:
    """The User-Agent must identify as claude-code or the endpoint throttles hard."""
    global _version_cache
    if _version_cache is None:
        try:
            out = subprocess.run(
                ["claude", "--version"], capture_output=True, text=True, timeout=15
            )
            _version_cache = out.stdout.strip().split()[0] or "0.0.0"
        except Exception:
            _version_cache = "0.0.0"
    return _version_cache


def read_token() -> str:
    """Read fresh every call -- Claude Code rotates this file underneath us."""
    try:
        creds = json.loads(config.CREDENTIALS_PATH.read_text())
        oauth = creds["claudeAiOauth"]
    except FileNotFoundError:
        raise UsageError(f"no credentials at {config.CREDENTIALS_PATH}", "auth")
    except (KeyError, json.JSONDecodeError) as e:
        raise UsageError(f"malformed credentials: {e}", "auth")

    expires_at = oauth.get("expiresAt")
    if expires_at and expires_at / 1000 < datetime.now(timezone.utc).timestamp():
        raise UsageError(
            "OAuth token expired; run any claude command to refresh it", "auth"
        )
    return oauth["accessToken"]


def fetch() -> dict:
    req = urllib.request.Request(
        config.USAGE_URL,
        headers={
            "Authorization": f"Bearer {read_token()}",
            "User-Agent": f"claude-code/{claude_version()}",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise UsageError(
                "token rejected; run any claude command to refresh it", "auth"
            )
        if e.code == 429:
            raise UsageError("throttled by usage endpoint (backing off)", "throttled")
        raise UsageError(f"HTTP {e.code}", "unknown")
    except urllib.error.URLError as e:
        raise UsageError(f"network: {e.reason}", "network")


def flatten(payload: dict) -> dict:
    """Pull the few fields we gate on out of the large response."""
    five = payload.get("five_hour") or {}
    seven = payload.get("seven_day") or {}
    extra = payload.get("extra_usage") or {}
    return {
        "five_hour_pct": five.get("utilization"),
        "five_hour_resets_at": five.get("resets_at"),
        "seven_day_pct": seven.get("utilization"),
        "seven_day_resets_at": seven.get("resets_at"),
        "extra_usage_enabled": 1 if extra.get("is_enabled") else 0,
    }
