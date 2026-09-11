import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import autonomous_live as al
import live_signer_worker as worker
import live_inventory
from execution_policy import TradeIntent

DYNAMIC_MINT = "B5WTLaRwaUQpKk7ir1wniNB6m5o8GgMrimhKMYan2R6B"


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
            "model": al.AUTONOMOUS_MODEL,
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

    def test_dynamic_token_uses_mint_as_identity_and_can_build_intent(self):
        cfg = al.with_runtime_coins(CFG, [{
            "symbol": DYNAMIC_MINT, "ticker": "PEPE", "mint": DYNAMIC_MINT,
            "decimals": 6, "entry_eligible": True,
        }])
        decision = {"action": "BUY", "symbol": DYNAMIC_MINT, "confidence": .8,
                    "expected_reward_nzd": .3, "expected_loss_nzd": .1}
        validated = al.validate_decision(decision, cfg)
        intent = al.build_intent(validated, cfg, {"USDC": 33.0}, 1800)
        self.assertEqual(intent.output_mint, DYNAMIC_MINT)
        self.assertEqual(intent.order_id, f"auto:2:{DYNAMIC_MINT}:BUY")

    def test_dynamic_buy_requires_mint_keyed_evidence(self):
        cfg = al.with_runtime_coins(CFG, [{
            "symbol": DYNAMIC_MINT, "ticker": "PEPE", "mint": DYNAMIC_MINT,
            "decimals": 6, "entry_eligible": True,
        }])
        executor = Mock(return_value={"state": "QUEUED"})
        result = al.apply_decision(
            {"action": "BUY", "symbol": DYNAMIC_MINT, "confidence": .8,
             "expected_reward_nzd": .3, "expected_loss_nzd": .1},
            cfg, balances={"USDC": 33.0, "SOL": .04, DYNAMIC_MINT: 0},
            evidence={"qualified": True, "by_symbol": {DYNAMIC_MINT: {"qualified": True}},
                      "dynamic_by_symbol": {DYNAMIC_MINT: {"qualified": True}},
                      "dynamic_strategy": {"qualified": True}},
            executor=executor, now=1800,
        )
        self.assertEqual(result["state"], "QUEUED")

    def test_dynamic_market_context_uses_fresh_discovery_snapshot(self):
        candidate = {
            "symbol": DYNAMIC_MINT, "ticker": "PEPE", "mint": DYNAMIC_MINT,
            "decimals": 6, "entry_eligible": True,
            "market": {"latest_usd": .001, "latest_ts": 1800, "samples": 2,
                       "return_5m_pct": 2.0},
        }
        cfg = al.with_runtime_coins(CFG, [candidate])
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "evidence.db"
            with sqlite3.connect(db) as con:
                con.execute("CREATE TABLE mh_pxhist(coin TEXT,ts REAL,px REAL)")
                for symbol, price in (("JUP", .2), ("ETH", 2000)):
                    con.executemany("INSERT INTO mh_pxhist VALUES(?,?,?)",
                                    [(symbol, 1790, price), (symbol, 1800, price)])
            with patch.dict("os.environ", {"MULTIHEDGE_EVIDENCE_DB": str(db)}):
                context = al.market_context(cfg, {"USDC": 33, "SOL": .04})
        self.assertEqual(context["assets"][DYNAMIC_MINT]["latest_usd"], .001)
        al.validate_market_context(context, cfg, now=1800)

    def test_dynamic_evidence_is_keyed_by_mint_not_duplicate_ticker(self):
        cfg = al.with_runtime_coins(CFG, [{
            "symbol": DYNAMIC_MINT, "ticker": "PEPE", "mint": DYNAMIC_MINT,
            "decimals": 6, "entry_eligible": True,
        }])
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "evidence.db"
            with sqlite3.connect(db) as con:
                con.execute("CREATE TABLE mh_trades(coin TEXT,symbol TEXT,setup TEXT,realized_pct REAL,realized_usd REAL,close_ts REAL)")
                con.executemany("INSERT INTO mh_trades VALUES(?,?,?,?,?,?)",
                                [(DYNAMIC_MINT, "PEPE", "dynamic_scalper", .01, .01, 900)] * 20
                                + [("JUP", "JUP", "momentum_breakout", .01, .01, 900)] * 30)
            with patch.dict("os.environ", {"MULTIHEDGE_EVIDENCE_DB": str(db)}):
                evidence = al.strategy_evidence(cfg, now=1000)
        self.assertEqual(evidence["by_symbol"][DYNAMIC_MINT]["n"], 20)
        self.assertTrue(evidence["by_symbol"][DYNAMIC_MINT]["qualified"])

    def test_dynamic_strategy_must_qualify_before_any_new_mint_can_enter(self):
        cfg = al.with_runtime_coins(CFG, [{
            "symbol": DYNAMIC_MINT, "ticker": "PEPE", "mint": DYNAMIC_MINT,
            "decimals": 6, "entry_eligible": True,
        }])
        cfg["live"]["autonomous"]["dynamic_universe"] = {
            "minimum_strategy_trades": 3, "minimum_strategy_win_rate": .6667,
            "minimum_strategy_net_usd": 0,
        }
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "evidence.db"
            with sqlite3.connect(db) as con:
                con.execute("CREATE TABLE mh_trades(coin TEXT,symbol TEXT,setup TEXT,realized_pct REAL,realized_usd REAL,close_ts REAL)")
                con.executemany("INSERT INTO mh_trades VALUES(?,?,?,?,?,?)", [
                    ("mint-one", "ONE", "dynamic_scalper", .03, .03, 900),
                    ("mint-two", "TWO", "dynamic_scalper", .03, .03, 900),
                    ("mint-three", "THREE", "dynamic_scalper", -.02, -.02, 900),
                ])
            with patch.dict("os.environ", {"MULTIHEDGE_EVIDENCE_DB": str(db)}):
                evidence = al.strategy_evidence(cfg, now=1000)
        self.assertFalse(evidence["dynamic_strategy"]["qualified"])
        self.assertFalse(al.entry_evidence_qualified(cfg, DYNAMIC_MINT, evidence))
        evidence["dynamic_strategy"].update(win_rate=.67, qualified=True)
        self.assertFalse(al.entry_evidence_qualified(cfg, DYNAMIC_MINT, evidence))

    def test_dynamic_entry_requires_selected_mint_evidence_not_only_aggregate(self):
        cfg = al.with_runtime_coins(CFG, [{
            "symbol": DYNAMIC_MINT, "ticker": "PEPE", "mint": DYNAMIC_MINT,
            "decimals": 6, "entry_eligible": True,
        }])
        evidence = {
            "qualified": True,
            "dynamic_strategy": {"qualified": True},
            "by_symbol": {DYNAMIC_MINT: {"qualified": False, "n": 0}},
            "dynamic_by_symbol": {DYNAMIC_MINT: {"qualified": False, "n": 0}},
        }
        self.assertFalse(al.entry_evidence_qualified(cfg, DYNAMIC_MINT, evidence))

    def test_stale_trade_history_cannot_qualify_entry(self):
        cfg = al.with_runtime_coins(CFG, [{
            "symbol": DYNAMIC_MINT, "ticker": "PEPE", "mint": DYNAMIC_MINT,
            "decimals": 6, "entry_eligible": True,
        }])
        cfg["live"]["autonomous"]["evidence_max_age_seconds"] = 100
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "evidence.db"
            with sqlite3.connect(db) as con:
                con.execute("CREATE TABLE mh_trades(coin TEXT,symbol TEXT,setup TEXT,realized_pct REAL,realized_usd REAL,close_ts REAL)")
                con.executemany("INSERT INTO mh_trades VALUES(?,?,?,?,?,?)", [
                    (DYNAMIC_MINT, "PEPE", "dynamic_scalper", .03, .03, 1000)
                ] * 60)
            with patch.dict("os.environ", {"MULTIHEDGE_EVIDENCE_DB": str(db)}):
                evidence = al.strategy_evidence(cfg, now=2000)
        self.assertEqual(evidence["n"], 0)
        self.assertFalse(evidence["dynamic_strategy"]["qualified"])

    @patch("live_signer_worker.verify_onchain_mint")
    @patch("live_signer_worker.verify_round_trip")
    @patch("live_signer_worker.verify_token")
    def test_signer_independently_admits_dynamic_buy(self, verify_token, verify_round_trip,
                                                     verify_onchain):
        verify_token.return_value = {"symbol": DYNAMIC_MINT, "ticker": "PEPE",
                                     "mint": DYNAMIC_MINT, "decimals": 6,
                                     "entry_eligible": True}
        intent = al.build_intent(
            {"action": "BUY", "symbol": DYNAMIC_MINT, "confidence": .8,
             "expected_reward_nzd": .3, "expected_loss_nzd": .1},
            al.with_runtime_coins(CFG, [verify_token.return_value]), {"USDC": 33}, 1800,
        )
        enriched = worker.enrich_dynamic_intent(CFG, intent, api_key="key", rpc_url="rpc", now=1800)
        self.assertEqual(al.tradeable_universe(enriched)[-1]["mint"], DYNAMIC_MINT)
        verify_round_trip.assert_called_once()
        verify_onchain.assert_called_once_with(DYNAMIC_MINT, 6, "rpc")

    def test_signer_allows_only_registered_dynamic_sell(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "evidence.db"
            buy = TradeIntent("buy", "BUY", al.USDC_MINT, DYNAMIC_MINT, 1_000_000,
                              50, "dynamic_scalper", ".03", ".01")
            live_inventory.record_fill(
                db, buy, {"verified": True, "input_atomic": 1_000_000,
                          "output_atomic": 1_000_000_000},
                ticker="PEPE", decimals=6, price_usd=.001, now=1000,
            )
            sell = TradeIntent(f"auto:2:{DYNAMIC_MINT}:SELL", "SELL", DYNAMIC_MINT,
                               al.USDC_MINT, 1_000_000_000, 50,
                               "nemotron_3_super_autonomous", "0", "0")
            with patch.dict("os.environ", {"MULTIHEDGE_EVIDENCE_DB": str(db)}):
                enriched = worker.enrich_dynamic_intent(
                    CFG, sell, api_key="key", rpc_url="rpc", now=1800
                )
            self.assertEqual(al.tradeable_universe(enriched)[-1]["mint"], DYNAMIC_MINT)

    def test_forced_exit_file_accepts_only_registered_sell(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "forced.json"
            path.write_text(json.dumps({"action": "SELL", "symbol": DYNAMIC_MINT,
                                        "confidence": 1.0, "expected_reward_nzd": 0,
                                        "expected_loss_nzd": 0, "exit_reason": "stop_loss"}))
            decision = al.load_forced_exit(path, {DYNAMIC_MINT})
            self.assertEqual(decision["action"], "SELL")
            self.assertNotIn("exit_reason", decision)
            path.write_text(json.dumps({"action": "BUY", "symbol": DYNAMIC_MINT}))
            self.assertIsNone(al.load_forced_exit(path, {DYNAMIC_MINT}))

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

    def test_risk_reducing_sell_does_not_require_buy_reward_ratio(self):
        result = al.validate_decision(
            {"action": "SELL", "symbol": "JUP", "confidence": .9,
             "expected_reward_nzd": 0, "expected_loss_nzd": 0}, CFG
        )
        self.assertEqual(result["action"], "SELL")

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
