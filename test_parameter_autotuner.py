import json
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
        # Live/default reads ignore an unpromoted paper candidate.
        self.assertEqual(li.risk_params(MINT, cfg(), db_path=self.db)["take_profit_pct"], 0.20)
        got = li.risk_params(MINT, cfg(), db_path=self.db, allow_tuned=True)
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
        con.execute(
            "CREATE TABLE mh_scalp_price_samples (mint TEXT,opened_ts REAL,"
            "sample_ts REAL,price_usd REAL,PRIMARY KEY(mint,opened_ts,sample_ts))"
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
        # Chronological replay sees the stop before the later peak.
        row = excursion(MINT, 0.001, 0.0015, 0.0008, 600, 0.0)
        row["samples"] = [
            {"sample_ts": 0.0, "price_usd": 0.001},
            {"sample_ts": 300.0, "price_usd": 0.0008},
            {"sample_ts": 600.0, "price_usd": 0.0015},
        ]
        p = {"take_profit_pct": 0.20, "stop_loss_pct": -0.10,
             "max_hold_seconds": 900}
        # Stop is observed first, so the later 50% peak cannot be claimed.
        self.assertLess(pa._expectancy([row], p), 0)

    def test_age_without_take_profit_crossing_is_censored(self):
        row = excursion(MINT, 1.0, 1.10, 0.95, 900, 0.10, close_ts=900)
        row["samples"] = [
            {"sample_ts": 0.0, "price_usd": 1.0},
            {"sample_ts": 600.0, "price_usd": 0.95},
            {"sample_ts": 900.0, "price_usd": 1.10},
        ]
        candidate = {"take_profit_pct": 0.20, "stop_loss_pct": -0.10,
                     "max_hold_seconds": 600}
        self.assertIsNone(pa._expectancy([row], candidate))

    def test_expectancy_and_wins_are_net_of_round_trip_cost(self):
        row = excursion(MINT, 1.0, 1.011, 1.0, 900, 0.011, close_ts=900)
        row["samples"] = [
            {"sample_ts": 0.0, "price_usd": 1.0},
            {"sample_ts": 900.0, "price_usd": 1.011},
        ]
        params = {"take_profit_pct": .01, "stop_loss_pct": -.10,
                  "max_hold_seconds": 900}
        self.assertAlmostEqual(
            pa._expectancy([row], params, round_trip_cost_pct=.008), .002)
        self.assertEqual(pa._win_rate([row], params, round_trip_cost_pct=.011), 0.0)

    def test_legacy_extremes_without_price_paths_never_tune(self):
        self._seed([excursion(MINT, 0.001, 0.0013, 0.00095, 900, 0.05,
                              open_ts=float(i), close_ts=float(i + 900))
                    for i in range(30)])
        rep = pa.maybe_tune(self.db, cfg())
        self.assertEqual(rep["evaluation"]["MEME"]["state"], "INSUFFICIENT_PRICE_PATHS")
        self.assertIsNone(li._risk_params_override(self.db, "MEME"))

    def test_empty_returns_incumbent_no_write(self):
        rep = pa.maybe_tune(self.db, cfg())
        self.assertEqual(rep["state"], "NO_CHANGE")
        self.assertIsNone(li._risk_params_override(self.db, "MEME"))

    def test_every_pass_is_logged_so_a_silent_no_op_is_visible(self):
        pa.maybe_tune(self.db, cfg(), now=1234.0)
        with sqlite3.connect(self.db) as con:
            rows = con.execute(
                "SELECT ts,coin,setup,event,detail FROM mh_optimization_log").fetchall()
        self.assertEqual(len(rows), 1)
        ts, coin, setup, event, detail = rows[0]
        self.assertEqual((ts, coin, setup, event), (1234.0, "*", "autotuner", "autotune_evaluation"))
        payload = json.loads(detail)
        self.assertEqual(payload["state"], "NO_CHANGE")
        self.assertIn("MEME", payload["evaluation"])
        self.assertIn("SERIOUS", payload["evaluation"])
        self.assertAlmostEqual(payload["round_trip_cost_pct"], 0.008, places=9)

    def test_declined_pass_reports_sample_progress_toward_the_gate(self):
        # The gate reason must show how close the sample is, not just "no".
        self._seed([excursion(MINT, 0.001, 0.0013, 0.00095, 900, 0.05,
                              open_ts=float(i), close_ts=float(i + 900))
                    for i in range(5)])
        rep = pa.maybe_tune(self.db, cfg())
        meme = rep["evaluation"]["MEME"]
        self.assertEqual(meme["state"], "INSUFFICIENT_HISTORY")
        self.assertEqual(meme["closed"], 5)
        self.assertEqual(meme["path_ready"], 0)
        self.assertEqual(meme["required"], pa.MIN_SAMPLE_CLOSED)


if __name__ == "__main__":
    unittest.main()
