"""The JSON the dashboard renders from."""
import json
import unittest
from datetime import datetime, timedelta, timezone

from csched import db, server


class StateTest(unittest.TestCase):
    # Never shell out to `claude` from a test: it may not be installed, and a
    # test suite that depends on it is not hermetic.
    AGENTS = []

    def setUp(self):
        db.init()
        self.conn = db.connect()
        for t in ("events", "jobs", "usage_samples", "meta"):
            self.conn.execute(f"DELETE FROM {t}")
        self.conn.commit()
        server._agents_cache.update(at=float("inf"), value=self.AGENTS)

    def tearDown(self):
        server._agents_cache.update(at=0.0, value=[])
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


class SessionListing(StateTest):
    AGENTS = [
        {"sessionId": "live-1", "name": "my terminal", "cwd": "/repo",
         "kind": "interactive"},
        {"sessionId": "done-1", "name": "finished job", "cwd": "/repo",
         "kind": "background", "state": "done"},
    ]

    def test_live_session_is_marked_busy(self):
        by_id = {s["session_id"]: s for s in server.resumable_sessions(self.conn)}
        self.assertTrue(by_id["live-1"]["busy"])

    def test_finished_session_is_resumable(self):
        by_id = {s["session_id"]: s for s in server.resumable_sessions(self.conn)}
        self.assertFalse(by_id["done-1"]["busy"])

    def test_interactive_sessions_count_as_busy(self):
        """They carry no `state`, so they must not read as idle -- otherwise a
        follow-up would fork the terminal the user is typing in."""
        self.assertIn("live-1", server.busy_sessions())
        self.assertNotIn("done-1", server.busy_sessions())

    def test_pending_job_targeting_a_live_session_is_flagged_blocked(self):
        self.conn.execute(
            "INSERT INTO jobs(prompt,working_dir,created_at,status,"
            "resume_session_id) VALUES('x','/tmp',?,'pending','live-1')",
            (db.utcnow(),))
        self.conn.commit()
        self.assertTrue(server.state()["queue"]["jobs"][0]["blocked"])


class HTTPMixin:
    """Serves the real handler on an ephemeral port.

    A mixin rather than a base class so a subclass can change the fixture
    without also inheriting -- and silently re-running -- the parent's tests.
    """

    def start_http(self):
        from http.server import ThreadingHTTPServer
        import threading
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def stop_http(self):
        self.srv.shutdown()
        self.srv.server_close()

    def post(self, payload):
        import urllib.error
        import urllib.request
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/jobs",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())


class PostJob(HTTPMixin, SessionListing):
    """The HTTP path, since that is where the refusal is actually enforced."""

    def setUp(self):
        super().setUp()
        self.start_http()

    def tearDown(self):
        self.stop_http()
        super().tearDown()

    def test_resuming_a_live_session_is_refused(self):
        """The page disables these in the picker, but it refreshes on a timer,
        so the server has to be the thing that actually holds."""
        status, body = self.post({"prompt": "hi", "resume_session_id": "live-1"})
        self.assertEqual(status, 409)
        self.assertIn("fork", body["error"])
        self.assertEqual(self.jobs(), [])

    def test_resuming_an_idle_session_is_accepted(self):
        status, _ = self.post({"prompt": "hi", "resume_session_id": "done-1"})
        self.assertEqual(status, 200)
        self.assertEqual(self.jobs()[0]["resume_session_id"], "done-1")

    def test_model_is_stored(self):
        self.post({"prompt": "hi", "model": "sonnet"})
        self.assertEqual(self.jobs()[0]["model"], "sonnet")

    def test_absurd_model_name_is_refused(self):
        status, _ = self.post({"prompt": "hi", "model": "x" * 100})
        self.assertEqual(status, 400)

    def test_empty_prompt_is_refused(self):
        status, _ = self.post({"prompt": "   "})
        self.assertEqual(status, 400)


class ListingUnavailable(HTTPMixin, StateTest):
    """`claude` missing or hanging yields None, not an empty listing. Every
    safety check has to treat that as unknown rather than as "nothing is
    running", or it fails open."""

    AGENTS = None

    def setUp(self):
        super().setUp()
        self.start_http()

    def tearDown(self):
        self.stop_http()
        super().tearDown()

    def test_no_sessions_are_offered(self):
        self.assertEqual(server.resumable_sessions(self.conn), [])

    def test_busy_sessions_is_unknown_not_empty(self):
        self.assertIsNone(server.busy_sessions())

    def test_resume_is_refused_rather_than_risked(self):
        status, body = self.post({"prompt": "hi", "resume_session_id": "x"})
        self.assertEqual(status, 409)
        self.assertIn("cannot reach", body["error"])

    def test_a_fresh_job_is_still_accepted(self):
        """Only follow-ups depend on the listing; ordinary work must not stop."""
        status, _ = self.post({"prompt": "hi"})
        self.assertEqual(status, 200)
