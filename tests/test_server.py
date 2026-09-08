"""The JSON the dashboard renders from."""
import unittest
from datetime import datetime, timedelta, timezone

from csched import db, server


class StateTest(unittest.TestCase):
    def setUp(self):
        db.init()
        self.conn = db.connect()
        for t in ("events", "jobs", "usage_samples", "meta"):
            self.conn.execute(f"DELETE FROM {t}")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def add(self, status="running", bridge=None, finished_at=None, started_at=None):
        cur = self.conn.execute(
            "INSERT INTO jobs(prompt,working_dir,created_at,status,"
            "bridge_session_id,finished_at,started_at) VALUES(?,?,?,?,?,?,?)",
            ("do a thing", "/tmp", db.utcnow(), status, bridge, finished_at,
             started_at))
        self.conn.commit()
        return cur.lastrowid

    def jobs(self):
        return server.state()["queue"]["jobs"]


class RemoteControlLinks(StateTest):
    def test_bridge_id_becomes_a_url(self):
        self.add(bridge="session_01ABC")
        self.assertEqual(self.jobs()[0]["rc_url"],
                         "https://claude.ai/code/session_01ABC")

    def test_absent_bridge_id_is_null_not_a_broken_url(self):
        """A job whose session has not connected yet must not render a link
        to https://claude.ai/code/None."""
        self.add(bridge=None)
        self.assertIsNone(self.jobs()[0]["rc_url"])

    def test_raw_bridge_id_is_not_also_exposed(self):
        """One representation of the URL, built server-side."""
        self.add(bridge="session_01ABC")
        self.assertNotIn("bridge_session_id", self.jobs()[0])


class FinishedJobWindow(StateTest):
    """Regression: finished_at is an ISO string with an offset, but SQLite's
    datetime() renders 'YYYY-MM-DD HH:MM:SS'. Comparing them as text put 'T'
    above ' ', so a job finishing earlier on the cutoff date compared greater
    and was wrongly listed."""

    def test_recently_finished_job_is_listed(self):
        recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        self.add(status="done", finished_at=recent)
        self.assertEqual(len(self.jobs()), 1)

    def test_job_finished_just_before_the_cutoff_is_excluded(self):
        # One minute the far side of the 24h cutoff, so it shares a date with
        # the cutoff -- exactly the case the text comparison got wrong.
        stale = (datetime.now(timezone.utc)
                 - timedelta(days=1, minutes=1)).isoformat()
        self.add(status="done", finished_at=stale)
        self.assertEqual(self.jobs(), [])

    def test_long_finished_job_is_excluded(self):
        old = (datetime.now(timezone.utc) - timedelta(days=9)).isoformat()
        self.add(status="done", finished_at=old)
        self.assertEqual(self.jobs(), [])

    def test_pending_and_running_are_always_listed(self):
        self.add(status="pending")
        self.add(status="running")
        self.assertEqual(len(self.jobs()), 2)


class Shape(StateTest):
    def test_state_has_the_keys_the_page_reads(self):
        state = server.state()
        for key in ("usage", "gate", "config", "queue", "events", "history"):
            self.assertIn(key, state)

    def test_running_before_pending(self):
        self.add(status="pending")
        self.add(status="running")
        self.assertEqual([j["status"] for j in self.jobs()],
                         ["running", "pending"])


if __name__ == "__main__":
    unittest.main()
