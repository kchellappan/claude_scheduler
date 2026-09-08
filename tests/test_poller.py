"""Events must be edge-triggered. Level-triggered checks would notify the
phone every poll interval for as long as a condition held."""
import unittest

from csched import db, poller


def rows(conn, sql, *a):
    return [dict(r) for r in conn.execute(sql, a)]


class EdgeTriggering(unittest.TestCase):
    def setUp(self):
        db.init()
        self.conn = db.connect()
        for t in ("events", "jobs", "usage_samples", "meta"):
            self.conn.execute(f"DELETE FROM {t}")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def kinds(self):
        return [r["kind"] for r in rows(self.conn, "SELECT kind FROM events ORDER BY id")]

    def gate(self, is_open):
        class G:
            open = is_open
            five_hour_pct, seven_day_pct, reasons = 10.0, 20.0, []
        return G()

    def test_repeated_open_emits_once(self):
        for _ in range(5):
            poller.reconcile(self.conn, self.gate(True))
        self.assertEqual(self.kinds().count("gate_opened"), 1)

    def test_transition_back_and_forth_emits_each_edge(self):
        poller.reconcile(self.conn, self.gate(True))
        poller.reconcile(self.conn, self.gate(False))
        poller.reconcile(self.conn, self.gate(True))
        self.assertEqual(self.kinds(),
                         ["gate_opened", "capacity_idle", "gate_closed", "gate_opened",
                          "capacity_idle"])

    def test_capacity_idle_requires_empty_queue(self):
        self.conn.execute(
            "INSERT INTO jobs(prompt,working_dir,created_at) VALUES('x','/tmp',?)",
            (db.utcnow(),))
        poller.reconcile(self.conn, self.gate(True))
        self.assertIn("gate_opened", self.kinds())
        self.assertNotIn("capacity_idle", self.kinds())

    def test_capacity_idle_fires_when_queue_drains(self):
        self.conn.execute(
            "INSERT INTO jobs(prompt,working_dir,created_at) VALUES('x','/tmp',?)",
            (db.utcnow(),))
        poller.reconcile(self.conn, self.gate(True))
        self.conn.execute("UPDATE jobs SET status='done'")
        poller.reconcile(self.conn, self.gate(True))
        self.assertEqual(self.kinds().count("capacity_idle"), 1)

    def test_queue_depth_counts_only_live_jobs(self):
        for status in ("pending", "pending", "running", "done", "cancelled"):
            self.conn.execute(
                "INSERT INTO jobs(prompt,working_dir,created_at,status) "
                "VALUES('x','/tmp',?,?)", (db.utcnow(), status))
        self.assertEqual(poller.queue_depth(self.conn), (2, 1))


if __name__ == "__main__":
    unittest.main()
