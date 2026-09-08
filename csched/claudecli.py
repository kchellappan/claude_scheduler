"""The subprocess boundary to the `claude` CLI.

Isolated behind one class so the runner's state machine can be tested without
launching real sessions.
"""
import json
import os
import re
import subprocess
from pathlib import Path

SESSIONS_DIR = Path(os.environ.get(
    "CSCHED_SESSIONS_DIR", Path.home() / ".claude/sessions"))

# `claude --bg` prints e.g. "backgrounded · f09128df"
_BG_ID = re.compile(r"backgrounded\s*[·:]?\s*([0-9a-f]{6,})")


class SpawnError(Exception):
    pass


class ClaudeCLI:
    def __init__(self, binary="claude", timeout=120):
        self.binary = binary
        self.timeout = timeout

    def _run(self, args, cwd=None, timeout=None):
        return subprocess.run(
            [self.binary, *args], cwd=cwd, capture_output=True, text=True,
            timeout=timeout or self.timeout)

    def spawn(self, prompt, cwd, name):
        """Start a detached, Remote-Control-enabled session. Returns its short id.

        --bg detaches so no TTY is needed; --remote-control still applies, which
        is what makes the session reachable from the phone. Headless (-p) would
        not be: Remote Control attaches to interactive sessions only.
        """
        proc = self._run(["--bg", "--remote-control", name, prompt], cwd=cwd)
        match = _BG_ID.search(proc.stdout or "")
        if not match:
            raise SpawnError(
                (proc.stderr or proc.stdout or "no output").strip()[:400])
        return match.group(1)

    def agents(self):
        """All sessions including completed background ones."""
        try:
            proc = self._run(["agents", "--json", "--all"], timeout=60)
            return json.loads(proc.stdout or "[]")
        except (subprocess.TimeoutExpired, json.JSONDecodeError):
            return []

    def stop(self, bg_id):
        try:
            self._run(["stop", bg_id], timeout=60)
        except subprocess.TimeoutExpired:
            pass

    @staticmethod
    def bridge_session_id(pid):
        """Read the Remote Control session id Claude Code records for a pid.

        The phone-openable URL is https://claude.ai/code/<bridgeSessionId>.
        Absent until Remote Control finishes connecting, so callers retry.
        """
        try:
            data = json.loads((SESSIONS_DIR / f"{pid}.json").read_text())
            return data.get("bridgeSessionId") or None
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None


def remote_control_url(bridge_session_id):
    return f"https://claude.ai/code/{bridge_session_id}" if bridge_session_id else None
