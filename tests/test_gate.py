"""The gate is the one piece where a wrong answer silently wastes budget."""
import unittest
from datetime import datetime, timedelta, timezone

from csched import config, gate

NOW = datetime(2026, 9, 7, 23, 19, tzinfo=timezone.utc)
WEEK_END = datetime(2026, 9, 8, 21, 0, tzinfo=timezone.utc)      # ~87% elapsed
WEEK_START = datetime(2026, 9, 14, 21, 0, tzinfo=timezone.utc)   # ~1% elapsed


def sample(five=3.0, seven=19.0, resets=WEEK_END, sampled=None):
    return {
        "sampled_at": (sampled or NOW).isoformat(),
        "five_hour_pct": five,
        "seven_day_pct": seven,
        "seven_day_resets_at": resets.isoformat() if resets else None,
    }


class FiveHourCap(unittest.TestCase):
    def test_under_cap_opens(self):
        self.assertTrue(gate.evaluate(sample(five=10), NOW).open)

    def test_over_cap_holds(self):
        g = gate.evaluate(sample(five=72), NOW)
        self.assertFalse(g.open)
        self.assertIn("5h at 72%", g.reasons[0])

    def test_boundary_is_inclusive(self):
        """At exactly the cap we still run; the cap is a ceiling, not a fence."""
        self.assertTrue(gate.evaluate(sample(five=config.FIVE_HOUR_MAX_PCT), NOW).open)


class WeeklyPacing(unittest.TestCase):
    def test_early_week_overspend_holds(self):
        """1% into the week, 60% spent -> far ahead of pace."""
        g = gate.evaluate(sample(seven=60, resets=WEEK_START), NOW)
        self.assertFalse(g.open)
        self.assertIn("ahead of pace", g.reasons[0])

    def test_early_week_underspend_opens(self):
        self.assertTrue(gate.evaluate(sample(seven=10, resets=WEEK_START), NOW).open)

    def test_gate_widens_as_week_runs_out(self):
        """80% spent holds early in the week but opens late, because unspent
        weekly budget is forfeited at reset."""
        self.assertFalse(gate.evaluate(sample(seven=80, resets=WEEK_START), NOW).open)
        self.assertTrue(gate.evaluate(sample(seven=80, resets=WEEK_END), NOW).open)

    def test_slack_is_applied(self):
        """Spend may run WEEKLY_PACE_SLACK points ahead of elapsed time."""
        g = gate.evaluate(sample(seven=10, resets=WEEK_START), NOW)
        self.assertAlmostEqual(g.weekly_allowance_pct,
                               g.week_elapsed_pct + config.WEEKLY_PACE_SLACK,
                               places=4)


class WeeklyHardCeiling(unittest.TestCase):
    """Regression: pace alone opened the gate at 99% because the allowance
    computed to 102%. A job started there dies mid-flight and wastes every
    token it spent getting there."""

    def test_near_wall_holds_even_at_end_of_week(self):
        g = gate.evaluate(sample(seven=99, resets=WEEK_END), NOW)
        self.assertFalse(g.open)

    def test_allowance_never_exceeds_hard_max(self):
        g = gate.evaluate(sample(seven=50, resets=WEEK_END), NOW)
        self.assertLessEqual(g.weekly_allowance_pct, config.WEEKLY_HARD_MAX_PCT)


class Staleness(unittest.TestCase):
    def test_old_reading_holds(self):
        g = gate.evaluate(sample(sampled=NOW - timedelta(hours=2)), NOW)
        self.assertFalse(g.open)
        self.assertTrue(g.stale)

    def test_fresh_reading_is_not_stale(self):
        self.assertFalse(gate.evaluate(sample(), NOW).stale)

    def test_no_sample_at_all_holds(self):
        g = gate.evaluate(None, NOW)
        self.assertFalse(g.open)
        self.assertTrue(g.stale)


class MissingFields(unittest.TestCase):
    """The endpoint returns nulls for windows that do not apply to a plan."""

    def test_missing_weekly_reset_is_not_binding(self):
        self.assertTrue(gate.evaluate(sample(seven=99, resets=None), NOW).open)

    def test_missing_percentages_do_not_crash(self):
        g = gate.evaluate(sample(five=None, seven=None), NOW)
        self.assertTrue(g.open)


class WindowMath(unittest.TestCase):
    def test_fraction_clamps_to_unit_interval(self):
        past = (NOW - timedelta(days=30)).isoformat()
        future = (NOW + timedelta(days=30)).isoformat()
        self.assertEqual(gate.window_elapsed_fraction(past, gate.SEVEN_DAY, NOW), 1.0)
        self.assertEqual(gate.window_elapsed_fraction(future, gate.SEVEN_DAY, NOW), 0.0)

    def test_midpoint(self):
        mid = (NOW + timedelta(days=3.5)).isoformat()
        self.assertAlmostEqual(
            gate.window_elapsed_fraction(mid, gate.SEVEN_DAY, NOW), 0.5, places=3)

    def test_unparseable_timestamp_is_none(self):
        self.assertIsNone(gate.window_elapsed_fraction("not-a-date", gate.SEVEN_DAY, NOW))


if __name__ == "__main__":
    unittest.main()
