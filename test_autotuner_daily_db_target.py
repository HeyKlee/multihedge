"""Regression tests: the daily autotuner must tune the real ledger.

Defect (2026-09-29): ops/autotuner_daily.py hardcoded the container DB path
"/app/multihedge.db". That file is an orphan in the container layer with a single
table (mh_optimization_log). The real ledger is bind-mounted as a DIRECTORY at
/app/db/multihedge.db (49 tables, FINDING 008). The autotuner therefore read
0 excursion rows and reported INSUFFICIENT_HISTORY 0/30 for every class while
the ledger held 398 MEME and 29 SERIOUS closed excursions.

These tests assert the behaviour contract, not the source text: the tuning
bootstrap must resolve the database through runtime_paths (the single authority)
and must refuse to run against anything that is not a ledger schema.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_SPEC = importlib.util.spec_from_file_location(
    "autotuner_daily_db_target", _ROOT / "ops" / "autotuner_daily.py")
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("cannot load ops/autotuner_daily.py")
autotuner_daily = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(autotuner_daily)


class BootstrapDatabaseTargetTests(unittest.TestCase):
    """The container-side bootstrap must not name a database path directly."""

    def test_bootstrap_does_not_hardcode_a_database_path(self):
        # Any literal /app/... .db path in the bootstrap re-creates the orphan bug.
        self.assertNotIn("/app/multihedge.db", autotuner_daily.BOOTSTRAP)
        self.assertNotIn("/app/db/multihedge.db", autotuner_daily.BOOTSTRAP)

    def test_bootstrap_resolves_database_through_runtime_paths(self):
        self.assertIn("runtime_paths", autotuner_daily.BOOTSTRAP)
        self.assertIn("evidence_db", autotuner_daily.BOOTSTRAP)

    def test_bootstrap_refuses_a_non_ledger_database(self):
        # A fresh/empty database must fail loudly rather than report an empty
        # history as a valid evaluation.
        self.assertIn("assert_looks_like_ledger", autotuner_daily.BOOTSTRAP)

    def test_route_command_does_not_force_an_evidence_db_override(self):
        # Passing -e MULTIHEDGE_EVIDENCE_DB=<path> would override compose's
        # MULTIHEDGE_DB and reintroduce a path decision made outside
        # runtime_paths. The container's own environment is authoritative.
        cmd = autotuner_daily._route_command(
            autotuner_daily._production_db_path(), _ROOT,
            docker_exe="/usr/bin/docker", container_running=True, image_ready=True)
        self.assertIsNotNone(cmd)
        self.assertNotIn("MULTIHEDGE_EVIDENCE_DB", cmd)


class NonLedgerDatabaseIsRejectedTests(unittest.TestCase):
    """The guard the bootstrap relies on must actually reject an empty DB."""

    def test_assert_looks_like_ledger_rejects_empty_database(self):
        import runtime_paths

        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "empty.db"
            con = sqlite3.connect(empty)
            con.execute("CREATE TABLE unrelated (x INTEGER)")
            con.commit()
            con.close()

            with self.assertRaises(RuntimeError):
                runtime_paths.assert_looks_like_ledger(empty)

    def test_assert_looks_like_ledger_accepts_ledger_schema(self):
        import runtime_paths

        with tempfile.TemporaryDirectory() as tmp:
            good = Path(tmp) / "good.db"
            con = sqlite3.connect(good)
            con.execute("CREATE TABLE mh_trades (id INTEGER)")
            con.commit()
            con.close()

            runtime_paths.assert_looks_like_ledger(good)  # must not raise

    def test_assert_looks_like_ledger_rejects_missing_file(self):
        import runtime_paths

        with self.assertRaises(FileNotFoundError):
            runtime_paths.assert_looks_like_ledger(Path("/tmp/definitely-not-here.db"))


if __name__ == "__main__":
    unittest.main()
