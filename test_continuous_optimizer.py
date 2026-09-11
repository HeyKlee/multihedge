import math
import sqlite3
import unittest

import continuous_optimizer as opt


class ContinuousOptimizerTests(unittest.TestCase):
    def test_round_trip_cost_is_charged_on_entry_and_exit(self):
        trade = {"entry_px": 100.0, "qty": 0.1, "realized_usd": 0.10}
        adjusted = opt.adjust_trade(trade, per_side_bps=40.0, fixed_cost_usd=0.0)
        self.assertAlmostEqual(adjusted["gross_usd"], 0.10)
        self.assertAlmostEqual(adjusted["modeled_cost_usd"], 0.08)
        self.assertAlmostEqual(adjusted["net_usd"], 0.02)

    def test_chronological_split_has_no_overlap(self):
        rows = [{"id": i, "close_ts": float(i)} for i in range(100)]
        split = opt.chronological_split(rows)
        self.assertEqual([len(split[k]) for k in ("train", "validation", "test")], [70, 15, 15])
        ids = [{r["id"] for r in split[k]} for k in ("train", "validation", "test")]
        self.assertFalse(ids[0] & ids[1] or ids[0] & ids[2] or ids[1] & ids[2])
        self.assertLess(max(r["close_ts"] for r in split["train"]), min(r["close_ts"] for r in split["validation"]))
        self.assertLess(max(r["close_ts"] for r in split["validation"]), min(r["close_ts"] for r in split["test"]))

    def test_conservative_score_rejects_tiny_sample(self):
        result = opt.conservative_expectancy([0.03, 0.04, 0.05], min_samples=20)
        self.assertFalse(result["eligible"])
        self.assertEqual(result["reason"], "insufficient_samples")

    def test_conservative_score_uses_payoff_not_win_rate_alone(self):
        high_win_bad_payoff = [0.002] * 18 + [-0.05] * 2
        lower_win_good_payoff = [0.02] * 12 + [-0.01] * 8
        bad = opt.conservative_expectancy(high_win_bad_payoff, min_samples=20)
        good = opt.conservative_expectancy(lower_win_good_payoff, min_samples=20)
        self.assertLess(bad["score"], good["score"])
        self.assertLess(bad["mean"], 0.0)
        self.assertGreater(good["mean"], 0.0)

    def test_promotion_fails_closed_on_small_test_sample(self):
        champion = {"n": 40, "expectancy": -0.01, "max_drawdown": 0.10}
        challenger = {"n": 19, "expectancy": 0.01, "max_drawdown": 0.05, "stress_expectancy": 0.005}
        verdict = opt.promotion_gate(champion, challenger, folds_won=3, folds_total=4)
        self.assertFalse(verdict["accepted"])
        self.assertIn("test_sample_below_20", verdict["reasons"])

    def test_promotion_fails_closed_on_negative_stress_expectancy(self):
        champion = {"n": 50, "expectancy": 0.001, "max_drawdown": 0.10}
        challenger = {"n": 50, "total_n": 50, "expectancy": 0.002, "max_drawdown": 0.09, "stress_expectancy": -0.001}
        verdict = opt.promotion_gate(champion, challenger, folds_won=3, folds_total=4)
        self.assertFalse(verdict["accepted"])
        self.assertIn("negative_stress_expectancy", verdict["reasons"])

    def test_promotion_enforces_relative_and_absolute_drawdown_limits(self):
        champion = {"n": 50, "expectancy": 0.001, "max_drawdown": 0.10}
        challenger = {"n": 20, "total_n": 50, "expectancy": 0.002, "max_drawdown": 0.15, "stress_expectancy": 0.001}
        verdict = opt.promotion_gate(champion, challenger, folds_won=3, folds_total=4)
        self.assertFalse(verdict["accepted"])
        self.assertIn("drawdown_relative_gate_failed", verdict["reasons"])

    def test_promotion_requires_fifty_total_challenger_trades(self):
        champion = {"n": 50, "expectancy": 0.001, "max_drawdown": 0.10}
        challenger = {"n": 20, "total_n": 49, "expectancy": 0.002, "max_drawdown": 0.09, "stress_expectancy": 0.001}
        verdict = opt.promotion_gate(champion, challenger, folds_won=3, folds_total=4)
        self.assertFalse(verdict["accepted"])
        self.assertIn("total_sample_below_50", verdict["reasons"])

    def test_account_reconciliation_reports_unexplained_difference(self):
        result = opt.reconcile_account(started=24.0, current=22.5, ledger_pnl=-1.75)
        self.assertAlmostEqual(result["expected_equity_usd"], 22.25)
        self.assertAlmostEqual(result["unexplained_difference_usd"], 0.25)
        self.assertFalse(result["reconciled"])

    def test_trade_owner_uses_setup_without_mixing_wallets(self):
        self.assertEqual(opt.trade_owner("reasoner"), "reasoner")
        self.assertEqual(opt.trade_owner("whale_trader"), "whale_trader")
        self.assertEqual(opt.trade_owner("memecoin_trader"), "memecoin_trader")
        self.assertEqual(opt.trade_owner("momentum_breakout"), "scalper")

    def test_grid_replay_counts_each_sell_once_when_cycle_ids_repeat(self):
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.execute("CREATE TABLE grid_trades(id INTEGER,ts REAL,side TEXT,level_px REAL,qty REAL,usd REAL,cycle_id INTEGER,realized_usd REAL)")
        con.executemany("INSERT INTO grid_trades VALUES(?,?,?,?,?,?,?,?)", [
            (1, 1.0, "BUY", 100.0, 0.1, 10.0, 7, 0.0),
            (2, 2.0, "BUY", 99.0, 0.1, 9.9, 7, 0.0),
            (3, 3.0, "SELL", 101.0, 0.1, 10.1, 7, 0.1),
        ])
        rows = opt.grid_completed_rows(con)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], 3)
        con.close()

    def test_valid_trade_requires_positive_entry_and_quantity(self):
        self.assertTrue(opt.valid_trade({"entry_px": 100.0, "qty": 0.1}))
        self.assertFalse(opt.valid_trade({"entry_px": -100.0, "qty": 0.1}))
        self.assertFalse(opt.valid_trade({"entry_px": 100.0, "qty": -0.1}))
        self.assertFalse(opt.valid_trade({"entry_px": 0.0, "qty": 0.1}))


if __name__ == "__main__":
    unittest.main()
