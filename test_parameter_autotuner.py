import sqlite3
import tempfile
import unittest
from pathlib import Path

import live_inventory as li
import parameter_autotuner as pa

MINT = "B5WTLaRwaUQpKk7ir1wniNB6m5o8GgMrimhKMYan2R6B"
JUP = "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN"


def cfg():
    return {"coins": [{"symbol": "JUP", "mint": JUP}]}


def excursion(mint, entry, peak, trough, hold, realized, reason="take_profit", open_ts=0.0, close_ts=3600.0):
    return {
        "mint": mint, "ticker": "X", "mode": li.mode_for_mint(mint, cfg()),
        "entry_usd": entry, "peak_usd": peak, "trough_usd": trough,
        "open_ts": open_ts, "close_ts": close_ts, "hold_seconds": hold,
        "realized_pct": realized, "exit_reason": reason,
    }


class RiskParamsOverrideTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "inv.db"

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_override_returns_default(self):
        self.assertEqual(li.risk_params(MINT, cfg(), db_path=self.db)["mode"], "MEME")
        self.assertEqual(li.risk_params(JUP, cfg(), db_path=self.db)["mode"], "SERIOUS")

    def test_override_is_read_and_prioritized(self):
        li.set_risk_params_override(self.db, {
            "take_profit_pct": 0.30, "stop_loss_pct": -0.12, "trail_arm_pct": 0.02,
            "trail_distance_pct": 0.01, "max_hold_seconds": 1200, "mode": "MEME",
        }, source="test", sample_n=40)
        got = li.risk_params(MINT, cfg(), db_path=self.db)
        self.assertEqual(got["take_profit_pct"], 0.30)
        self.assertEqual(got["stop_loss_pct"], -0.12)
        self.assertEqual(got["max_hold_seconds"], 1200)
        # SERIOUS unaffected by the MEME override.
        self.assertEqual(li.risk_params(JUP, cfg(), db_path=self.db)["take_profit_pct"], 0.05)

    def test_check_constraint_rejects_degenerate_params(self):
        with self.assertRaises(sqlite3.IntegrityError):
            li.set_risk_params_override(self.db, {
                "take_profit_pct": 5.0, "stop_loss_pct": -0.10, "trail_arm_pct": 0.02,
                "trail_distance_pct": 0.01, "max_hold_seconds": 900, "mode": "MEME",
            }, source="test", sample_n=40)


class AutotunerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "auto.db"
        con = sqlite3.connect(self.db)
        con.execute(
            "CREATE TABLE mh_scalp_excursions (mint TEXT,ticker TEXT,mode TEXT,"
            "entry_usd REAL,peak_usd REAL,trough_usd REAL,open_ts REAL,close_ts REAL,"
            "hold_seconds REAL,realized_pct REAL,exit_reason TEXT)"
        )
        con.commit()
        con.close()

    def tearDown(self):
        self.tmp.cleanup()

    def _seed(self, rows):
        con = sqlite3.connect(self.db)
        con.executemany(
            "INSERT INTO mh_scalp_excursions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            [tuple(r.values()) for r in rows])
        con.commit()
        con.close()

    def test_insufficient_history_never_tunes(self):
        self._seed([excursion(MINT, 0.001, 0.0013, 0.00095, 600, 0.05)
                    for _ in range(10)])
        rep = pa.maybe_tune(self.db, cfg())
        self.assertEqual(rep["state"], "NO_CHANGE")
        self.assertIn("INSUFFICIENT_HISTORY", rep["evaluation"]["MEME"]["state"])
        self.assertIsNone(li._risk_params_override(self.db, "MEME"))

    def test_wins_condition_is_conservative_both_hit(self):
        # Both TP and SL pierced -> stop counts first (pessimistic).
        row = excursion(MINT, 0.001, 0.0015, 0.0008, 600, 0.0)
        p = {"take_profit_pct": 0.20, "stop_loss_pct": -0.10,
             "max_hold_seconds": 900}
        # trough -20% <= -10% -> counted as the SL loss, not the 50% TP.
        self.assertLess(pa._expectancy([row], p), 0)

    def test_empty_returns_incumbent_no_write(self):
        rep = pa.maybe_tune(self.db, cfg())
        self.assertEqual(rep["state"], "NO_CHANGE")
        self.assertIsNone(li._risk_params_override(self.db, "MEME"))


if __name__ == "__main__":
    unittest.main()
