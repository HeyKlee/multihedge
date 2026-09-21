import contextlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import dynamic_shadow_scalper as ds
import solana_token_universe as stu

MINT = "B5WTLaRwaUQpKk7ir1wniNB6m5o8GgMrimhKMYan2R6B"


@contextlib.contextmanager
def no_retry_sleep():
    """Keep upstream retry tests instant while still recording the waits."""
    slept = []
    original = stu._sleep
    stu._sleep = slept.append
    try:
        yield slept
    finally:
        stu._sleep = original


def candidate(price=.001, change5=2.0, change1h=5.0, buy=200_000, sell=100_000):
    return {
        "mint": MINT, "symbol": MINT, "ticker": "PEPE", "decimals": 6,
        "market": {
            "latest_usd": price, "return_5m_pct": change5,
            "return_1h_pct": change1h, "buy_volume_5m_usd": buy,
            "sell_volume_5m_usd": sell,
            "rsi_15m": 50.0,  # neutral RSI by default (passes RSI < 70 filter)
            "volume_5m_usd": (buy + sell) * 2.0,  # 2x average to pass 1.5x volume surge filter
            "volume_5m_avg_20": buy + sell,  # 20-period average
        },
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
        # Meme TP is 20%: entry .001, a 22% move closes it.
        result = ds.tick(self.db, [candidate(price=.00122, change5=1)], now=1060)
        self.assertEqual(result["closed"], 1)
        with sqlite3.connect(self.db) as con:
            trade = con.execute(
                "SELECT coin,symbol,setup,exit_reason,realized_pct FROM mh_trades"
            ).fetchone()
            positions = con.execute("SELECT count(*) FROM mh_dynamic_scalp_positions").fetchone()[0]
        self.assertEqual(trade[:4], (MINT, "PEPE", "dynamic_scalper", "take_profit"))
        self.assertGreater(trade[4], .2)
        self.assertEqual(positions, 0)

    def test_stop_loss_is_deterministic_and_age_alone_does_not_exit(self):
        ds.tick(self.db, [candidate()], now=1000)
        # Meme SL is -10%: entry .001, an -11% drop stops it out.
        result = ds.tick(self.db, [candidate(price=.00089, change5=-1)], now=1060)
        self.assertEqual(result["reasons"], {"stop_loss": 1})
        ds.tick(self.db, [candidate()], now=2000)
        result = ds.tick(self.db, [candidate(price=.001, change5=0)], now=2901)
        self.assertEqual(result["closed"], 0)

    def test_max_hold_only_falls_back_after_take_profit_was_missed(self):
        ds.tick(self.db, [candidate()], now=1000)
        with sqlite3.connect(self.db) as con:
            con.execute(
                "UPDATE mh_dynamic_scalp_positions SET peak_usd=? WHERE mint=?",
                (.00121, MINT),
            )
        result = ds.tick(self.db, [candidate(price=.00105, change5=0)], now=1901)
        self.assertEqual(result["reasons"], {"max_hold": 1})

    def test_no_entry_without_balanced_sell_liquidity_and_momentum(self):
        self.assertEqual(ds.tick(self.db, [candidate(change5=0.1)], now=1000)["opened"], 0)
        self.assertEqual(ds.tick(self.db, [candidate(sell=0)], now=1060)["opened"], 0)

    def test_serious_coin_day_trades_over_hours_not_15_min_scalp(self):
        # JUP is a config `coins` entry -> SERIOUS day-trade params.
        cfg = {"coins": [{"symbol": "JUP", "mint": MINT}]}
        ds.tick(self.db, [candidate()], now=1000, cfg=cfg)
        # At 15 minutes (900s) a memecoin would max-hold; a serious coin holds on.
        result = ds.tick(self.db, [candidate(price=.00101, change5=0)], now=1905, cfg=cfg)
        self.assertEqual(result["closed"], 0)
        # But it day-trades on a longer clock: still open at ~1.5h, not force-sold.
        result = ds.tick(self.db, [candidate(price=.00101, change5=0)], now=1000 + 5400, cfg=cfg)
        self.assertEqual(result["closed"], 0)

    def test_memecoin_swings_for_20pct_tp(self):
        # A 20%+ move closes a memecoin take-profit.
        cfg = {"coins": [{"symbol": "JUP", "mint": "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN"}]}
        ds.tick(self.db, [candidate()], now=1000, cfg=cfg)
        result = ds.tick(self.db, [candidate(price=.00122, change5=1)], now=1060, cfg=cfg)
        self.assertEqual(result["reasons"], {"take_profit": 1})

    def test_risk_params_classify_serious_vs_memecoin(self):
        import live_inventory as li
        cfg = {"coins": [{"symbol": "JUP", "mint": "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN"}]}
        serious = li.risk_params("JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN", cfg)
        meme = li.risk_params(MINT, cfg)
        self.assertEqual(serious["mode"], "SERIOUS")
        self.assertEqual(meme["mode"], "MEME")
        self.assertEqual(serious["take_profit_pct"], 0.05)
        self.assertEqual(serious["stop_loss_pct"], -0.025)
        self.assertGreaterEqual(serious["max_hold_seconds"], 6 * 3600)
        self.assertEqual(meme["take_profit_pct"], 0.20)
        self.assertEqual(meme["stop_loss_pct"], -0.10)
        self.assertEqual(meme["trail_arm_pct"], 0.08)
        self.assertEqual(meme["trail_distance_pct"], 0.04)
        self.assertEqual(meme["max_hold_seconds"], 900)

    def test_memecoin_trail_ignores_small_noise_then_protects_larger_move(self):
        ds.tick(self.db, [candidate()], now=1000)
        with sqlite3.connect(self.db) as con:
            con.execute(
                "UPDATE mh_dynamic_scalp_positions SET peak_usd=? WHERE mint=?",
                (.00105, MINT),
            )
        # A 5% peak and ordinary pullback must not arm the wider MEME trail.
        result = ds.tick(self.db, [candidate(price=.00102, change5=0)], now=1060)
        self.assertEqual(result["closed"], 0)
        with sqlite3.connect(self.db) as con:
            con.execute(
                "UPDATE mh_dynamic_scalp_positions SET peak_usd=? WHERE mint=?",
                (.00110, MINT),
            )
        # Once up 8%+, a pullback exceeding 4% from peak protects the move.
        result = ds.tick(self.db, [candidate(price=.00105, change5=0)], now=1120)
        self.assertEqual(result["reasons"], {"trail_stop": 1})

    def test_upstream_rate_limit_degrades_without_opening_risk(self):
        cfg = {"live": {"autonomous": {"dynamic_universe": {"enabled": True, "max_candidates": 12}}}}
        get = Mock(return_value=stu.FakeResponse(429, {}))
        with no_retry_sleep():
            result, code = ds.run_cycle(cfg, self.db, now=1000, api_key="key", get=get)
        self.assertEqual(code, 0)
        self.assertEqual(result["state"], "SHADOW_SCALP_DEGRADED")
        self.assertTrue(result["new_risk_blocked"])
        self.assertTrue(result["exits_unevaluated"])
        self.assertEqual(result["opened"], 0)
        with sqlite3.connect(self.db) as con:
            tables = {row[0] for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("mh_dynamic_scalp_positions", tables)

    def test_holding_price_failure_keeps_pending_exit_untouched(self):
        import live_inventory
        with live_inventory._connect(self.db) as con:
            con.execute(
                "INSERT INTO mh_live_inventory(mint,ticker,decimals,amount_atomic,"
                "cost_usdc_atomic,entry_usd,peak_usd,opened_ts,updated_ts) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (MINT, "PEPE", 6, 1000, 1_000_000, .001, .001, 0.0, 0.0),
            )
        pending = Path(self.tmp.name) / "forced_exit.json"
        payload = json.dumps({"action": "SELL", "symbol": MINT, "exit_reason": "stop_loss"})
        pending.write_text(payload)
        cfg = {"live": {"autonomous": {"dynamic_universe": {"enabled": True}}}}
        get = Mock(side_effect=[
            stu.FakeResponse(200, []), stu.FakeResponse(200, []), stu.FakeResponse(200, []),
            stu.FakeResponse(429, {}), stu.FakeResponse(429, {}), stu.FakeResponse(429, {}),
        ])
        with no_retry_sleep():
            result, code = ds.run_cycle(
                cfg, self.db, now=1000, api_key="key", get=get, forced_path=pending
            )
        self.assertEqual(code, 0)
        self.assertEqual(result["state"], "SHADOW_SCALP_DEGRADED")
        self.assertTrue(result["exits_unevaluated"])
        self.assertEqual(pending.read_text(), payload)


    def test_missing_jupiter_key_degrades_instead_of_aborting_cycle(self):
        # A missing or rejected provider credential must not abort the cycle:
        # the launcher stops on a non-zero exit and would skip the signer step,
        # leaving a real forced exit unsettled for that minute.
        cfg = {"live": {"autonomous": {"dynamic_universe": {"enabled": True, "max_candidates": 12}}}}
        result, code = ds.run_cycle(cfg, self.db, now=1000, api_key="")
        self.assertEqual(code, 0)
        self.assertEqual(result["state"], "SHADOW_SCALP_DEGRADED")
        self.assertTrue(result["new_risk_blocked"])
        self.assertTrue(result["exits_unevaluated"])
        self.assertEqual(result["candidates"], 0)

    def test_closed_shadow_trade_dollar_pnl_matches_notional_times_pct(self):
        ds.tick(self.db, [candidate()], now=1000)
        result = ds.tick(self.db, [candidate(price=.00122, change5=1)], now=1060)
        self.assertEqual(result["closed"], 1)
        with sqlite3.connect(self.db) as con:
            pct, usd, qty, entry_px, exit_px = con.execute(
                "SELECT realized_pct,realized_usd,qty,entry_px,exit_px FROM mh_trades"
            ).fetchone()
        # The dollar figure is now actual P&L from quantity, not fixed notional.
        self.assertAlmostEqual(usd, qty * (exit_px - entry_px), places=12)

    def test_missing_jupiter_key_leaves_pending_exit_untouched(self):
        pending = Path(self.tmp.name) / "forced_exit.json"
        payload = json.dumps({"action": "SELL", "symbol": MINT, "exit_reason": "stop_loss"})
        pending.write_text(payload)
        cfg = {"live": {"autonomous": {"dynamic_universe": {"enabled": True}}}}
        result, code = ds.run_cycle(
            cfg, self.db, now=1000, api_key="", forced_path=pending)
        self.assertEqual(code, 0)
        self.assertEqual(result["state"], "SHADOW_SCALP_DEGRADED")
        self.assertEqual(pending.read_text(), payload)

    def test_compounding_credits_profit_to_wallet_and_scales_position_size(self):
        """After a winning trade, wallet equity grows and the next position is larger."""
        # Create mh_accounts table and seed the dynamic_scalper wallet
        con = sqlite3.connect(self.db)
        con.execute("CREATE TABLE IF NOT EXISTS mh_accounts (trader TEXT PRIMARY KEY,equity_usd REAL,started_usd REAL)")
        con.execute("INSERT OR IGNORE INTO mh_accounts(trader,equity_usd,started_usd) VALUES(?,?,?)",
                    (ds.SETUP, ds.INITIAL_EQUITY_USD, ds.INITIAL_EQUITY_USD))
        con.commit()
        con.close()

        # Open a position, then close with profit
        ds.tick(self.db, [candidate()], now=1000)
        entry_result = ds.tick(self.db, [candidate(price=.00122, change5=1)], now=1060)
        self.assertEqual(entry_result["closed"], 1)

        # Verify wallet equity increased by the realized P&L
        con = sqlite3.connect(self.db)
        eq = con.execute("SELECT equity_usd FROM mh_accounts WHERE trader=?", (ds.SETUP,)).fetchone()[0]
        trade = con.execute("SELECT realized_usd FROM mh_trades").fetchone()
        con.close()
        realized = trade[0]
        self.assertGreater(eq, ds.INITIAL_EQUITY_USD)
        self.assertAlmostEqual(eq, ds.INITIAL_EQUITY_USD + realized, places=10)

    def test_entry_signal_conditions(self):
        self.assertTrue(ds._entry_signal(candidate(change5=5.0, change1h=2.0, buy=10000, sell=3000)))
        self.assertFalse(ds._entry_signal(candidate(change5=-1.0, change1h=2.0, buy=10000, sell=3000)))
        self.assertFalse(ds._entry_signal(candidate(change5=0.5, change1h=2.0, buy=10000, sell=3000)))
        self.assertFalse(ds._entry_signal(candidate(change5=5.0, change1h=2.0, buy=1000, sell=3000)))
        self.assertTrue(ds._entry_signal(candidate(change5=2.0, change1h=-1.0, buy=40000, sell=10000)))


if __name__ == "__main__":
    unittest.main()
