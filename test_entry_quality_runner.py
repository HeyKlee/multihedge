"""Tests for the read-only XORA-SURVIVAL entry-quality evidence report."""

import hashlib
import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RUNNER_PATH = ROOT / "ops" / "xora_entry_quality_daily.py"


def load_runner():
    spec = importlib.util.spec_from_file_location("entry_quality_runner", RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def seed(path: Path, observations: int = 100, outcomes: int = 30) -> None:
    with sqlite3.connect(path) as con:
        con.execute("CREATE TABLE mh_shadow_entry_observations ("
                    "observed_ts REAL NOT NULL,mint TEXT NOT NULL,latest_usd REAL NOT NULL,"
                    "return_5m_pct REAL NOT NULL,return_1h_pct REAL NOT NULL,"
                    "buy_volume_5m_usd REAL NOT NULL,sell_volume_5m_usd REAL NOT NULL,"
                    "entry_signal BOOLEAN NOT NULL,entry_opened BOOLEAN NOT NULL,"
                    "PRIMARY KEY(observed_ts,mint))")
        con.execute("CREATE TABLE mh_shadow_entry_outcomes ("
                    "observed_ts REAL NOT NULL,mint TEXT NOT NULL,opened_ts REAL NOT NULL,"
                    "close_ts REAL NOT NULL,exit_reason TEXT NOT NULL,realized_pct REAL NOT NULL,"
                    "realized_usd REAL NOT NULL,PRIMARY KEY(observed_ts,mint),"
                    "UNIQUE(mint,opened_ts))")
        for i in range(observations):
            con.execute("INSERT INTO mh_shadow_entry_observations VALUES(?,?,?,?,?,?,?,?,?)",
                        (float(i), f"mint-{i}", 1.0, 1.0, 2.0, 10.0, 5.0, 1, int(i < outcomes)))
        for i in range(outcomes):
            pct = .05 if i % 2 else -.03
            con.execute("INSERT INTO mh_shadow_entry_outcomes VALUES(?,?,?,?,?,?,?)",
                        (float(i), f"mint-{i}", float(i), float(i + 60),
                         "take_profit" if pct > 0 else "stop_loss", pct, pct))


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class EntryQualityRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "source.db"
        self.out = self.root / "reports"
        self.runner = load_runner()

    def tearDown(self):
        self.tmp.cleanup()

    def test_report_is_read_only_and_blocks_proposals_below_thresholds(self):
        seed(self.db, observations=99, outcomes=29)
        before = sha(self.db)
        self.assertEqual(self.runner.main(["--db", str(self.db), "--output-dir", str(self.out)]), 0)
        self.assertEqual(sha(self.db), before)
        report = json.loads((self.out / "entry_quality.json").read_text())
        self.assertFalse(report["proposal_generation_allowed"])
        self.assertIn("observations_below_100", report["blocking_reasons"])
        self.assertIn("outcomes_below_30", report["blocking_reasons"])

    def test_report_requires_valid_linked_rows_and_never_generates_a_filter(self):
        seed(self.db)
        self.assertEqual(self.runner.main(["--db", str(self.db), "--output-dir", str(self.out)]), 0)
        report = json.loads((self.out / "entry_quality.json").read_text())
        self.assertEqual(report["valid_observations"], 100)
        self.assertEqual(report["valid_outcomes"], 30)
        self.assertEqual(report["orphan_outcomes"], 0)
        self.assertFalse(report["proposal_generation_allowed"])
        self.assertIn("rejected_candidates_unlabelled", report["blocking_reasons"])
        self.assertTrue((self.out / "report.md").is_file())


if __name__ == "__main__":
    unittest.main()
