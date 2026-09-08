"""Runner state machine, with the `claude` subprocess boundary faked out.

No test here launches a real session.
"""
import unittest

from csched import config, db, runner
from csched.claudecli import SpawnError


class FakeCLI:
    """Stands in for ClaudeCLI. `sessions` mimics `claude agents --json --all`."""

    def __init__(self, fail_with=None):
        self.sessions = []
        self.spawned = []
        self.fail_with = fail_with
        self.bridges = {}
        self.busy = set()
        self.listing_ok = True     # False mimics `claude agents` failing
        self._next = 0

    def busy_sessions(self):
        return set(self.busy) if self.listing_ok else None

    def spawn(self, prompt, cwd, name, model=None, resume=None):
        if self.fail_with:
            raise SpawnError(self.fail_with)
        self._next += 1
        bg_id = f"bg{self._next:04d}"
        self.spawned.append({"prompt": prompt, "cwd": cwd, "name": name,
                             "model": model, "resume": resume})
        self.sessions.append({"id": bg_id, "pid": 1000 + self._next,
                              "sessionId": f"uuid-{self._next}", "state": "running"})
        return bg_id

    def agents(self):
        return list(self.sessions) if self.listing_ok else None

    def finish(self, bg_id):
        for s in self.sessions:
            if s["id"] == bg_id:
                s["state"] = "done"

    def vanish(self, bg_id):
        self.sessions = [s for s in self.sessions if s["id"] != bg_id]

    def bridge_session_id(self, pid):
        return self.bridges.get(pid)


class RunnerTest(unittest.TestCase):
    def setUp(self):
        db.init()
        self.conn = db.connect()
        for t in ("events", "jobs", "usage_samples", "meta"):
            self.conn.execute(f"DELETE FROM {t}")
        self.conn.commit()
        self.cli = FakeCLI()

    def tearDown(self):
        self.conn.close()

    def usage(self, five=3.0, seven=10.0):
        """Seed a fresh sample so gate.evaluate has something to read."""
        self.conn.execute(
            "INSERT INTO usage_samples(sampled_at, ok, five_hour_pct, "
            "seven_day_pct, seven_day_resets_at) VALUES (?,1,?,?,NULL)",
            (db.utcnow(), five, seven))
        self.conn.commit()

    def add(self, prompt="do a thing", priority=100, max_attempts=3):
        cur = self.conn.execute(
            "INSERT INTO jobs(prompt,working_dir,priority,created_at,max_attempts)"
            " VALUES(?,?,?,?,?)",
            (prompt, "/tmp", priority, db.utcnow(), max_attempts))
        self.conn.commit()
        return cur.lastrowid

    def job(self, job_id):
        return self.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()

    def kinds(self):
        return [r["kind"] for r in
                self.conn.execute("SELECT kind FROM events ORDER BY id")]


class Launching(RunnerTest):
    def test_launches_when_gate_open(self):
        self.usage()
        jid = self.add()
        runner.tick(self.conn, self.cli)
        self.assertEqual(self.job(jid)["status"], "running")
        self.assertEqual(len(self.cli.spawned), 1)
        self.assertIn("job_started", self.kinds())

    def test_does_not_launch_when_gate_held(self):
        self.usage(five=90.0)          # over the 5-hour cap
        jid = self.add()
        runner.tick(self.conn, self.cli)
        self.assertEqual(self.job(jid)["status"], "pending")
        self.assertEqual(self.cli.spawned, [])

    def test_does_not_launch_on_stale_reading(self):
        """No usage sample at all -- the gate must fail closed."""
        jid = self.add()
        runner.tick(self.conn, self.cli)
        self.assertEqual(self.job(jid)["status"], "pending")

    def test_respects_max_concurrent(self):
        self.usage()
        for _ in range(4):
            self.add()
        runner.tick(self.conn, self.cli)
        self.assertEqual(len(self.cli.spawned), config.MAX_CONCURRENT)

    def test_priority_order(self):
        self.usage()
        self.add("low", priority=200)
        self.add("high", priority=1)
        runner.tick(self.conn, self.cli)
        self.assertEqual(self.cli.spawned[0]["prompt"], "high")

    def test_session_name_identifies_the_job(self):
        self.usage()
        jid = self.add("refactor the gate module")
        runner.tick(self.conn, self.cli)
        self.assertTrue(self.cli.spawned[0]["name"].startswith(f"csched #{jid}:"))


class Completion(RunnerTest):
    def test_done_state_finishes_the_job(self):
        self.usage()
        jid = self.add()
        runner.tick(self.conn, self.cli)
        self.cli.finish(self.job(jid)["bg_id"])
        runner.tick(self.conn, self.cli)
        self.assertEqual(self.job(jid)["status"], "done")
        self.assertIn("job_done", self.kinds())

    def test_vanished_session_does_not_wedge_the_queue(self):
        """A session gone from the listing is unrecoverable; leaving the job
        'running' forever would block every later job."""
        self.usage()
        jid = self.add()
        runner.tick(self.conn, self.cli)
        self.cli.vanish(self.job(jid)["bg_id"])
        runner.tick(self.conn, self.cli)
        self.assertEqual(self.job(jid)["status"], "done")

    def test_queue_drained_emitted_once_when_last_job_finishes(self):
        self.usage()
        jid = self.add()
        runner.tick(self.conn, self.cli)
        self.cli.finish(self.job(jid)["bg_id"])
        runner.tick(self.conn, self.cli)
        self.assertEqual(self.kinds().count("queue_drained"), 1)

    def test_finishing_one_job_starts_the_next(self):
        self.usage()
        first, second = self.add("a"), self.add("b")
        runner.tick(self.conn, self.cli)
        self.cli.finish(self.job(first)["bg_id"])
        runner.tick(self.conn, self.cli)
        self.assertEqual(self.job(second)["status"], "running")


class BridgeIds(RunnerTest):
    def test_backfills_once_remote_control_connects(self):
        """The id is absent at spawn time; it must be picked up later."""
        self.usage()
        jid = self.add()
        runner.tick(self.conn, self.cli)
        self.assertIsNone(self.job(jid)["bridge_session_id"])
        self.cli.bridges[1001] = "session_01ABC"
        runner.tick(self.conn, self.cli)
        self.assertEqual(self.job(jid)["bridge_session_id"], "session_01ABC")
        self.assertIn("job_reachable", self.kinds())

    def test_no_bridge_id_is_not_an_error(self):
        self.usage()
        self.add()
        runner.tick(self.conn, self.cli)
        runner.tick(self.conn, self.cli)
        self.assertNotIn("job_failed", self.kinds())


class SpawnFailure(RunnerTest):
    def test_retries_then_fails(self):
        self.usage()
        self.cli.fail_with = "claude not found"
        jid = self.add(max_attempts=2)
        runner.tick(self.conn, self.cli)
        self.assertEqual(self.job(jid)["status"], "pending")
        self.assertEqual(self.job(jid)["attempts"], 1)
        runner.tick(self.conn, self.cli)
        self.assertEqual(self.job(jid)["status"], "failed")
        self.assertIn("job_failed", self.kinds())

    def test_error_is_recorded(self):
        self.usage()
        self.cli.fail_with = "boom"
        jid = self.add(max_attempts=1)
        runner.tick(self.conn, self.cli)
        self.assertIn("boom", self.job(jid)["error"])


if __name__ == "__main__":
    unittest.main()


class OutputParsing(unittest.TestCase):
    """`claude --bg` prints the short id in prose; we depend on that shape."""

    def test_parses_bg_id(self):
        from csched.claudecli import _BG_ID
        out = "Starting background service…\nbackgrounded · f09128df\n"
        self.assertEqual(_BG_ID.search(out).group(1), "f09128df")

    def test_missing_id_is_detected(self):
        from csched.claudecli import _BG_ID
        self.assertIsNone(_BG_ID.search("error: not logged in"))

    def test_url_shape(self):
        from csched.claudecli import remote_control_url
        self.assertEqual(remote_control_url("session_01A"),
                         "https://claude.ai/code/session_01A")
        self.assertIsNone(remote_control_url(None))


class FastJobs(BridgeIds):
    """Regression: a job that finishes inside one tick must still record its
    Remote Control URL, or the phone has nothing to open."""

    def test_url_captured_even_when_job_finishes_immediately(self):
        self.usage()
        jid = self.add()
        runner.tick(self.conn, self.cli)
        self.cli.bridges[1001] = "session_01FAST"
        self.cli.finish(self.job(jid)["bg_id"])
        runner.tick(self.conn, self.cli)
        row = self.job(jid)
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["bridge_session_id"], "session_01FAST")

    def test_job_done_event_carries_the_url(self):
        import json
        self.usage()
        jid = self.add()
        runner.tick(self.conn, self.cli)
        self.cli.bridges[1001] = "session_01FAST"
        self.cli.finish(self.job(jid)["bg_id"])
        runner.tick(self.conn, self.cli)
        payload = json.loads(self.conn.execute(
            "SELECT payload FROM events WHERE kind='job_done'").fetchone()["payload"])
        self.assertEqual(payload["url"], "https://claude.ai/code/session_01FAST")


class ModelSelection(RunnerTest):
    def test_model_is_passed_through(self):
        self.usage()
        self.conn.execute(
            "INSERT INTO jobs(prompt,working_dir,created_at,model) "
            "VALUES('x','/tmp',?,'sonnet')", (db.utcnow(),))
        self.conn.commit()
        runner.tick(self.conn, self.cli)
        self.assertEqual(self.cli.spawned[0]["model"], "sonnet")

    def test_no_model_means_claude_code_decides(self):
        self.usage()
        self.add()
        runner.tick(self.conn, self.cli)
        self.assertIsNone(self.cli.spawned[0]["model"])


class ResumingSessions(RunnerTest):
    def follow_up(self, target, priority=100):
        cur = self.conn.execute(
            "INSERT INTO jobs(prompt,working_dir,created_at,priority,"
            "resume_session_id) VALUES('follow up','/tmp',?,?,?)",
            (db.utcnow(), priority, target))
        self.conn.commit()
        return cur.lastrowid

    def test_resume_is_passed_through(self):
        self.usage()
        self.follow_up("sess-abc")
        runner.tick(self.conn, self.cli)
        self.assertEqual(self.cli.spawned[0]["resume"], "sess-abc")

    def test_waits_while_the_target_is_live(self):
        """Launching now would fork a copy rather than continue the session."""
        self.usage()
        jid = self.follow_up("sess-abc")
        self.cli.busy = {"sess-abc"}
        runner.tick(self.conn, self.cli)
        self.assertEqual(self.job(jid)["status"], "pending")
        self.assertEqual(self.cli.spawned, [])

    def test_starts_once_the_target_goes_idle(self):
        self.usage()
        jid = self.follow_up("sess-abc")
        self.cli.busy = {"sess-abc"}
        runner.tick(self.conn, self.cli)
        self.cli.busy = set()
        runner.tick(self.conn, self.cli)
        self.assertEqual(self.job(jid)["status"], "running")

    def test_blocked_follow_up_does_not_hold_up_the_queue(self):
        """A waiting follow-up is skipped, not a barrier -- otherwise one
        long-running session stalls every unrelated job behind it."""
        self.usage()
        blocked = self.follow_up("sess-abc", priority=1)
        other = self.add("unrelated work", priority=50)
        self.cli.busy = {"sess-abc"}
        runner.tick(self.conn, self.cli)
        self.assertEqual(self.job(blocked)["status"], "pending")
        self.assertEqual(self.job(other)["status"], "running")


class ListingUnavailable(RunnerTest):
    """Regression: `claude agents` failing returned [], and a job whose session
    is absent counts as finished -- so one timeout marked every running job
    done. Unavailable is now None and must be treated as "do not conclude"."""

    def test_running_jobs_are_not_marked_done(self):
        self.usage()
        jid = self.add()
        runner.tick(self.conn, self.cli)
        self.assertEqual(self.job(jid)["status"], "running")
        self.cli.listing_ok = False         # listing unavailable
        runner.tick(self.conn, self.cli)
        self.assertEqual(self.job(jid)["status"], "running")

    def test_queue_drained_is_not_emitted(self):
        self.usage()
        self.add()
        runner.tick(self.conn, self.cli)
        self.cli.listing_ok = False
        runner.tick(self.conn, self.cli)
        self.assertNotIn("queue_drained", self.kinds())

    def test_follow_ups_are_held(self):
        self.usage()
        cur = self.conn.execute(
            "INSERT INTO jobs(prompt,working_dir,created_at,resume_session_id)"
            " VALUES('f','/tmp',?,'sess-abc')", (db.utcnow(),))
        self.conn.commit()
        self.cli.listing_ok = False
        runner.tick(self.conn, self.cli)
        self.assertEqual(self.job(cur.lastrowid)["status"], "pending")

    def test_ordinary_jobs_still_launch(self):
        self.usage()
        jid = self.add()
        self.cli.listing_ok = False
        runner.tick(self.conn, self.cli)
        self.assertEqual(self.job(jid)["status"], "running")


class RealCLIFailures(unittest.TestCase):
    def test_missing_binary_yields_none_not_empty(self):
        from csched.claudecli import ClaudeCLI
        cli = ClaudeCLI(binary="definitely-not-a-real-binary-xyz")
        self.assertIsNone(cli.agents())
        self.assertIsNone(cli.busy_sessions())

    def test_unknown_session_state_counts_as_busy(self):
        from csched.claudecli import ClaudeCLI
        cli = ClaudeCLI(binary="definitely-not-a-real-binary-xyz")
        self.assertTrue(cli.session_busy("anything"))
