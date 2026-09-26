"""Behavioural regression tests for cost-aware paper P&L accounting.

Defect: every paper close path computed realized P&L as gross price movement
(`qty * entry * pct`) with NO trading cost subtracted, while the live path pays
Jupiter quote fees plus slippage on BOTH legs of a round trip. Paper therefore
reported systematically optimistic results, and at the configured $1 minimum
notional a nominal winner can be a real net loser.

These tests assert the invariant (net P&L = gross minus round-trip cost) and
fail closed on malformed cost configuration. They deliberately exercise the
callable unit rather than reading source text.
"""

import math
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import paper


class RoundTripCostTests(unittest.TestCase):
    """The shared cost model must be deterministic and fail closed."""

    def test_cost_is_sum_of_both_legs_of_the_round_trip(self):
        # 40 bps quote + 50 bps slippage per leg, two legs (buy then sell)
        # = 90 * 2 = 180 bps = 1.8% of notional.
        cfg = {"paper": {"quote_bps": 40, "slippage_bps": 50}}
        self.assertAlmostEqual(paper.round_trip_cost_pct(cfg), 0.018, places=9)

    def test_zero_cost_config_is_explicitly_zero(self):
        cfg = {"paper": {"quote_bps": 0, "slippage_bps": 0}}
        self.assertEqual(paper.round_trip_cost_pct(cfg), 0.0)

    def test_missing_config_fails_closed_to_documented_default(self):
        # Never silently assume free trading: absent keys must use the
        # conservative default rather than zero.
        self.assertGreater(paper.round_trip_cost_pct({}), 0.0)
        self.assertGreater(paper.round_trip_cost_pct({"paper": {}}), 0.0)

    def test_negative_or_nonfinite_cost_config_is_rejected(self):
        for bad in (-1, float("nan"), float("inf"), "abc", None):
            cfg = {"paper": {"quote_bps": bad, "slippage_bps": 50}}
            with self.assertRaises(ValueError):
                paper.round_trip_cost_pct(cfg)

    def test_cost_never_exceeds_one(self):
        cfg = {"paper": {"quote_bps": 40000, "slippage_bps": 40000}}
        with self.assertRaises(ValueError):
            paper.round_trip_cost_pct(cfg)


class NetRealizedPnlTests(unittest.TestCase):
    """Net P&L must deduct the full round trip from gross movement."""

    def test_gross_gain_below_cost_becomes_a_net_loss(self):
        # +1.0% gross against a 1.8% round trip must NOT book as a winner.
        net_pct, net_usd, cost_usd = paper.net_realized(
            qty=10.0, entry=1.0, gross_pct=0.010,
            cfg={"paper": {"quote_bps": 40, "slippage_bps": 50}})
        self.assertLess(net_pct, 0.0, "sub-cost gain must book as a net loss")
        self.assertLess(net_usd, 0.0)
        self.assertAlmostEqual(net_pct, 0.010 - 0.018, places=12)
        self.assertAlmostEqual(cost_usd, 10.0 * 1.0 * 0.018, places=12)

    def test_large_gain_still_nets_to_gross_minus_cost(self):
        net_pct, net_usd, cost_usd = paper.net_realized(
            qty=4.0, entry=100.0, gross_pct=0.05,
            cfg={"paper": {"quote_bps": 40, "slippage_bps": 50}})
        self.assertAlmostEqual(net_pct, 0.05 - 0.018, places=12)
        self.assertAlmostEqual(cost_usd, 4.0 * 100.0 * 0.018, places=12)
        self.assertAlmostEqual(net_usd, 4.0 * 100.0 * (0.05 - 0.018), places=10)

    def test_net_realized_rejects_nonfinite_inputs(self):
        cfg = {"paper": {"quote_bps": 40, "slippage_bps": 50}}
        # qty and entry must be strictly positive.
        for bad in (float("nan"), float("inf"), 0.0, -1.0, "abc", None):
            with self.assertRaises(ValueError):
                paper.net_realized(qty=bad, entry=1.0, gross_pct=0.01, cfg=cfg)
            with self.assertRaises(ValueError):
                paper.net_realized(qty=1.0, entry=bad, gross_pct=0.01, cfg=cfg)
        # gross_pct may legitimately be 0.0 (flat close) or negative (a loss),
        # but must still be a finite real number.
        for bad in (float("nan"), float("inf"), "abc", None):
            with self.assertRaises(ValueError):
                paper.net_realized(qty=1.0, entry=1.0, gross_pct=bad, cfg=cfg)

    def test_flat_close_is_allowed_and_still_pays_cost(self):
        net_pct, net_usd, cost_usd = paper.net_realized(
            qty=1.0, entry=100.0, gross_pct=0.0,
            cfg={"paper": {"quote_bps": 40, "slippage_bps": 50}})
        self.assertAlmostEqual(net_pct, -0.018, places=12)
        self.assertLess(net_usd, 0.0, "a doji close still loses the round trip")
        self.assertAlmostEqual(cost_usd, 100.0 * 0.018, places=12)


class ClosePositionPersistsCostTests(unittest.TestCase):
    """The persisted trade row and the wallet credit must both be NET."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = patch.object(paper, "DB_PATH", Path(self.tmp.name) / "t.db")
        self.p.start()
        self.cfg = {"paper": {"quote_bps": 40, "slippage_bps": 50}}
        paper.ensure_account(paper.TRADER_SCALPER, 100.0)
        self.con = paper._connect()
        self.con.execute(
            "INSERT INTO mh_positions(coin,symbol,setup,side,open_ts,entry_px,qty,peak_px,trail_armed)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            ("SOL", "SOL", "momentum", "LONG", 1000.0, 100.0, 1.0, 100.0, 0))
        self.con.commit()
        self.pos = dict(self.con.execute(
            "SELECT * FROM mh_positions").fetchone())
        self.con.close()

    def tearDown(self):
        self.p.stop()
        self.tmp.cleanup()

    def _close_and_read(self, exit_px, cfg):
        r = paper.close_position(self.pos, exit_px, "take_profit", cfg=cfg)
        con = paper._connect()
        row = con.execute("SELECT * FROM mh_trades").fetchone()
        eq = con.execute(
            "SELECT equity_usd FROM mh_accounts WHERE trader=?",
            (paper.TRADER_SCALPER,)).fetchone()["equity_usd"]
        con.close()
        return r, dict(row), eq

    def test_row_and_wallet_record_net_not_gross(self):
        r, row, eq = self._close_and_read(100.5, self.cfg)
        # +0.5% gross against a 1.8% round trip is a NET LOSS of -1.3%.
        expected_pct = 0.005 - 0.018
        self.assertLess(expected_pct, 0.0)
        self.assertAlmostEqual(row["realized_pct"], expected_pct, places=12)
        self.assertAlmostEqual(row["realized_usd"], 100.0 * expected_pct, places=9)
        # wallet started at 100.0 and must be credited the NET amount only
        self.assertAlmostEqual(eq, 100.0 + row["realized_usd"], places=9)
        self.assertAlmostEqual(r["pct"], expected_pct, places=12)
        self.assertGreater(row["cost_usd"], 0.0, "cost must be persisted, not implicit")

    def test_backtest_era_legacy_rows_are_not_rewritten(self):
        con = paper._connect()
        con.execute(
            "INSERT INTO mh_trades(coin,symbol,setup,side,open_ts,close_ts,entry_px,exit_px,"
            "qty,realized_pct,realized_usd,cost_usd,exit_reason)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("OLD", "OLD", "legacy", "LONG", 1, 2, 1.0, 1.05, 1.0, 0.05, 0.05, 0.0, "take_profit"))
        con.commit()
        con.close()
        self._close_and_read(100.5, self.cfg)
        con = paper._connect()
        legacy = con.execute(
            "SELECT realized_pct, cost_usd FROM mh_trades WHERE coin='OLD'").fetchone()
        con.close()
        self.assertAlmostEqual(legacy["realized_pct"], 0.05, places=12)
        self.assertEqual(legacy["cost_usd"], 0.0)

    def test_malformed_cost_config_blocks_the_close(self):
        before = paper.equity(paper.TRADER_SCALPER)
        with self.assertRaises(ValueError):
            paper.close_position(self.pos, 100.5, "take_profit",
                                 cfg={"paper": {"quote_bps": -5, "slippage_bps": 50}})
        con = paper._connect()
        n = con.execute("SELECT COUNT(*) c FROM mh_trades").fetchone()["c"]
        pos = con.execute("SELECT COUNT(*) c FROM mh_positions").fetchone()["c"]
        con.close()
        self.assertEqual(n, 0, "a blocked close must not write a trade row")
        self.assertEqual(pos, 1, "a blocked close must not delete the open position")
        self.assertEqual(paper.equity(paper.TRADER_SCALPER), before)


if __name__ == "__main__":
    unittest.main()
