import sqlite3
import tempfile
import unittest
from pathlib import Path

import dynamic_shadow_scalper as ds

MINT = "B5WTLaRwaUQpKk7ir1wniNB6m5o8GgMrimhKMYan2R6B"


def candidate(price=.001, change5=2.0, change1h=5.0, buy=20_000, sell=10_000):
    return {
        "mint": MINT, "symbol": MINT, "ticker": "PEPE", "decimals": 6,
        "market": {"latest_usd": price, "return_5m_pct": change5,
                   "return_1h_pct": change1h, "buy_volume_5m_usd": buy,
                   "sell_volume_5m_usd": sell},
    }


class DynamicShadowScalperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "paper.db"

    def tearDown(self):
        self.tmp.cleanup()

    def test_momentum_candidate_opens_one_mint_keyed_paper_position(self):
        result = ds.tick(self.db, [candidate()], now=1000)
        self.assertEqual(result["opened"], 1)
        with sqlite3.connect(self.db) as con:
            row = con.execute("SELECT mint,ticker,entry_usd FROM mh_dynamic_scalp_positions").fetchone()
        self.assertEqual(row, (MINT, "PEPE", .001))
        self.assertEqual(ds.tick(self.db, [candidate()], now=1060)["opened"], 0)

    def test_take_profit_closes_and_records_mint_evidence(self):
        ds.tick(self.db, [candidate()], now=1000)
        result = ds.tick(self.db, [candidate(price=.001031, change5=1)], now=1060)
        self.assertEqual(result["closed"], 1)
        with sqlite3.connect(self.db) as con:
            trade = con.execute(
                "SELECT coin,symbol,setup,exit_reason,realized_pct FROM mh_trades"
            ).fetchone()
            positions = con.execute("SELECT count(*) FROM mh_dynamic_scalp_positions").fetchone()[0]
        self.assertEqual(trade[:4], (MINT, "PEPE", "dynamic_scalper", "take_profit"))
        self.assertGreater(trade[4], .03)
        self.assertEqual(positions, 0)

    def test_stop_loss_and_max_hold_are_deterministic(self):
        ds.tick(self.db, [candidate()], now=1000)
        result = ds.tick(self.db, [candidate(price=.000979, change5=-1)], now=1060)
        self.assertEqual(result["reasons"], {"stop_loss": 1})
        ds.tick(self.db, [candidate()], now=2000)
        result = ds.tick(self.db, [candidate(price=.001, change5=0)], now=2901)
        self.assertEqual(result["reasons"], {"max_hold": 1})

    def test_no_entry_without_balanced_sell_liquidity_and_momentum(self):
        self.assertEqual(ds.tick(self.db, [candidate(change5=0.1)], now=1000)["opened"], 0)
        self.assertEqual(ds.tick(self.db, [candidate(sell=0)], now=1060)["opened"], 0)


if __name__ == "__main__":
    unittest.main()
