"""Regression tests for the autonomous cycle launcher step retry.

The cron fires every minute and one-shot containers read/write the same
multihedge.db as the persistent multihedge daemon. In delete-journal mode a
writer commit briefly holds an EXCLUSIVE lock, so a step can legitimately die
on "database is locked" and abort the whole cycle even though the DB is fine.
run() must retry ONLY that transient condition with backoff, and must surface
any real failure immediately rather than masking it.
"""

import subprocess
import unittest
from unittest import mock

from ops.multihedge_autonomous_live import LOCKED_MARKERS, run


class _Result:
    def __init__(self, returncode, stderr=""):
        self.returncode = returncode
        self.stdout = "{}"
        self.stderr = stderr


class RunRetryTests(unittest.TestCase):
    def test_success_returns_first_result(self):
        result = _Result(0)
        with mock.patch("ops.multihedge_autonomous_live.subprocess.run",
                        return_value=result) as m:
            out = run(["prog"])
        self.assertIs(out, result)
        self.assertEqual(m.call_count, 1)

    def test_retries_only_locked_marker_with_backoff(self):
        locked = _Result(1, stderr="sqlite3.OperationalError: database is locked")
        ok = _Result(0)
        with mock.patch("ops.multihedge_autonomous_live.subprocess.run",
                        side_effect=[locked, ok]) as m, \
             mock.patch("ops.multihedge_autonomous_live.time.sleep") as sleep:
            out = run(["prog"])
        self.assertIs(out, ok)
        self.assertEqual(m.call_count, 2)
        self.assertEqual(sleep.call_count, 1)

    def test_real_failure_is_not_retried(self):
        failed = _Result(1, stderr="ModuleNotFoundError: No module named 'x'")
        with mock.patch("ops.multihedge_autonomous_live.subprocess.run",
                        return_value=failed) as m, \
             mock.patch("ops.multihedge_autonomous_live.time.sleep") as sleep:
            out = run(["prog"])
        self.assertIs(out, failed)
        self.assertEqual(m.call_count, 1)
        sleep.assert_not_called()

    def test_gives_up_after_attempts(self):
        locked = _Result(1, stderr="OperationalError: database is busy")
        with mock.patch("ops.multihedge_autonomous_live.subprocess.run",
                        return_value=locked) as m, \
             mock.patch("ops.multihedge_autonomous_live.time.sleep") as sleep:
            out = run(["prog"], attempts=3, delay=2)
        self.assertIs(out, locked)
        self.assertEqual(m.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_empty_stderr_is_not_retried(self):
        failed = _Result(1, stderr="")
        with mock.patch("ops.multihedge_autonomous_live.subprocess.run",
                        return_value=failed) as m, \
             mock.patch("ops.multihedge_autonomous_live.time.sleep") as sleep:
            out = run(["prog"])
        self.assertIs(out, failed)
        self.assertEqual(m.call_count, 1)
        sleep.assert_not_called()


class MarkerTest(unittest.TestCase):
    def test_markers_cover_sqlite_phrases(self):
        self.assertIn("database is locked", LOCKED_MARKERS)
        self.assertIn("database is busy", LOCKED_MARKERS)


if __name__ == "__main__":
    unittest.main()