import tempfile
import unittest
from pathlib import Path

import execution_policy as ep


WALLET = "CqsTCGDXQBeGUAPXHtGDFZ3cU1pqMWiuf9B6hxAZqaxw"
SOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
JUP = "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN"


def valid_intent():
    return ep.TradeIntent(
        order_id="strategy-1:USDC-JUP:buy:001",
        side="BUY",
        input_mint=USDC,
        output_mint=JUP,
        amount_atomic=1_000_000,
        slippage_bps=50,
        strategy="qualified_strategy_v1",
        expected_reward_nzd="0.30",
        expected_loss_nzd="0.10",
    )


def ix(program_id, signer=False, pubkey=WALLET):
    return {
        "programId": program_id,
        "accounts": [{"pubkey": pubkey, "isSigner": signer, "isWritable": True}],
        "data": "AA==",
    }


def valid_build():
    return {
        "inputMint": USDC,
        "outputMint": JUP,
        "inAmount": "1000000",
        "outAmount": "500000",
        "otherAmountThreshold": "497500",
        "slippageBps": 50,
        "priceImpactPct": "0.001",
        "computeBudgetInstructions": [ix(ep.COMPUTE_BUDGET_PROGRAM)],
        "setupInstructions": [ix(ep.ASSOCIATED_TOKEN_PROGRAM)],
        "swapInstruction": ix(ep.JUPITER_V6_PROGRAM, signer=True),
        "cleanupInstruction": None,
        "otherInstructions": [],
        "tipInstruction": None,
        "addressesByLookupTableAddress": {},
        "blockhashWithMetadata": {"blockhash": [1] * 32, "lastValidBlockHeight": 1200},
    }


class ExecutionPolicyTests(unittest.TestCase):
    def test_valid_buy_intent_and_build_pass(self):
        intent = valid_intent()
        ep.validate_intent(intent, {USDC, JUP})
        approved = ep.validate_build(intent, valid_build(), WALLET, current_block_height=1000)
        self.assertEqual(approved.min_output_atomic, 497500)
        self.assertEqual(approved.program_ids, (
            ep.COMPUTE_BUDGET_PROGRAM,
            ep.ASSOCIATED_TOKEN_PROGRAM,
            ep.JUPITER_V6_PROGRAM,
        ))

    def test_sell_to_usdc_is_supported(self):
        intent = valid_intent()
        intent = ep.TradeIntent(**{**intent.__dict__, "side": "SELL", "input_mint": JUP, "output_mint": USDC})
        build = valid_build()
        build.update(inputMint=JUP, outputMint=USDC)
        ep.validate_intent(intent, {USDC, JUP})
        ep.validate_build(intent, build, WALLET, current_block_height=1000)

    def test_every_trade_must_use_usdc_as_one_side(self):
        intent = valid_intent()
        with self.assertRaisesRegex(ep.PolicyDenied, "USDC"):
            ep.validate_intent(
                ep.TradeIntent(**{**intent.__dict__, "input_mint": SOL}),
                {SOL, USDC, JUP},
            )
        with self.assertRaisesRegex(ep.PolicyDenied, "BUY"):
            ep.validate_intent(
                ep.TradeIntent(**{**intent.__dict__, "input_mint": JUP, "output_mint": USDC}),
                {USDC, JUP},
            )

    def test_sol_is_fees_only_and_never_a_trading_leg(self):
        intent = valid_intent()
        with self.assertRaisesRegex(ep.PolicyDenied, "fees-only"):
            ep.validate_intent(
                ep.TradeIntent(**{**intent.__dict__, "output_mint": SOL}),
                {SOL, USDC, JUP},
            )
        with self.assertRaisesRegex(ep.PolicyDenied, "fees-only"):
            ep.validate_intent(
                ep.TradeIntent(**{**intent.__dict__, "side": "SELL", "input_mint": SOL,
                                  "output_mint": USDC}),
                {SOL, USDC, JUP},
            )

    def test_operational_sol_reserve_has_measured_headroom(self):
        self.assertEqual(ep.MIN_FEE_RESERVE_SOL, ep.Decimal("0.01"))
        measured = ep.calculate_fee_reserve_sol(
            priority_micro_lamports_per_cu=0,
            transactions=10,
            compute_units_per_transaction=200_000,
            token_account_rent_lamports=1_855_569,
            token_accounts=2,
            contingency_multiplier="2",
        )
        self.assertEqual(measured, ep.Decimal("0.01"))

    def test_unknown_program_is_denied(self):
        build = valid_build()
        build["otherInstructions"] = [ix("Unknown111111111111111111111111111111111")]
        with self.assertRaisesRegex(ep.PolicyDenied, "program"):
            ep.validate_build(valid_intent(), build, WALLET, 1000)

    def test_extra_signer_is_denied(self):
        build = valid_build()
        build["setupInstructions"][0]["accounts"].append({
            "pubkey": "Other1111111111111111111111111111111111", "isSigner": True, "isWritable": True
        })
        with self.assertRaisesRegex(ep.PolicyDenied, "signer"):
            ep.validate_build(valid_intent(), build, WALLET, 1000)

    def test_tip_and_other_instructions_are_denied(self):
        build = valid_build()
        build["tipInstruction"] = ix(ep.SYSTEM_PROGRAM)
        with self.assertRaisesRegex(ep.PolicyDenied, "tip"):
            ep.validate_build(valid_intent(), build, WALLET, 1000)

    def test_expired_or_long_lived_quote_is_denied(self):
        build = valid_build()
        build["blockhashWithMetadata"]["lastValidBlockHeight"] = 1000
        with self.assertRaisesRegex(ep.PolicyDenied, "expired"):
            ep.validate_build(valid_intent(), build, WALLET, 1000)
        build["blockhashWithMetadata"]["lastValidBlockHeight"] = 1401
        with self.assertRaisesRegex(ep.PolicyDenied, "expiry"):
            ep.validate_build(valid_intent(), build, WALLET, 1000)

    def test_slippage_price_impact_and_reward_risk_are_enforced(self):
        intent = valid_intent()
        with self.assertRaisesRegex(ep.PolicyDenied, "slippage"):
            ep.validate_intent(ep.TradeIntent(**{**intent.__dict__, "slippage_bps": 51}), {USDC, JUP})
        with self.assertRaisesRegex(ep.PolicyDenied, "reward"):
            ep.validate_intent(ep.TradeIntent(**{**intent.__dict__, "expected_reward_nzd": "0.19"}), {USDC, JUP})
        build = valid_build()
        build["priceImpactPct"] = "0.0076"
        with self.assertRaisesRegex(ep.PolicyDenied, "impact"):
            ep.validate_build(intent, build, WALLET, 1000)

    def test_protected_floor_and_fee_reserve_are_enforced(self):
        ep.validate_reserves("81.23", "78.00", "0.030")
        with self.assertRaisesRegex(ep.PolicyDenied, "protected"):
            ep.validate_reserves("81.23", "59.99", "0.030")
        with self.assertRaisesRegex(ep.PolicyDenied, "fee reserve"):
            ep.validate_reserves("81.23", "78.00", "0.0099")

    def test_order_store_is_idempotent_and_ambiguous_orders_never_resend(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ep.OrderStore(Path(tmp) / "orders.db")
            intent = valid_intent()
            self.assertTrue(store.reserve(intent))
            self.assertFalse(store.reserve(intent))
            store.mark_submitted(intent.order_id, "signature-one")
            self.assertFalse(store.reserve(intent))
            store.mark_reconciled(intent.order_id, {"input_delta": -1000000, "output_delta": 497500})
            self.assertEqual(store.get(intent.order_id)["state"], "RECONCILED")

    def test_swap_instruction_must_be_jupiter_and_require_wallet_signature(self):
        build = valid_build()
        build["swapInstruction"]["programId"] = ep.TOKEN_PROGRAM
        with self.assertRaisesRegex(ep.PolicyDenied, "Jupiter V6"):
            ep.validate_build(valid_intent(), build, WALLET, 1000)
        build = valid_build()
        build["swapInstruction"]["accounts"][0]["isSigner"] = False
        with self.assertRaisesRegex(ep.PolicyDenied, "wallet signer"):
            ep.validate_build(valid_intent(), build, WALLET, 1000)

    def test_usdc_route_denies_other_instructions(self):
        build = valid_build()
        build["otherInstructions"] = [ix(ep.COMPUTE_BUDGET_PROGRAM)]
        with self.assertRaisesRegex(ep.PolicyDenied, "other"):
            ep.validate_build(valid_intent(), build, WALLET, 1000)

    def test_restart_reopens_ledger_and_locks_orphaned_orders(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "orders.db"
            first = ep.OrderStore(path)
            intent = valid_intent()
            self.assertTrue(first.reserve(intent))
            second = ep.OrderStore(path)
            self.assertFalse(second.reserve(intent))
            self.assertEqual(second.recover_orphans(), 1)
            self.assertEqual(second.get(intent.order_id)["state"], "REQUIRES_HUMAN")


if __name__ == "__main__":
    unittest.main()
