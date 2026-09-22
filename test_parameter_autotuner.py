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
        self.assertEqual(li.risk_params(MINT, cfg(), db_path=self.db)["take_profit_pct"], 0.015)
        got = li.risk_params(MINT, cfg(), db_path=self.db, allow_tuned=True)
        self.assertEqual(got["take_profit_pct"], 0.30)
        self.assertEqual(got["stop_loss_pct"], -0.12)
        self.assertEqual(got["max_hold_seconds"], 1200)
        # SERIOUS unaffected by the MEME override.
        self.assertEqual(li.risk_params(JUP, cfg(), db_path=self.db)["take_profit_pct"], 0.010)

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

    def _seed_with_paths(self, n=4, entry=1.0, prices=(1.0, 1.085, 1.05, 1.02)):
        """Excursions that carry replayable price paths, in chronological order."""
        con = sqlite3.connect(self.db)
        for i in range(n):
            opened = float(i * 10_000)
            con.execute(
                "INSERT INTO mh_scalp_excursions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (MINT, "X", li.mode_for_mint(MINT, cfg()), entry, max(prices), min(prices),
                 opened, opened + 180.0, 180.0, prices[-1] / entry - 1, "trail_stop"))
            for j, price in enumerate(prices):
                con.execute("INSERT INTO mh_scalp_price_samples VALUES(?,?,?,?)",
                            (MINT, opened, opened + j * 60.0, price))
        con.commit()
        con.close()

    def _seed_mixed_paths(self, n=30):
        """Seed MEME-mode excursions with a mix of path types that produce
        a >2pp improvement margin between the best candidate (TP=0.03,
        SL=-0.02) and the current MEME defaults (TP=0.015, SL=-0.015).

        22 `good` excursions (17 train + 5 holdout):
          1.0 -> 1.03  — both incumbent and best exit via TP.
          Gap per excursion = 0.022 - 0.007 = 0.015 = 1.5pp.

        8 `bad` excursions (5 train + 3 holdout):
          1.0 -> 0.985 -> 1.03  — incumbent SL=-0.015 fires at -0.023;
          best (SL=-0.02) survives the dip and reaches TP=0.03 for 0.022.
          Gap per excursion = 0.022 - (-0.023) = 0.045 = 4.5pp.

        Train average gap: (17*0.015 + 5*0.045) / 22 = 0.0218 > 0.02
        Holdout average gap: (5*0.015 + 3*0.045) / 8 = 0.02625 > 0.02
        Win rate: 100% (both paths end positive for the best candidate).
        """
        con = sqlite3.connect(self.db)
        entry = 1.0
        for i in range(n):
            opened = float(i * 10_000)
            # Train: indices 0-21 (17 good + 5 bad).
            # Holdout: indices 22-29 (5 good + 3 bad).
            if i < 17 or (22 <= i < 27):
                prices = (entry, 1.03)
                close_ts = opened + 120.0
            else:
                prices = (entry, 0.985, 1.03)
                close_ts = opened + 200.0
            con.execute(
                "INSERT INTO mh_scalp_excursions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (MINT, "X", li.mode_for_mint(MINT, cfg()),
                 entry, max(prices), min(prices),
                 opened, close_ts, close_ts - opened,
                 prices[-1] / entry - 1, "take_profit"))
            for j, price in enumerate(prices):
                con.execute("INSERT INTO mh_scalp_price_samples VALUES(?,?,?,?)",
                            (MINT, opened, opened + j * 60.0, price))
        con.commit()
        con.close()

    def test_trailing_is_inside_the_search_space(self):
        # Trail exits dominate the MEME class, so a search that freezes the arm
        # and distance cannot fix the thing that actually costs money.
        candidates = list(pa._candidates("MEME"))
        arms = {c["trail_arm_pct"] for c in candidates}
        dists = {c["trail_distance_pct"] for c in candidates}
        self.assertGreater(len(arms), 1)
        self.assertGreater(len(dists), 1)
        # The incumbent's own pair stays reachable so the comparison is fair.
        self.assertIn(0.02, arms)  # incumbent MEME trail_arm = 0.02
        self.assertIn(0.01, dists)  # incumbent MEME trail_distance = 0.01
        for c in candidates:
            self.assertLess(c["trail_distance_pct"], c["trail_arm_pct"])

    def test_meme_candidate_grid_includes_defensive_bearish_profile(self):
        candidates = list(pa._candidates("MEME"))
        self.assertIn({
            "take_profit_pct": 0.02,
            "stop_loss_pct": -0.015,
            "trail_arm_pct": 0.02,
            "trail_distance_pct": 0.01,
            "max_hold_seconds": 1800,
            "mode": "MEME",
        }, candidates)
        self.assertNotIn(0.02, {c["take_profit_pct"] for c in pa._candidates("SERIOUS")})

    def test_tighter_trail_can_be_adopted(self):
        # Under current MEME defaults (TP=0.015, SL=-0.015, trail_arm=0.02,
        # trail_distance=0.01, max_hold=1800), the best candidate
        # (TP=0.03, SL=-0.02, trail_distance=0.008) achieves a >2pp
        # improvement by surviving a -1.5% dip that stops out the incumbent,
        # then reaching the 3% TP.  The mixed-path seed produces 30
        # excursions where the train average gap is 0.0218 and holdout
        # average gap is 0.0263, both above IMPROVEMENT_MARGIN=0.02.
        self._seed_mixed_paths(n=pa.MIN_SAMPLE_CLOSED)
        import dynamic_shadow_scalper as ds
        with ds._connect(self.db) as con:
            con.executemany("INSERT INTO mh_scalp_policy_evidence VALUES(?,?,?,0)",
                            [(MINT, float(i * 10_000), pa.policy_signature(li._default_params("MEME")))
                             for i in range(pa.MIN_SAMPLE_CLOSED)])
        rep = pa.maybe_tune(self.db, cfg())
        self.assertEqual(rep["state"], "TUNED", rep)
        got = li._risk_params_override(self.db, "MEME")
        self.assertIsNotNone(got)
        self.assertEqual(got["trail_distance_pct"], 0.008)
        self.assertEqual(rep["tuned"]["MEME"]["trail_distance_pct"], 0.008)

    def test_sub_margin_candidate_never_tunes(self):
        # Prove that a genuinely sub-margin improvement still yields
        # NOT_IMPROVED.  The original stale fixture path (1.0 -> 1.085 ->
        # 1.05 -> 1.02) produces a max gap of 0.015 between best candidate
        # (TP=0.03, return 0.022) and incumbent (return 0.007), which is
        # below IMPROVEMENT_MARGIN=0.02.  The autotuner must correctly
        # decline adoption.
        self._seed_with_paths(n=pa.MIN_SAMPLE_CLOSED)
        import dynamic_shadow_scalper as ds
        with ds._connect(self.db) as con:
            con.executemany("INSERT INTO mh_scalp_policy_evidence VALUES(?,?,?,0)",
                            [(MINT, float(i * 10_000), pa.policy_signature(li._default_params("MEME")))
                             for i in range(pa.MIN_SAMPLE_CLOSED)])
        rep = pa.maybe_tune(self.db, cfg())
        self.assertEqual(rep["state"], "NO_CHANGE", rep)
        meme = rep["evaluation"]["MEME"]
        self.assertEqual(meme["state"], "NOT_IMPROVED")
        self.assertIsNone(li._risk_params_override(self.db, "MEME"))
        self.assertLess(
            meme["candidate_holdout_expectancy"] - meme["incumbent_holdout_expectancy"],
            0.02,
            "gap must stay below IMPROVEMENT_MARGIN for the stale path")

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
