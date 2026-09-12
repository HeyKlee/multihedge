import tempfile
import unittest
from pathlib import Path

import live_inventory as li
from execution_policy import TradeIntent

MINT = "B5WTLaRwaUQpKk7ir1wniNB6m5o8GgMrimhKMYan2R6B"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def intent(side="BUY", amount=1_000_000):
    return TradeIntent(
        order_id=f"order-{side}", side=side,
        input_mint=USDC if side == "BUY" else MINT,
        output_mint=MINT if side == "BUY" else USDC,
        amount_atomic=amount, slippage_bps=50, strategy="dynamic_scalper",
        expected_reward_nzd="0.03", expected_loss_nzd="0.01",
    )


class LiveInventoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "inventory.db"

    def tearDown(self):
        self.tmp.cleanup()

    def test_reconciled_buy_creates_mint_keyed_inventory(self):
        li.record_fill(self.db, intent(), {"verified": True, "input_atomic": 1_000_000,
                                          "output_atomic": 1_000_000_000},
                       ticker="PEPE", decimals=6, price_usd=.001, now=1000)
        row = li.get_holding(self.db, MINT)
        self.assertEqual(row["amount_atomic"], 1_000_000_000)
        self.assertEqual(row["cost_usdc_atomic"], 1_000_000)
        self.assertEqual(row["mint"], MINT)

    def test_reconciled_full_sell_removes_inventory(self):
        li.record_fill(self.db, intent(), {"verified": True, "input_atomic": 1_000_000,
                                          "output_atomic": 1_000_000_000},
                       ticker="PEPE", decimals=6, price_usd=.001, now=1000)
        li.record_fill(self.db, intent("SELL", 1_000_000_000),
                       {"verified": True, "input_atomic": 1_000_000_000,
                        "output_atomic": 1_020_000},
                       ticker="PEPE", decimals=6, price_usd=.00102, now=1100)
        self.assertIsNone(li.get_holding(self.db, MINT))

    def test_take_profit_and_stop_loss_force_sell(self):
        # Meme thresholds: 20% TP, -10% SL, 900s max hold (entry .001).
        cases = [(.00122, 1060, "take_profit"), (.00089, 1060, "stop_loss")]
        for price, now, reason in cases:
            with self.subTest(reason=reason):
                db = self.db.with_name(reason + ".db")
                li.record_fill(db, intent(), {"verified": True, "input_atomic": 1_000_000,
                                              "output_atomic": 1_000_000_000},
                               ticker="PEPE", decimals=6, price_usd=.001, now=1000)
                decision = li.forced_exit(db, {MINT: price}, now=now)
                self.assertEqual(decision["symbol"], MINT)
                self.assertEqual(decision["exit_reason"], reason)

    def test_max_hold_only_falls_back_after_take_profit_was_missed(self):
        li.record_fill(self.db, intent(), {"verified": True, "input_atomic": 1_000_000,
                                          "output_atomic": 1_000_000_000},
                       ticker="PEPE", decimals=6, price_usd=.001, now=1000)
        # Age alone must no longer force a sale if TP was never reached.
        self.assertIsNone(li.forced_exit(self.db, {MINT: .001}, now=1901))
        # Simulate a TP crossing whose queued sell did not complete. The live
        # inventory retains the observed peak, so max-hold becomes the fallback.
        with li._connect(self.db) as con:
            con.execute("UPDATE mh_live_inventory SET peak_usd=? WHERE mint=?", (.00121, MINT))
        decision = li.forced_exit(self.db, {MINT: .00105}, now=1902)
        self.assertEqual(decision["exit_reason"], "max_hold")

    def test_live_memecoin_trail_ignores_noise_then_protects_larger_move(self):
        li.record_fill(self.db, intent(), {
            "verified": True, "input_atomic": 1_000_000,
            "output_atomic": 1_000_000_000,
        }, ticker="PEPE", decimals=6, price_usd=.001, now=1000)
        with li._connect(self.db) as con:
            con.execute("UPDATE mh_live_inventory SET peak_usd=? WHERE mint=?", (.00105, MINT))
        self.assertIsNone(li.forced_exit(self.db, {MINT: .00102}, now=1060))
        with li._connect(self.db) as con:
            con.execute("UPDATE mh_live_inventory SET peak_usd=? WHERE mint=?", (.00110, MINT))
        decision = li.forced_exit(self.db, {MINT: .00105}, now=1120)
        self.assertEqual(decision["exit_reason"], "trail_stop")

    def test_unregistered_wallet_token_cannot_be_treated_as_bot_inventory(self):
        self.assertIsNone(li.get_holding(self.db, MINT))
        self.assertIsNone(li.forced_exit(self.db, {MINT: .001}, now=1000))

    def test_unpromoted_autotune_cannot_change_live_exit(self):
        params = {
            "take_profit_pct": .30, "stop_loss_pct": -.12,
            "trail_arm_pct": .25, "trail_distance_pct": .10,
            "max_hold_seconds": 1800, "mode": "MEME",
        }
        for promoted, expected in ((False, "take_profit"), (True, None)):
            with self.subTest(promoted=promoted):
                db = self.db.with_name(f"promotion-{promoted}.db")
                li.record_fill(db, intent(), {
                    "verified": True, "input_atomic": 1_000_000,
                    "output_atomic": 1_000_000_000,
                }, ticker="PEPE", decimals=6, price_usd=.001, now=1000)
                li.set_risk_params_override(
                    db, params, source="approved:test" if promoted else "autotuner@test",
                    sample_n=50)
                cfg = {"live": {"autonomous": {
                    "autotune_live_promotion_enabled": promoted,
                }}}
                decision = li.forced_exit(db, {MINT: .00125}, now=1100, cfg=cfg)
                self.assertEqual(decision and decision["exit_reason"], expected)

    def test_config_flip_alone_cannot_promote_unapproved_candidate(self):
        li.record_fill(self.db, intent(), {
            "verified": True, "input_atomic": 1_000_000,
            "output_atomic": 1_000_000_000,
        }, ticker="PEPE", decimals=6, price_usd=.001, now=1000)
        li.set_risk_params_override(self.db, {
            "take_profit_pct": .30, "stop_loss_pct": -.12,
            "trail_arm_pct": .25, "trail_distance_pct": .10,
            "max_hold_seconds": 1800, "mode": "MEME",
        }, source="autotuner@1000", sample_n=50)
        cfg = {"live": {"autonomous": {"autotune_live_promotion_enabled": True}}}
        decision = li.forced_exit(self.db, {MINT: .00125}, now=1100, cfg=cfg)
        self.assertEqual(decision["exit_reason"], "take_profit")

    def test_enabled_approved_override_controls_live_exit(self):
        li.record_fill(self.db, intent(), {
            "verified": True, "input_atomic": 1_000_000,
            "output_atomic": 1_000_000_000,
        }, ticker="PEPE", decimals=6, price_usd=.001, now=1000)
        li.set_risk_params_override(self.db, {
            "take_profit_pct": .30, "stop_loss_pct": -.12,
            "trail_arm_pct": .25, "trail_distance_pct": .10,
            "max_hold_seconds": 1800, "mode": "MEME",
        }, source="approved:test", sample_n=50)
        cfg = {"live": {"autonomous": {"autotune_live_promotion_enabled": True}}}
        self.assertIsNone(li.forced_exit(self.db, {MINT: .00125}, now=1100, cfg=cfg))
        decision = li.forced_exit(self.db, {MINT: .00131}, now=1100, cfg=cfg)
        self.assertEqual(decision["exit_reason"], "take_profit")


if __name__ == "__main__":
    unittest.main()
