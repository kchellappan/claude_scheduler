"""The run/hold decision.

Two windows, opposite economics:

  * The 5-hour window genuinely refills, so holding a reserve costs nothing.
    We cap it, keeping headroom for interactive sessions.

  * The 7-day window is use-it-or-lose-it -- unspent budget evaporates at reset.
    Capping it would forfeit budget, so we *pace* instead: spend may run ahead
    of the fraction of the week elapsed, but only by WEEKLY_PACE_SLACK points.
    The gate therefore widens as the week runs out, which is the correct
    behaviour when leftovers are worthless -- but never past WEEKLY_HARD_MAX_PCT,
    since a job started on the last of the budget dies mid-flight.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import config

FIVE_HOUR = timedelta(hours=5)
SEVEN_DAY = timedelta(days=7)


def _parse(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def window_elapsed_fraction(resets_at, length, now):
    """How far through a rolling window we are, 0.0 -> 1.0."""
    reset = _parse(resets_at)
    if reset is None:
        return None
    start = reset - length
    frac = (now - start) / length
    return min(max(frac, 0.0), 1.0)


@dataclass
class Gate:
    open: bool
    stale: bool
    reasons: list = field(default_factory=list)
    five_hour_pct: float = None
    seven_day_pct: float = None
    week_elapsed_pct: float = None
    weekly_allowance_pct: float = None
    age_s: float = None

    def summary(self) -> str:
        return ("OPEN" if self.open else "HELD") + (
            "" if not self.reasons else "  (" + "; ".join(self.reasons) + ")"
        )


def evaluate(sample, now=None) -> Gate:
    """`sample` is a usage_samples row (or None if we have never polled)."""
    now = now or datetime.now(timezone.utc)

    if sample is None:
        return Gate(open=False, stale=True, reasons=["no usage reading yet"])

    age = (now - _parse(sample["sampled_at"])).total_seconds()
    stale = age > config.STALE_AFTER_S
    reasons = []
    if stale:
        reasons.append(f"reading is {int(age // 60)}m old")

    five = sample["five_hour_pct"]
    seven = sample["seven_day_pct"]

    if five is not None and five > config.FIVE_HOUR_MAX_PCT:
        reasons.append(f"5h at {five:.0f}% (cap {config.FIVE_HOUR_MAX_PCT:.0f}%)")

    elapsed = window_elapsed_fraction(sample["seven_day_resets_at"], SEVEN_DAY, now)
    allowance = None
    if elapsed is not None and seven is not None:
        allowance = min(
            elapsed * 100 + config.WEEKLY_PACE_SLACK, config.WEEKLY_HARD_MAX_PCT
        )
        if seven > allowance:
            reasons.append(
                f"weekly {seven:.0f}% ahead of pace (allowance {allowance:.0f}%)"
            )

    return Gate(
        open=not reasons,
        stale=stale,
        reasons=reasons,
        five_hour_pct=five,
        seven_day_pct=seven,
        week_elapsed_pct=None if elapsed is None else elapsed * 100,
        weekly_allowance_pct=allowance,
        age_s=age,
    )
