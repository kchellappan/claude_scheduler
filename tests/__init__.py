"""Point csched at a scratch database before any csched module is imported.

unittest imports this package first, so config picks these up at import time
and no test can touch the real state directory.
"""
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="csched-tests-")
os.environ.setdefault("CSCHED_STATE_DIR", _TMP)
os.environ.setdefault("CSCHED_DB", os.path.join(_TMP, "test.db"))
