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

    def test_take_profit_stop_loss_and_max_hold_force_sell(self):
        # Meme thresholds: 20% TP, -10% SL, 900s max hold (entry .001).
        cases = [(.00122, 1060, "take_profit"), (.00089, 1060, "stop_loss"),
                 (.001, 1901, "max_hold")]
        for price, now, reason in cases:
            with self.subTest(reason=reason):
                db = self.db.with_name(reason + ".db")
                li.record_fill(db, intent(), {"verified": True, "input_atomic": 1_000_000,
                                              "output_atomic": 1_000_000_000},
                               ticker="PEPE", decimals=6, price_usd=.001, now=1000)
                decision = li.forced_exit(db, {MINT: price}, now=now)
                self.assertEqual(decision["symbol"], MINT)
                self.assertEqual(decision["exit_reason"], reason)

    def test_unregistered_wallet_token_cannot_be_treated_as_bot_inventory(self):
        self.assertIsNone(li.get_holding(self.db, MINT))
        self.assertIsNone(li.forced_exit(self.db, {MINT: .001}, now=1000))


if __name__ == "__main__":
    unittest.main()
