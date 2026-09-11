import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from survival_policy import (
    AuditStore,
    classify_survival_state,
    evaluate_new_position,
    max_new_position_nzd,
)


class SurvivalStateTests(unittest.TestCase):
    def test_shadow_only_overrides_verified_treasury_state(self):
        result = classify_survival_state(
            treasury_nzd=Decimal("100.00"),
            data_verified=True,
            shadow_only=True,
            independent_below_death_checks=0,
        )

        self.assertEqual(result.state, "SHADOW_ONLY")
        self.assertEqual(result.protected_reserve_nzd, Decimal("60.00"))
        self.assertEqual(result.risk_capital_nzd, Decimal("40.00"))

    def test_unverified_data_is_unknown_not_dead(self):
        result = classify_survival_state(
            treasury_nzd=None,
            data_verified=False,
            shadow_only=False,
            independent_below_death_checks=2,
        )

        self.assertEqual(result.state, "UNKNOWN")
        self.assertIsNone(result.treasury_nzd)
        self.assertEqual(result.risk_capital_nzd, Decimal("0.00"))

    def test_death_requires_two_independent_checks(self):
        first = classify_survival_state(
            treasury_nzd=Decimal("39.99"),
            data_verified=True,
            shadow_only=False,
            independent_below_death_checks=1,
        )
        second = classify_survival_state(
            treasury_nzd=Decimal("39.99"),
            data_verified=True,
            shadow_only=False,
            independent_below_death_checks=2,
        )

        self.assertEqual(first.state, "UNKNOWN")
        self.assertEqual(second.state, "DEAD")

    def test_exact_survival_boundaries(self):
        cases = {
            Decimal("40.00"): "DISTRESS",
            Decimal("60.00"): "CONSERVATION",
            Decimal("70.00"): "NORMAL",
            Decimal("90.00"): "GROWTH",
        }

        for treasury, expected in cases.items():
            with self.subTest(treasury=treasury):
                result = classify_survival_state(
                    treasury_nzd=treasury,
                    data_verified=True,
                    shadow_only=False,
                    independent_below_death_checks=0,
                )
                self.assertEqual(result.state, expected)


class PositionPolicyTests(unittest.TestCase):
    def test_new_position_cap_uses_most_restrictive_limit(self):
        state = classify_survival_state(
            treasury_nzd=Decimal("100.00"),
            data_verified=True,
            shadow_only=False,
            independent_below_death_checks=0,
        )

        cap = max_new_position_nzd(
            state,
            current_token_exposure_nzd=Decimal("3.00"),
            aggregate_exposure_nzd=Decimal("12.00"),
        )

        self.assertEqual(cap, Decimal("3.00"))

    def test_stale_data_denies_position(self):
        state = classify_survival_state(
            treasury_nzd=Decimal("100.00"),
            data_verified=True,
            shadow_only=False,
            independent_below_death_checks=0,
        )

        decision = evaluate_new_position(
            state,
            proposed_position_nzd=Decimal("2.00"),
            current_token_exposure_nzd=Decimal("0.00"),
            aggregate_exposure_nzd=Decimal("0.00"),
            expected_net_reward_nzd=Decimal("1.00"),
            expected_loss_nzd=Decimal("0.40"),
            daily_loss_nzd=Decimal("0.00"),
            data_fresh=False,
            controls_healthy=True,
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "STALE_OR_UNVERIFIED_STATE")

    def test_valid_growth_position_is_advisory_only(self):
        state = classify_survival_state(
            treasury_nzd=Decimal("100.00"),
            data_verified=True,
            shadow_only=False,
            independent_below_death_checks=0,
        )

        decision = evaluate_new_position(
            state,
            proposed_position_nzd=Decimal("4.00"),
            current_token_exposure_nzd=Decimal("0.00"),
            aggregate_exposure_nzd=Decimal("0.00"),
            expected_net_reward_nzd=Decimal("1.00"),
            expected_loss_nzd=Decimal("0.40"),
            daily_loss_nzd=Decimal("0.00"),
            data_fresh=True,
            controls_healthy=True,
        )

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.max_position_nzd, Decimal("5.00"))
        self.assertFalse(decision.signing_authorized)


class AuditStoreTests(unittest.TestCase):
    def test_store_creates_missing_parent_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "survival.db"
            AuditStore(path)
            self.assertTrue(path.exists())

    def test_cycle_is_persisted_and_cannot_be_changed_or_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = AuditStore(Path(tmp) / "survival.db")
            cycle_id = store.append_cycle(
                {
                    "state": "SHADOW_ONLY",
                    "treasury_nzd": None,
                    "protected_reserve_nzd": "60.00",
                    "risk_capital_nzd": "0.00",
                    "data_freshness": "UNVERIFIED",
                    "highest_value_goal": "build deterministic controls",
                    "planned_actions": ["run tests"],
                    "policy_checks": ["mainnet disabled"],
                    "actions_completed": ["policy core created"],
                    "external_readbacks": [],
                    "cost_nzd": "0.00",
                    "revenue_nzd": "0.00",
                    "pnl_nzd": "0.00",
                    "risk_change": "reduced",
                    "n8n_changes": [],
                    "messages_sent": [],
                    "failures": [],
                    "next_wake_reason": "continue boot sequence",
                    "verdict": "CYCLE: SAFE PROGRESS, NO REVENUE YET",
                }
            )

            saved = store.get_cycle(cycle_id)
            self.assertEqual(saved["state"], "SHADOW_ONLY")
            self.assertEqual(saved["verdict"], "CYCLE: SAFE PROGRESS, NO REVENUE YET")
            with self.assertRaises(PermissionError):
                store.replace_cycle(cycle_id, {"state": "GROWTH"})
            with self.assertRaises(PermissionError):
                store.delete_cycle(cycle_id)

    def test_verify_chain_detects_payload_tampering(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "survival.db"
            store = AuditStore(path)
            cycle_id = store.append_cycle(
                {
                    "state": "SHADOW_ONLY",
                    "treasury_nzd": None,
                    "protected_reserve_nzd": "60.00",
                    "risk_capital_nzd": "0.00",
                    "data_freshness": "UNVERIFIED",
                    "highest_value_goal": "verify audit integrity",
                    "planned_actions": [],
                    "policy_checks": [],
                    "actions_completed": [],
                    "external_readbacks": [],
                    "cost_nzd": "0.00",
                    "revenue_nzd": "0.00",
                    "pnl_nzd": None,
                    "risk_change": "unchanged",
                    "n8n_changes": [],
                    "messages_sent": [],
                    "failures": [],
                    "next_wake_reason": "integrity check",
                    "verdict": "CYCLE: SAFE PROGRESS, NO REVENUE YET",
                }
            )
            self.assertTrue(store.verify_chain())

            with sqlite3.connect(path) as con:
                con.execute("DROP TRIGGER survival_cycles_no_update")
                con.execute(
                    "UPDATE survival_cycles SET payload=? WHERE id=?",
                    ('{"state":"GROWTH"}', cycle_id),
                )

            self.assertFalse(store.verify_chain())
            with self.assertRaises(RuntimeError):
                store.append_cycle(
                    {
                        "state": "SHADOW_ONLY",
                        "treasury_nzd": None,
                        "protected_reserve_nzd": "60.00",
                        "risk_capital_nzd": "0.00",
                        "data_freshness": "UNVERIFIED",
                        "highest_value_goal": "must not append after corruption",
                        "planned_actions": [],
                        "policy_checks": [],
                        "actions_completed": [],
                        "external_readbacks": [],
                        "cost_nzd": "0.00",
                        "revenue_nzd": "0.00",
                        "pnl_nzd": None,
                        "risk_change": "unchanged",
                        "n8n_changes": [],
                        "messages_sent": [],
                        "failures": ["audit corruption"],
                        "next_wake_reason": "human review",
                        "verdict": "CYCLE: UNKNOWN STATE, FAIL CLOSED",
                    }
                )


if __name__ == "__main__":
    unittest.main()
