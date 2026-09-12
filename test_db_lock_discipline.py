"""Connection discipline for the shared SQLite database.

On 2026-09-12 the container wedged for minutes: a daemon held SQLite's PENDING
byte while its busy handler spun, so nothing in the stack could read or write.
`/proc/locks` identified the holder as the reasoner loop.

The load-bearing facts, and the ones guarded here:

  1. A blocked writer holds the PENDING byte, which blocks new readers, and it
     waits for existing readers to clear before it can take EXCLUSIVE. So the
     busy wait must be BOUNDED: an unbounded or long wait turns one contended
     write into a system-wide stall.
  2. The schema DDL is NOT the cause, and it is worth recording why, because the
     opposite is easy to assume: `CREATE TABLE IF NOT EXISTS` on a table that
     already exists performs no write and takes no lock. It only writes when the
     table is actually missing.
"""

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

import live_inventory as li
import mh_reasoner


class DdlIsNotAWriteWhenTablesExistTests(unittest.TestCase):
    """Records the finding that ruled out the first hypothesis."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "inv.db"
        with li._connect(self.db) as con:
            con.execute("SELECT 1").fetchone()

    def tearDown(self):
        try:
            os.chmod(self.db, 0o644)
        except OSError:
            pass
        self.tmp.cleanup()

    def test_if_not_exists_on_an_existing_table_takes_no_write(self):
        os.chmod(self.db, 0o444)
        con = sqlite3.connect(self.db)
        try:
            # Every statement _connect issues, against a read-only file. If any of
            # these wrote, this would raise "attempt to write a readonly database".
            for statement in (
                "CREATE TABLE IF NOT EXISTS mh_risk_params (mode TEXT PRIMARY KEY,"
                "take_profit_pct REAL, stop_loss_pct REAL, trail_arm_pct REAL,"
                "trail_distance_pct REAL, max_hold_seconds REAL, source TEXT,"
                "sample_n INTEGER, applied_ts REAL)",
                "CREATE TABLE IF NOT EXISTS mh_live_inventory (mint TEXT PRIMARY KEY)",
                "CREATE TABLE IF NOT EXISTS mh_coin_risk_params (mint TEXT PRIMARY KEY)",
            ):
                con.execute(statement)
        finally:
            con.close()

    def test_the_same_statement_does_write_when_the_table_is_missing(self):
        # The contrast that makes the test above meaningful rather than vacuous.
        os.chmod(self.db, 0o444)
        con = sqlite3.connect(self.db)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                con.execute(
                    "CREATE TABLE IF NOT EXISTS mh_definitely_absent (id INTEGER PRIMARY KEY)")
        finally:
            con.close()


class BoundedWaitTests(unittest.TestCase):
    def test_busy_wait_is_bounded_in_both_connection_helpers(self):
        for module in (li, mh_reasoner):
            value = module.BUSY_TIMEOUT_SECONDS
            self.assertGreater(value, 0, module.__name__)
            self.assertLessEqual(value, 15.0, module.__name__)

    def test_reasoner_connect_uses_the_bounded_timeout(self):
        tmp = tempfile.TemporaryDirectory()
        original = mh_reasoner.DB_PATH
        mh_reasoner.DB_PATH = Path(tmp.name) / "reasoner.db"
        try:
            con = mh_reasoner._connect()
            self.assertEqual(con.execute("SELECT 1").fetchone()[0], 1)
            self.assertEqual(con.execute("PRAGMA busy_timeout").fetchone()[0],
                             int(mh_reasoner.BUSY_TIMEOUT_SECONDS * 1000))
            con.close()
        finally:
            mh_reasoner.DB_PATH = original
            tmp.cleanup()

    def test_live_inventory_connect_uses_the_bounded_timeout(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            db = Path(tmp.name) / "inv.db"
            with li._connect(db) as con:
                self.assertEqual(con.execute("PRAGMA busy_timeout").fetchone()[0],
                                 int(li.BUSY_TIMEOUT_SECONDS * 1000))
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
