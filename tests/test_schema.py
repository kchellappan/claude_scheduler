"""Schema integrity.

SQLite accepts multi-word type names, so a missing comma between two column
definitions silently merges them into one instead of raising -- a dropped
column that only surfaces as a runtime error much later.
"""
import unittest

from csched import db

JOB_COLUMNS = {
    "id", "prompt", "working_dir", "priority", "status", "created_at",
    "started_at", "finished_at", "attempts", "max_attempts", "session_id",
    "exit_code", "output_path", "error", "bg_id", "bridge_session_id",
    "model", "resume_session_id", "source",
}


class SchemaTest(unittest.TestCase):
    def columns(self, table):
        db.init()
        with db.connect() as conn:
            return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}

    def test_jobs_columns_exactly(self):
        self.assertEqual(self.columns("jobs"), JOB_COLUMNS)

    def test_every_table_exists(self):
        db.init()
        with db.connect() as conn:
            tables = {r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        for t in ("jobs", "events", "usage_samples", "push_subscriptions", "meta"):
            self.assertIn(t, tables)

    def test_no_column_absorbed_a_neighbour(self):
        """A merged definition shows up as a multi-word declared type."""
        db.init()
        with db.connect() as conn:
            for table in ("jobs", "events", "usage_samples"):
                for row in conn.execute(f"PRAGMA table_info({table})"):
                    self.assertLessEqual(
                        len(row["type"].split()), 1,
                        f"{table}.{row['name']} has type {row['type']!r} -- "
                        "a missing comma probably merged two columns")


if __name__ == "__main__":
    unittest.main()
