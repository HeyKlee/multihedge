"""Tests for the per-coin shadow reviewer.

The contract under test: it reviews each coin separately, diagnoses from recorded
facts, and may only apply a bounded per-coin change when a deterministic replay
says the candidate beats the incumbent out of sample. It must never widen live
risk, and it must never let one coin's override leak into another coin.
"""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import live_inventory as li
import mh_coin_review as cr

COIN_A = "5dvXTZ5qwgafnHtwu3Ls3QrWx1U4LQsFeCuJgkk4QEC6"   # memecoin class
COIN_B = "Dz9mQ9NzkBcCsuGPFJ3r1bS4wgqKMHBPiVuniW8Mbonk"
COIN_C = "9BB6NFEcjBCtnNLFko2FqVQBq8HHM13kCyYcdQbgpump"


def cfg():
    return {"coins": [{"symbol": "JUP", "mint": "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN"}],
            "paper": {"quote_bps": 40}}


class CoinReviewBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "review.db"
        with sqlite3.connect(self.db) as con:
            con.execute(
                "CREATE TABLE mh_trades (id INTEGER PRIMARY KEY, coin TEXT, symbol TEXT, setup TEXT, "
                "side TEXT, open_ts REAL, close_ts REAL, entry_px REAL, exit_px REAL, qty REAL, "
                "realized_pct REAL, realized_usd REAL, exit_reason TEXT)")
            con.execute(
                "CREATE TABLE mh_scalp_excursions (mint TEXT, ticker TEXT, mode TEXT, entry_usd REAL, "
                "peak_usd REAL, trough_usd REAL, open_ts REAL, close_ts REAL, hold_seconds REAL, "
                "realized_pct REAL, exit_reason TEXT)")
            con.execute(
                "CREATE TABLE mh_scalp_price_samples (mint TEXT, opened_ts REAL, sample_ts REAL, "
                "price_usd REAL, PRIMARY KEY (mint, opened_ts, sample_ts))")

    def tearDown(self):
        self.tmp.cleanup()

    def add_trade(self, mint, symbol, entry, exit_px, pct, usd, reason,
                  open_ts=0.0, close_ts=100.0, qty=1.0):
        with sqlite3.connect(self.db) as con:
            con.execute(
                "INSERT INTO mh_trades(coin,symbol,setup,side,open_ts,close_ts,entry_px,exit_px,"
                "qty,realized_pct,realized_usd,exit_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (mint, symbol, "dynamic_scalper", "LONG", open_ts, close_ts, entry, exit_px,
                 qty, pct, usd, reason))

    def add_path(self, mint, prices=(1.0, 1.085, 1.05, 1.02), opened=0.0, reason="trail_stop"):
        import dynamic_shadow_scalper as ds
        import parameter_autotuner as pa
        with ds._connect(self.db) as con:
            con.execute("INSERT INTO mh_scalp_policy_evidence VALUES(?,?,?,0)",
                        (mint, opened, pa.policy_signature(li._default_params(li.mode_for_mint(mint, cfg())))))
            con.execute(
                "INSERT INTO mh_scalp_excursions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (mint, "X", li.mode_for_mint(mint, cfg()), prices[0], max(prices), min(prices),
                 opened, opened + 180.0, 180.0, prices[-1] / prices[0] - 1, reason))
            for j, price in enumerate(prices):
                con.execute("INSERT INTO mh_scalp_price_samples VALUES(?,?,?,?)",
                            (mint, opened, opened + j * 60.0, price))


class PerCoinWindowTests(CoinReviewBase):
    def test_window_is_per_coin_and_newest_first(self):
        for i in range(7):
            self.add_trade(COIN_A, "A", 1.0, 1.01, 0.01, 0.01, "take_profit",
                           open_ts=float(i), close_ts=float(i) + 1)
        self.add_trade(COIN_B, "B", 1.0, 0.9, -0.1, -0.1, "stop_loss", open_ts=99.0, close_ts=100.0)
        rows = cr.recent_trades(self.db, COIN_A, limit=5)
        self.assertEqual(len(rows), 5)
        self.assertTrue(all(r["coin"] == COIN_A for r in rows))
        self.assertEqual([r["close_ts"] for r in rows], sorted([r["close_ts"] for r in rows], reverse=True))

    def test_collect_keeps_coins_separate(self):
        self.add_trade(COIN_A, "A", 1.0, 1.01, 0.01, 0.01, "take_profit", open_ts=1.0, close_ts=2.0)
        self.add_trade(COIN_B, "B", 1.0, 0.9, -0.1, -0.1, "stop_loss", open_ts=1.0, close_ts=2.0)
        bundle = cr.collect(self.db, cfg())
        self.assertEqual(len(bundle["coins"]), 2)
        by_mint = {c["mint"]: c for c in bundle["coins"]}
        self.assertEqual(by_mint[COIN_A]["metrics"]["win_rate"], 1.0)
        self.assertEqual(by_mint[COIN_B]["metrics"]["win_rate"], 0.0)
        self.assertEqual(by_mint[COIN_A]["metrics"]["n"], 1)

    def test_metrics_report_payoff_asymmetry(self):
        for _ in range(4):
            self.add_trade(COIN_A, "A", 1.0, 1.02, 0.02, 0.02, "trail_stop")
        self.add_trade(COIN_A, "A", 1.0, 0.9, -0.10, -0.10, "stop_loss")
        m = cr.metrics(cr.recent_trades(self.db, COIN_A))
        self.assertEqual((m["n"], m["wins"], m["losses"]), (5, 4, 1))
        self.assertAlmostEqual(m["payoff_ratio"], 0.2, places=3)
        self.assertLess(m["expectancy_usd"], 0)


class DiagnosisTests(CoinReviewBase):
    def test_flags_stop_that_landed_beyond_the_threshold(self):
        self.add_trade(COIN_A, "A", 1.0, 0.8084, -0.1916, -0.19, "stop_loss")
        findings = cr.diagnose(cr.coin_case(self.db, cfg(), COIN_A))
        self.assertIn("stop_not_honoured", [f["finding"] for f in findings])

    def test_flags_giving_back_the_run(self):
        self.add_trade(COIN_A, "A", 1.0, 1.02, 0.02, 0.02, "trail_stop", open_ts=5.0, close_ts=10.0)
        with sqlite3.connect(self.db) as con:
            con.execute(
                "INSERT INTO mh_scalp_excursions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (COIN_A, "A", "MEME", 1.0, 1.30, 0.95, 5.0, 10.0, 5.0, 0.02, "trail_stop"))
        findings = cr.diagnose(cr.coin_case(self.db, cfg(), COIN_A))
        self.assertIn("gives_back_run", [f["finding"] for f in findings])

    def test_flags_dead_max_hold_exits(self):
        for _ in range(2):
            self.add_trade(COIN_A, "A", 1.0, 1.0005, 0.0005, 0.0005, "max_hold")
        findings = cr.diagnose(cr.coin_case(self.db, cfg(), COIN_A))
        self.assertIn("dead_max_hold_exits", [f["finding"] for f in findings])

    def test_no_trades_reports_no_closed_trades(self):
        case = cr.coin_case(self.db, cfg(), COIN_C)
        self.assertEqual(case["findings"][0]["finding"], "no_closed_trades")


class GateTests(CoinReviewBase):
    def good(self, **over):
        proposal = {"mint": COIN_A, "take_profit_pct": 0.20, "stop_loss_pct": -0.10,
                    "trail_arm_pct": 0.08, "trail_distance_pct": 0.02,
                    "max_hold_seconds": 900}
        proposal.update(over)
        return proposal

    def seed_winning_paths(self, n=cr.MIN_PATHS_FOR_PROPOSAL):
        for i in range(n):
            self.add_path(COIN_A, opened=float(i * 10_000))

    def test_rejects_unsupported_keys(self):
        clean, reason = cr._validate_proposal(self.good(position_fraction=0.9))
        self.assertIsNone(clean)
        self.assertIn("unsupported keys", reason)

    def test_rejects_out_of_bounds(self):
        clean, reason = cr._validate_proposal(self.good(stop_loss_pct=-0.90))
        self.assertIsNone(clean)
        self.assertIn("outside", reason)

    def test_rejects_trail_distance_above_its_arm(self):
        clean, reason = cr._validate_proposal(self.good(trail_arm_pct=0.02, trail_distance_pct=0.04))
        self.assertIsNone(clean)
        self.assertIn("below its own arm", reason)

    def test_refuses_without_enough_replayable_paths(self):
        self.add_path(COIN_A, opened=0.0)
        result = cr.review_coin(self.db, cfg(), self.good())
        self.assertFalse(result["applied"])
        self.assertEqual(result["reason"], "insufficient_replayable_paths")
        self.assertIsNone(li._coin_risk_params_override(self.db, COIN_A))

    def test_applies_when_the_replay_says_it_beats_the_incumbent(self):
        self.seed_winning_paths()
        result = cr.review_coin(self.db, cfg(), self.good())
        self.assertTrue(result["applied"], result)
        self.assertEqual(result["reason"], "beats_incumbent_out_of_sample")
        self.assertEqual(result["confidence"], "low")
        got = li._coin_risk_params_override(self.db, COIN_A)
        self.assertIsNotNone(got)
        self.assertEqual(got["trail_distance_pct"], 0.02)

    def test_rejects_when_the_candidate_is_worse(self):
        self.seed_winning_paths()
        worse = self.good(trail_arm_pct=0.12, trail_distance_pct=0.06)
        result = cr.review_coin(self.db, cfg(), worse)
        self.assertFalse(result["applied"])
        self.assertIsNone(li._coin_risk_params_override(self.db, COIN_A))

    def test_cooldown_blocks_a_second_change(self):
        self.seed_winning_paths()
        self.assertTrue(cr.review_coin(self.db, cfg(), self.good())["applied"])
        second = cr.review_coin(self.db, cfg(), self.good(take_profit_pct=0.25))
        self.assertFalse(second["applied"])
        self.assertIn("cooldown", second["reason"])

    def test_every_review_is_logged_accepted_or_rejected(self):
        self.add_path(COIN_A, opened=0.0)
        cr.review_coin(self.db, cfg(), self.good())
        with sqlite3.connect(self.db) as con:
            rows = con.execute("SELECT decision, reason, applied FROM mh_coin_review_log").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "rejected")
        self.assertEqual(rows[0][2], 0)


class OverrideScopeTests(CoinReviewBase):
    def test_per_coin_override_does_not_leak_to_another_coin(self):
        self.seed_paths_for(COIN_A)
        self.assertTrue(cr.review_coin(self.db, cfg(), {
            "mint": COIN_A, "take_profit_pct": 0.20, "stop_loss_pct": -0.10,
            "trail_arm_pct": 0.08, "trail_distance_pct": 0.02,
            "max_hold_seconds": 900})["applied"])
        a = li.risk_params(COIN_A, cfg(), db_path=self.db, allow_tuned=True)
        b = li.risk_params(COIN_B, cfg(), db_path=self.db, allow_tuned=True)
        self.assertEqual(a["trail_distance_pct"], 0.02)
        self.assertNotEqual(b["trail_distance_pct"], 0.02)
        self.assertEqual(b["trail_distance_pct"], li.MEME_TRAIL_DISTANCE_PCT)

    def test_live_callers_never_see_the_override(self):
        self.seed_paths_for(COIN_A)
        cr.review_coin(self.db, cfg(), {
            "mint": COIN_A, "take_profit_pct": 0.20, "stop_loss_pct": -0.10,
            "trail_arm_pct": 0.08, "trail_distance_pct": 0.02,
            "max_hold_seconds": 900})
        live = li.risk_params(COIN_A, cfg(), db_path=self.db, allow_tuned=False)
        self.assertEqual(live["trail_distance_pct"], li.MEME_TRAIL_DISTANCE_PCT)

    def seed_paths_for(self, mint, n=cr.MIN_PATHS_FOR_PROPOSAL):
        for i in range(n):
            self.add_path(mint, opened=float(i * 10_000))

    def test_check_constraint_rejects_degenerate_coin_params(self):
        with self.assertRaises(sqlite3.IntegrityError):
            li.set_coin_risk_params_override(
                self.db, COIN_A,
                {"take_profit_pct": 5.0, "stop_loss_pct": -0.10, "trail_arm_pct": 0.08,
                 "trail_distance_pct": 0.02, "max_hold_seconds": 900, "mode": "MEME"},
                source="test", sample_n=9)


if __name__ == "__main__":
    unittest.main()
