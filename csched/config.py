"""Configuration. Env vars override defaults; nothing here is secret."""
import os
from pathlib import Path

HOME = Path.home()
STATE_DIR = Path(os.environ.get("CSCHED_STATE_DIR", HOME / ".local/state/csched"))
DB_PATH = Path(os.environ.get("CSCHED_DB", STATE_DIR / "csched.db"))
OUTPUT_DIR = Path(os.environ.get("CSCHED_OUTPUT_DIR", STATE_DIR / "jobs"))

CREDENTIALS_PATH = Path(
    os.environ.get("CSCHED_CREDENTIALS", HOME / ".claude/.credentials.json")
)
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"

# The /api/oauth/usage endpoint throttles hard without a claude-code User-Agent.
# 180s is the community-established safe floor; don't lower it.
POLL_INTERVAL_S = int(os.environ.get("CSCHED_POLL_INTERVAL", "180"))

# --- Gate thresholds (see README for the reasoning) ---------------------------
# The 5-hour window refills, so holding a reserve costs nothing.
FIVE_HOUR_MAX_PCT = float(os.environ.get("CSCHED_FIVE_HOUR_MAX", "50"))
# The weekly window is use-it-or-lose-it, so we pace rather than cap: spend is
# allowed to run this many points ahead of the fraction of the week elapsed.
WEEKLY_PACE_SLACK = float(os.environ.get("CSCHED_WEEKLY_SLACK", "15"))

# Pace never authorises spending the last of the weekly budget: a job started
# above this hits the real limit mid-flight, which wastes the tokens it spent.
WEEKLY_HARD_MAX_PCT = float(os.environ.get("CSCHED_WEEKLY_HARD_MAX", "95"))

# How many jobs may run at once. Each is a full Claude Code session, so more
# than one both burns budget faster and risks two sessions editing one repo.
MAX_CONCURRENT = int(os.environ.get("CSCHED_MAX_CONCURRENT", "1"))
RUNNER_INTERVAL_S = int(os.environ.get("CSCHED_RUNNER_INTERVAL", "20"))

# A reading older than this is not trusted for gating decisions.
STALE_AFTER_S = int(os.environ.get("CSCHED_STALE_AFTER", "600"))
