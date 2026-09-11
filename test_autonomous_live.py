import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import autonomous_live as al
import live_signer_worker as worker


CFG = {
    "coins": [
        {"symbol": "SOL", "mint": al.NATIVE_SOL_MINT},
        {"symbol": "JUP", "mint": "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN"},
        {"symbol": "ETH", "mint": "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs"},
    ],
    "live": {
        "reserve_mint": al.USDC_MINT,
        "autonomous": {
            "enabled": True,
            "model": "deepseek/deepseek-v4-flash-0731",
            "max_buy_usdc": 1.0,
            "min_confidence": 0.70,
            "cycle_seconds": 900,
        },
    },
}


class AutonomousLiveTests(unittest.TestCase):
    def test_tradeable_universe_excludes_sol_and_usdc(self):
        universe = al.tradeable_universe(CFG)
        self.assertEqual([x["symbol"] for x in universe], ["JUP", "ETH"])

    def test_invalid_or_extra_model_fields_fail_closed(self):
        with self.assertRaises(al.DecisionDenied):
            al.validate_decision({"action": "BUY", "symbol": "JUP", "confidence": .8,
                                  "expected_reward_nzd": .3, "expected_loss_nzd": .1,
                                  "amount_usdc": 999}, CFG)

    def test_model_cannot_trade_unapproved_symbol(self):
        with self.assertRaises(al.DecisionDenied):
            al.validate_decision({"action": "BUY", "symbol": "BONK", "confidence": .8,
                                  "expected_reward_nzd": .3, "expected_loss_nzd": .1}, CFG)

    def test_buy_requires_qualified_evidence(self):
        decision = {"action": "BUY", "symbol": "JUP", "confidence": .8,
                    "expected_reward_nzd": .3, "expected_loss_nzd": .1}
        executor = Mock()
        result = al.apply_decision(
            decision, CFG,
            balances={"USDC": 33.0, "SOL": .04, "JUP": 0.0, "ETH": 0.0},
            evidence={"qualified": False, "reason": "gate_failed"},
            executor=executor,
            now=1800,
        )
        self.assertEqual(result["state"], "HOLD")
        self.assertEqual(result["reason"], "strategy_evidence_not_qualified")
        executor.assert_not_called()

    def test_buy_requires_selected_symbol_evidence(self):
        executor = Mock()
        result = al.apply_decision(
            {"action": "BUY", "symbol": "JUP", "confidence": .8,
             "expected_reward_nzd": .3, "expected_loss_nzd": .1},
            CFG, balances={"USDC": 33.0, "SOL": .04, "JUP": 0.0, "ETH": 0.0},
            evidence={"qualified": True, "by_symbol": {"JUP": {"qualified": False}}},
            executor=executor, now=1800,
        )
        self.assertEqual(result["reason"], "strategy_evidence_not_qualified")
        executor.assert_not_called()

    def test_stale_market_context_is_rejected(self):
        with self.assertRaisesRegex(al.DecisionDenied, "stale"):
            al.validate_market_context(
                {"assets": {"JUP": {"latest_usd": .2, "samples": 20, "latest_ts": 1000},
                            "ETH": {"latest_usd": 2000, "samples": 20, "latest_ts": 1000}}},
                CFG, now=1401,
            )

    def test_buy_size_is_deterministic_and_not_model_controlled(self):
        decision = {"action": "BUY", "symbol": "JUP", "confidence": .8,
                    "expected_reward_nzd": .3, "expected_loss_nzd": .1}
        executor = Mock(return_value={"state": "RECONCILED", "signature": "sig"})
        result = al.apply_decision(
            decision, CFG,
            balances={"USDC": 33.0, "SOL": .04, "JUP": 0.0, "ETH": 0.0},
            evidence={"qualified": True, "reason": "passed",
                      "by_symbol": {"JUP": {"qualified": True}}},
            executor=executor,
            now=1800,
        )
        intent = executor.call_args.args[1]
        self.assertEqual(intent.side, "BUY")
        self.assertEqual(intent.amount_atomic, 1_000_000)
        self.assertEqual(intent.order_id, "auto:2:JUP:BUY")
        self.assertEqual(result["state"], "RECONCILED")

    def test_sell_is_allowed_for_risk_reduction_when_entry_gate_fails(self):
        decision = {"action": "SELL", "symbol": "JUP", "confidence": .9,
                    "expected_reward_nzd": .2, "expected_loss_nzd": .1}
        executor = Mock(return_value={"state": "RECONCILED", "signature": "sig"})
        al.apply_decision(
            decision, CFG,
            balances={"USDC": 33.0, "SOL": .04, "JUP": 4.391346, "ETH": 0.0},
            evidence={"qualified": False, "reason": "gate_failed"},
            executor=executor,
            now=1800,
        )
        intent = executor.call_args.args[1]
        self.assertEqual(intent.side, "SELL")
        self.assertEqual(intent.input_mint, CFG["coins"][1]["mint"])
        self.assertEqual(intent.output_mint, al.USDC_MINT)
        self.assertEqual(intent.amount_atomic, 4_391_346)

    def test_sell_with_no_inventory_holds(self):
        executor = Mock()
        result = al.apply_decision(
            {"action": "SELL", "symbol": "JUP", "confidence": .9,
             "expected_reward_nzd": .2, "expected_loss_nzd": .1},
            CFG,
            balances={"USDC": 33.0, "SOL": .04, "JUP": 0.0, "ETH": 0.0},
            evidence={"qualified": True}, executor=executor, now=1800,
        )
        self.assertEqual(result["state"], "HOLD")
        self.assertEqual(result["reason"], "no_inventory")
        executor.assert_not_called()

    def test_low_fee_reserve_holds_before_execution(self):
        executor = Mock()
        result = al.apply_decision(
            {"action": "BUY", "symbol": "JUP", "confidence": .9,
             "expected_reward_nzd": .3, "expected_loss_nzd": .1},
            CFG,
            balances={"USDC": 33.0, "SOL": .009, "JUP": 0.0, "ETH": 0.0},
            evidence={"qualified": True}, executor=executor, now=1800,
        )
        self.assertEqual(result["reason"], "fee_reserve_below_minimum")
        executor.assert_not_called()

    def test_model_failure_becomes_hold_and_is_logged(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = al.run_cycle(
                CFG,
                model_call=lambda *_: (_ for _ in ()).throw(RuntimeError("provider")),
                balance_reader=lambda *_: {"USDC": 33.0, "SOL": .04, "JUP": 4.0, "ETH": 0.0},
                evidence_reader=lambda *_: {"qualified": True},
                executor=Mock(),
                log_path=Path(tmp) / "cycles.jsonl",
                now=1800,
            )
            self.assertEqual(result["state"], "HOLD")
            self.assertEqual(result["reason"], "model_or_validation_failure")
            logged = json.loads((Path(tmp) / "cycles.jsonl").read_text().strip())
            self.assertEqual(logged["state"], "HOLD")
            self.assertNotIn("provider", json.dumps(logged))

    def test_queue_contains_only_validated_intent_and_is_idempotent(self):
        decision = {"action": "BUY", "symbol": "JUP", "confidence": .8,
                    "expected_reward_nzd": .3, "expected_loss_nzd": .1}
        intent = al.build_intent(decision, CFG, {"USDC": 33.0}, 1800)
        with tempfile.TemporaryDirectory() as tmp, \
                patch.dict("os.environ", {"MULTIHEDGE_LIVE_QUEUE": tmp}):
            first = al.queue_intent(CFG, intent)
            second = al.queue_intent(CFG, intent)
            self.assertFalse(first["duplicate"])
            self.assertTrue(second["duplicate"])
            files = list((Path(tmp) / "pending").glob("*.json"))
            self.assertEqual(len(files), 1)
            self.assertEqual(worker._load_intent(files[0]), intent)
            self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)

    def test_signer_rechecks_symbol_and_aggregate_evidence(self):
        buy = al.build_intent(
            {"action": "BUY", "symbol": "JUP", "confidence": .8,
             "expected_reward_nzd": .3, "expected_loss_nzd": .1},
            CFG, {"USDC": 33.0}, 1800,
        )
        self.assertFalse(worker.entry_authorized(
            CFG, buy, {"qualified": True, "by_symbol": {"JUP": {"qualified": False}}}
        ))
        self.assertTrue(worker.entry_authorized(
            CFG, buy, {"qualified": True, "by_symbol": {"JUP": {"qualified": True}}}
        ))

    def test_signer_rejects_tampered_queue_payload(self):
        decision = {"action": "BUY", "symbol": "JUP", "confidence": .8,
                    "expected_reward_nzd": .3, "expected_loss_nzd": .1}
        intent = al.build_intent(decision, CFG, {"USDC": 33.0}, 1800)
        with tempfile.TemporaryDirectory() as tmp, \
                patch.dict("os.environ", {"MULTIHEDGE_LIVE_QUEUE": tmp}):
            al.queue_intent(CFG, intent)
            path = next((Path(tmp) / "pending").glob("*.json"))
            path.write_text(path.read_text().replace("1000000", "9000000"))
            with self.assertRaisesRegex(Exception, "hash mismatch"):
                worker._load_intent(path)

    def test_signer_rejects_stale_or_forged_order_bucket(self):
        intent = al.build_intent(
            {"action": "BUY", "symbol": "JUP", "confidence": .8,
             "expected_reward_nzd": .3, "expected_loss_nzd": .1},
            CFG, {"USDC": 33.0}, 1800,
        )
        worker.validate_autonomous_order_id(CFG, intent, 1800)
        with self.assertRaisesRegex(Exception, "invalid or stale"):
            worker.validate_autonomous_order_id(CFG, intent, 7200)


if __name__ == "__main__":
    unittest.main()
