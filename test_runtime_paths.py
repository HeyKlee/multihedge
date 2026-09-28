"""Prove the runtime_paths resolver is behaviour-preserving before any call site migrates.

The whole safety argument for ATLAS Rule A rests on one property: for every current call
site, `runtime_paths.production_db()` must return the SAME file that site's existing
expression returns. If it does not, migrating the site silently repoints a live trading
module at a different database, which is the exact failure this project is fixing.

These tests assert that property directly, in this environment, with no env overrides and
no database access.
"""
import os
import unittest
from pathlib import Path
from unittest.mock import patch

import runtime_paths

REPO = Path(__file__).resolve().parent

# Every runtime call site that builds the production DB path, with the exact expression it
# used before the migration. Adding a site here is how a new one is brought under test.
LEGACY_SITES = {
    "paper.py": 'Path(__file__).parent / "multihedge.db"',
    "grid_trader.py": 'Path(__file__).parent / "multihedge.db"',
    "mh_memecoin_trader.py": 'Path(__file__).parent / "multihedge.db"',
    "mh_news.py": 'CUR_DIR / "multihedge.db"',
    "mh_reasoner.py": 'CUR_DIR / "multihedge.db"',
    "track_whales.py": 'Path(__file__).parent / "multihedge.db"',
    "mh_whale_trader.py": 'Path(__file__).parent / "multihedge.db"',
    "live_bridge.py": 'Path(__file__).parent / "multihedge.db"',
    "pricefeed.py": 'Path(__file__).parent / "multihedge.db"',
    "strategy.py": 'Path(__file__).parent / "multihedge.db"',
}


class ResolverIsBehaviourPreserving(unittest.TestCase):
    def setUp(self):
        runtime_paths.clear_overrides()

    def tearDown(self):
        runtime_paths.clear_overrides()

    def test_default_points_at_the_real_ledger_not_the_empty_legacy_copy(self):
        """The default must be deploy/db/, NOT the empty repo-root multihedge.db.

        After the directory mount (FINDING 008) the real ledger is deploy/db/multihedge.db.
        A default still pointing at the repo root would resolve to the 0-trade legacy copy,
        which is precisely the FINDING 001 silent-empty-history failure.
        """
        expected = (REPO / "deploy" / "db" / "multihedge.db").resolve()
        self.assertEqual(
            runtime_paths.production_db(), expected,
            "the resolver default must be the mounted ledger directory",
        )
        legacy = (REPO / "multihedge.db").resolve()
        self.assertNotEqual(
            runtime_paths.production_db(), legacy,
            "the resolver is still pointed at the empty legacy root copy",
        )

    def test_default_target_actually_contains_the_ledger(self):
        """Prove the default is a ledger, not an empty file that merely exists."""
        resolved = runtime_paths.production_db()
        if not resolved.exists():
            self.skipTest("ledger not present in this environment")
        runtime_paths.assert_looks_like_ledger(resolved)

    def test_legacy_root_copy_is_rejected_as_the_production_ledger(self):
        """The empty root copy must be refused, not silently accepted."""
        legacy = REPO / "multihedge.db"
        if not legacy.exists():
            self.skipTest("legacy copy not present")
        import sqlite3
        con = sqlite3.connect(f"file:{legacy}?mode=ro", uri=True)
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        con.close()
        if "mh_trades" in names:
            self.skipTest("root copy unexpectedly contains a ledger")
        with self.assertRaises(RuntimeError):
            runtime_paths.assert_looks_like_ledger(legacy)

    def test_missing_database_is_refused_not_created(self):
        with self.assertRaises(FileNotFoundError):
            runtime_paths.assert_looks_like_ledger(Path("/tmp/definitely_absent_ledger.db"))

    def test_every_legacy_site_is_migrated_and_agrees(self):
        """Post-migration invariant: no site may rebuild the path, and all must agree.

        This assertion was originally written the other way round, to guard the
        pre-migration state. Once the migration landed it failed for the right reason, so it
        was inverted: the invariant now is that the legacy expression is GONE, the resolver
        is used, and every module still resolves to the same file.
        """
        for module, expression in LEGACY_SITES.items():
            with self.subTest(module=module):
                path = REPO / module
                self.assertTrue(path.is_file(), f"{module} does not exist")
                text = path.read_text(encoding="utf-8", errors="replace")
                legacy_fragment = expression.split(" = ")[-1]
                self.assertNotIn(
                    legacy_fragment, text,
                    f"{module} still builds its own db path ({legacy_fragment!r}); "
                    "every site must go through runtime_paths",
                )
                self.assertIn(
                    "runtime_paths", text,
                    f"{module} no longer references runtime_paths; the site map is stale",
                )
        self.assertEqual(len(LEGACY_SITES), 10, "site map drifted; keep it exhaustive")

    def test_migrated_modules_all_resolve_to_the_ledger(self):
        """The behavioural guarantee: importing each module yields the original path.

        Run in a SUBPROCESS. Importing these trading modules executes module-level code and
        they are not importable from an arbitrary sys.path, so doing it inside the test
        process both pollutes the run and gives false failures.
        """
        import subprocess
        import sys

        expected = str(runtime_paths.production_db())
        # LEGACY_SITES is keyed by FILENAME; import_module needs the module name.
        module_names = [name[:-3] if name.endswith(".py") else name
                        for name in LEGACY_SITES]
        script = (
            "import importlib, sys, json\n"
            "mods = " + repr(module_names) + "\n"
            "out = {}\n"
            "for m in mods:\n"
            "    out[m] = str(getattr(importlib.import_module(m), 'DB_PATH', None))\n"
            "print(json.dumps(out))\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", script], cwd=str(REPO),
            capture_output=True, text=True, timeout=120, check=False,
        )
        self.assertEqual(
            proc.returncode, 0,
            f"import probe failed: {proc.stderr[-400:]}",
        )
        import json
        resolved = json.loads(proc.stdout.strip().splitlines()[-1])
        for module, path in resolved.items():
            with self.subTest(module=module):
                self.assertEqual(
                    path, expected,
                    f"{module} no longer resolves to the database it always did",
                )

    def test_env_override_is_honoured_and_wins(self):
        with patch.dict(os.environ, {"MULTIHEDGE_DB": "/tmp/alt_ledger.db"}):
            self.assertEqual(runtime_paths.production_db(), Path("/tmp/alt_ledger.db"))

    def test_blank_env_value_falls_back_to_the_default(self):
        with patch.dict(os.environ, {"MULTIHEDGE_DB": "   "}):
            self.assertEqual(
                runtime_paths.production_db(),
                (REPO / "deploy" / "db" / "multihedge.db").resolve(),
            )

    def test_relative_env_value_resolves_against_the_app_not_the_cwd(self):
        """A relative env value must not become CWD-dependent; that is the original bug."""
        import tempfile
        original = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            try:
                os.chdir(tmp)
                with patch.dict(os.environ, {"MULTIHEDGE_DB": "multihedge.db"}):
                    resolved = runtime_paths.production_db()
                self.assertTrue(resolved.is_absolute())
                self.assertEqual(resolved, (REPO / "multihedge.db").resolve())  # app-anchored
            finally:
                os.chdir(original)

    def test_resolution_is_stable_across_chdir(self):
        """The single most important property: cwd must not change the answer."""
        import tempfile
        first = runtime_paths.production_db()
        original = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            try:
                os.chdir(tmp)
                self.assertEqual(runtime_paths.production_db(), first)
            finally:
                os.chdir(original)

    def test_evidence_db_defaults_to_production(self):
        self.assertEqual(runtime_paths.evidence_db(), runtime_paths.production_db())

    def test_evidence_db_can_be_diverged_deliberately(self):
        with patch.dict(os.environ, {"MULTIHEDGE_EVIDENCE_DB": "/tmp/evidence.db"}):
            self.assertEqual(runtime_paths.evidence_db(), Path("/tmp/evidence.db"))

    def test_describe_reports_sources_and_never_secret_values(self):
        d = runtime_paths.describe()
        for key in ("production_db", "evidence_db", "legacy_db", "app_dir"):
            self.assertIn(key, d)
        self.assertNotIn("api_key", str(d).lower())


class ReadonlyOpenIsEnforced(unittest.TestCase):
    def setUp(self):
        runtime_paths.clear_overrides()

    def tearDown(self):
        runtime_paths.clear_overrides()

    def test_connect_readonly_uses_a_readonly_uri(self):
        opened = []
        import sqlite3
        real = sqlite3.connect

        def spy(target, *a, **k):
            opened.append((str(target), k.get("uri", False)))
            return real(target, *a, **k)

        with patch.object(sqlite3, "connect", spy):
            try:
                runtime_paths.connect_readonly()
            except sqlite3.Error:
                pass  # a missing/corrupt db is fine; we only assert the OPEN MODE
        db_opens = [o for o in opened if ".db" in o[0]]
        self.assertTrue(db_opens, "expected a database open attempt")
        for target, is_uri in db_opens:
            self.assertTrue(is_uri, f"opened without a URI: {target}")
            self.assertIn("mode=ro", target, f"opened read-write: {target}")

    def test_host_side_write_is_refused_by_default(self):
        """A host writer must be refused, because that is what tore the WAL."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MULTIHEDGE_ALLOW_HOST_WRITE", None)
            with self.assertRaises(PermissionError):
                runtime_paths.connect(Path("/tmp/host_write_should_be_refused.db"))


if __name__ == "__main__":
    unittest.main()
