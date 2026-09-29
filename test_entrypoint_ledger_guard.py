#!/usr/bin/env python3
"""The container entrypoint must refuse to start on a missing or empty ledger.

WHY THIS EXISTS
---------------
A 0-byte SQLite file is a valid, openable database with zero tables. The
container carried exactly that at /app/multihedge.db, a retired path, and any
process that opened it would report "no trades", "no evidence", "no history"
as if those were measurements. A read-only monitor then publishes an empty
history as fact. That is FINDING 001 and FINDING 008 one layer up.

The guard lives in deploy/entrypoint.sh because it has to run before
supervisord starts the daemons. These tests drive that script against
synthetic ledgers so the failure modes stay fixed.

A note on method: probing for a missing ledger with sqlite3.connect() CREATES
a 0-byte file, so the guard uses stat and a read-only connect only. The tests
assert that too, because a guard that manufactures the condition it exists to
prevent is worse than no guard.
"""

import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENTRYPOINT = ROOT / "deploy" / "entrypoint.sh"
REAL_LEDGER_CANDIDATES = (
    ROOT / "deploy" / "db" / "multihedge.db",
    ROOT / "multihedge.db",
)


def _real_ledger() -> Path:
    for c in REAL_LEDGER_CANDIDATES:
        if c.is_file() and c.stat().st_size > 1024:
            return c
    raise unittest.SkipTest("no real ledger available to copy")


def _script_for(tmp: Path, ledger_dir: Path, orphan: Path) -> Path:
    """Render the entrypoint with its container paths pointed at `tmp`.

    Substituted on a copy so the real deploy/entrypoint.sh is never edited by a
    test, and so the test exercises the shipped script's logic rather than a
    reimplementation of it.
    """
    src = ENTRYPOINT.read_text(encoding="utf-8")
    src = src.replace('LEDGER_DIR="/app/db"', f'LEDGER_DIR="{ledger_dir}"')
    src = src.replace('ORPHAN="/app/multihedge.db"', f'ORPHAN="{orphan}"')
    src = src.replace('path = "/app/db/multihedge.db"',
                      f'path = "{ledger_dir}/multihedge.db"')
    out = tmp / "entrypoint_under_test.sh"
    out.write_text(src, encoding="utf-8")
    return out


class EntrypointLedgerGuardTests(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mh-entrypoint-"))
        self.db = self.tmp / "db"
        self.db.mkdir()
        self.orphan = self.tmp / "orphan.db"
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _run(self):
        script = _script_for(self.tmp, self.db, self.orphan)
        return subprocess.run(
            ["bash", str(script), "echo", "EXEC_REACHED"],
            capture_output=True, text=True, timeout=60,
        )

    # --- the happy path --------------------------------------------------
    def test_real_ledger_starts(self):
        shutil.copy(_real_ledger(), self.db / "multihedge.db")
        r = self._run()
        self.assertIn("EXEC_REACHED", r.stdout,
                      f"a real ledger must start: {r.stderr}")
        self.assertNotIn("FATAL", r.stderr)

    # --- the failure modes that must fail closed ------------------------
    def test_zero_byte_ledger_refuses_to_start(self):
        (self.db / "multihedge.db").write_bytes(b"")
        r = self._run()
        self.assertNotIn("EXEC_REACHED", r.stdout)
        self.assertIn("FATAL", r.stderr)
        self.assertIn("0 bytes", r.stderr)

    def test_missing_ledger_directory_refuses_to_start(self):
        shutil.rmtree(self.db)
        r = self._run()
        self.assertNotIn("EXEC_REACHED", r.stdout)
        self.assertIn("not present", r.stderr)

    def test_missing_ledger_file_refuses_to_start(self):
        r = self._run()
        self.assertNotIn("EXEC_REACHED", r.stdout)
        self.assertIn("missing", r.stderr)

    def test_nonempty_non_database_refuses_to_start(self):
        # A file can be large and still be unusable. Size alone is not proof.
        (self.db / "multihedge.db").write_bytes(os.urandom(5000))
        r = self._run()
        self.assertNotIn("EXEC_REACHED", r.stdout)
        self.assertIn("FATAL", r.stderr)

    def test_small_but_schema_bearing_ledger_is_accepted(self):
        """Documented limit, pinned deliberately.

        A SQLite file containing one table is about 8KB, so a valid schema can
        never fall under the 1KB size gate. Anything larger than 1KB is
        therefore accepted on size, and correctness beyond that is carried by
        the read-only open plus the table count, both of which must pass.
        An earlier version of this test asserted that a small valid database
        was refused; that was wrong, and it would have pushed the size gate up
        to a number that could mask a truncated real ledger.
        """
        con = sqlite3.connect(self.db / "multihedge.db")
        con.execute("CREATE TABLE t(x)")
        con.commit()
        con.close()
        size = (self.db / "multihedge.db").stat().st_size
        self.assertGreater(size, 1024,
                           "premise: a table-bearing sqlite file exceeds 1KB")
        r = self._run()
        self.assertIn("EXEC_REACHED", r.stdout)

    # --- the orphan path -------------------------------------------------
    def test_orphan_is_removed_and_start_continues(self):
        """A good ledger plus a 0-byte orphan: warn, clean up, still start.

        The orphan is not itself fatal because the real ledger has already been
        proven good. Failing here would take the system down for a file nothing
        reads, which is its own kind of harm.
        """
        shutil.copy(_real_ledger(), self.db / "multihedge.db")
        self.orphan.write_bytes(b"")
        r = self._run()
        self.assertIn("EXEC_REACHED", r.stdout)
        self.assertIn("WARNING", r.stderr)
        self.assertFalse(self.orphan.exists(),
                         "the orphan must be removed, not just reported")

    # --- the guard must not manufacture the fault it prevents ------------
    def test_guard_does_not_create_the_ledger_it_checks_for(self):
        """sqlite3.connect() on a missing path CREATES a 0-byte file.

        A guard that opened the missing ledger with a bare connect would create
        the very condition it exists to detect, then report the system healthy
        on the next boot. Assert the shipped script never does this.
        """
        src = ENTRYPOINT.read_text(encoding="utf-8")
        bare = []
        for line in src.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue  # a comment mentioning connect() is not a connect
            if "sqlite3.connect(" not in stripped:
                continue
            if "mode=ro" in stripped:
                continue
            bare.append(stripped)
        self.assertEqual(
            bare, [],
            "the entrypoint opens a database without mode=ro, which would "
            "create a 0-byte file at the path it is meant to be validating")

    def test_guard_checks_size_before_opening(self):
        """Order matters: a size check first means no connect can create it."""
        src = ENTRYPOINT.read_text(encoding="utf-8")
        size_at = src.find("stat -c%s")
        connect_at = src.find("mode=ro")
        self.assertGreater(size_at, 0, "no size check found")
        self.assertGreater(connect_at, 0, "no read-only open found")
        self.assertLess(
            size_at, connect_at,
            "the size check must run before any connect, otherwise the "
            "connect can create the file the size check was meant to catch")


if __name__ == "__main__":
    unittest.main()
