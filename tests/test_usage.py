"""Parsing and auth-failure handling. Nothing here touches the network."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from csched import config, usage


class Flatten(unittest.TestCase):
    def test_extracts_the_gated_fields(self):
        flat = usage.flatten({
            "five_hour": {"utilization": 3.0, "resets_at": "2026-09-08T04:10:00+00:00"},
            "seven_day": {"utilization": 19.0, "resets_at": "2026-09-08T21:00:00+00:00"},
            "extra_usage": {"is_enabled": True},
        })
        self.assertEqual(flat["five_hour_pct"], 3.0)
        self.assertEqual(flat["seven_day_pct"], 19.0)
        self.assertEqual(flat["extra_usage_enabled"], 1)

    def test_nulls_do_not_crash(self):
        """The endpoint returns null for windows that do not apply to a plan."""
        flat = usage.flatten({"five_hour": None, "seven_day": None, "extra_usage": None})
        self.assertIsNone(flat["five_hour_pct"])
        self.assertEqual(flat["extra_usage_enabled"], 0)

    def test_empty_payload(self):
        self.assertIsNone(usage.flatten({})["seven_day_pct"])


class TokenReading(unittest.TestCase):
    def _creds(self, expires_at):
        path = Path(tempfile.mkdtemp()) / ".credentials.json"
        path.write_text(json.dumps({"claudeAiOauth": {
            "accessToken": "test-token", "expiresAt": expires_at}}))
        config.CREDENTIALS_PATH = path
        return path

    def test_valid_token_is_returned(self):
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).timestamp() * 1000
        self._creds(future)
        self.assertEqual(usage.read_token(), "test-token")

    def test_expired_token_raises_auth(self):
        """Claude Code rotates this file; a stale token must be reported as an
        auth problem so the poller emits auth_stale rather than dying."""
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).timestamp() * 1000
        self._creds(past)
        with self.assertRaises(usage.UsageError) as cm:
            usage.read_token()
        self.assertEqual(cm.exception.kind, "auth")

    def test_missing_file_raises_auth(self):
        config.CREDENTIALS_PATH = Path("/nonexistent/creds.json")
        with self.assertRaises(usage.UsageError) as cm:
            usage.read_token()
        self.assertEqual(cm.exception.kind, "auth")

    def test_malformed_file_raises_auth(self):
        path = Path(tempfile.mkdtemp()) / "bad.json"
        path.write_text("{not json")
        config.CREDENTIALS_PATH = path
        with self.assertRaises(usage.UsageError) as cm:
            usage.read_token()
        self.assertEqual(cm.exception.kind, "auth")


if __name__ == "__main__":
    unittest.main()
