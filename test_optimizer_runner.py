"""Tests for the weekly read-only optimiser runner.

The runner closes a loop that had no scheduler. It must be read-only against the
trading database, must snapshot consistently, and must treat a rejected
challenger as a successful run (only execution failures are non-zero).
"""

import hashlib
import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
RUNNER_PATH = REPO_ROOT / "ops" / "multihedge_optimizer_weekly.py"


def _load_runner():
    spec = importlib.util.spec_from_file_location("mh_weekly_runner", RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _seed_source_db(path: Path, trades: int = 60) -> None:
    with sqlite3.connect(path) as con:
        con.execute(
            "CREATE TABLE mh_trades (id INTEGER PRIMARY KEY, coin TEXT, symbol TEXT, setup TEXT, "
            "side TEXT, open_ts REAL, close_ts REAL, entry_px REAL, exit_px REAL, qty REAL, "
            "realized_pct REAL, realized_usd REAL, exit_reason TEXT)")
        con.execute("CREATE TABLE mh_pxhist (coin TEXT, ts REAL, px REAL)")
        con.execute("CREATE TABLE mh_accounts (trader TEXT, started_usd REAL, equity_usd REAL)")
        con.executemany("INSERT INTO mh_accounts VALUES(?,?,?)",
                        [("scalper", 24.0, 23.98), ("reasoner", 24.0, 23.99),
                         ("whale_trader", 24.0, 24.0), ("memecoin_trader", 24.0, 24.0)])
        con.execute("CREATE TABLE mh_positions (id INTEGER PRIMARY KEY)")
        con.execute("CREATE TABLE mh_reasoner_positions (id INTEGER PRIMARY KEY)")
        con.execute("CREATE TABLE mh_whale_positions (id INTEGER PRIMARY KEY)")
        con.execute("CREATE TABLE mh_memecoin_positions (id INTEGER PRIMARY KEY)")
        con.execute("CREATE TABLE grid_trades (id INTEGER PRIMARY KEY, ts REAL, side TEXT, "
                    "level_px REAL, qty REAL, usd REAL, cycle_id INTEGER, realized_usd REAL)")
        for i in range(trades):
            con.execute(
                "INSERT INTO mh_trades(coin,symbol,setup,side,open_ts,close_ts,entry_px,exit_px,"
                "qty,realized_pct,realized_usd,exit_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                ("MINT1", "TOK", "momentum_breakout", "LONG", float(i), float(i) + 30.0,
                 1.0, 1.01, 10.0, 0.01, 0.1, "take_profit"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class OptimizerRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "source.db"
        self.out = self.root / "results"
        _seed_source_db(self.source)
        self.runner = _load_runner()

    def tearDown(self):
        self.tmp.cleanup()

    def test_snapshot_copies_rows_without_touching_the_source(self):
        before = _sha256(self.source)
        dest = self.root / "snap.db"
        self.runner.snapshot(self.source, dest)
        self.assertEqual(_sha256(self.source), before)
        with sqlite3.connect(dest) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM mh_trades").fetchone()[0], 60)

    def test_run_writes_artifacts_and_leaves_the_source_database_unchanged(self):
        before = _sha256(self.source)
        code = self.runner.main([
            "--db", str(self.source), "--output-dir", str(self.out)])
        self.assertEqual(code, 0)
        self.assertEqual(_sha256(self.source), before, "runner must not write to the trading database")
        runs = [p for p in self.out.iterdir() if p.is_dir()]
        self.assertEqual(len(runs), 1)
        run = runs[0]
        self.assertTrue((run / "report.md").is_file())
        self.assertTrue((run / "baseline.json").is_file())
        self.assertTrue((run / "authoritative-prechange.db").is_file())
        body = (run / "report.md").read_text(encoding="utf-8")
        self.assertIn("VERDICT", body)
        payload = json.loads((run / "baseline.json").read_text(encoding="utf-8"))
        self.assertIn("deployment_state", payload)
        self.assertIn("challenger", payload)

    def test_separate_runs_get_separate_directories(self):
        self.runner.main(["--db", str(self.source), "--output-dir", str(self.out)])
        self.runner.main(["--db", str(self.source), "--output-dir", str(self.out)])
        self.assertGreaterEqual(len([p for p in self.out.iterdir() if p.is_dir()]), 1)


if __name__ == "__main__":
    unittest.main()
