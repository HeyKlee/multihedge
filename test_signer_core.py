import tempfile
import unittest
from pathlib import Path

from execution_policy import OrderStore, PolicyDenied, validate_build
from test_execution_policy import JUP, SOL, USDC, WALLET, valid_build, valid_intent
import signer_core


class FakeBackend:
    def __init__(self):
        self.sent = 0
        self.simulation = {"ok": True, "message_hash": "message-hash", "units": 100000}
        self.confirmed = True
        self.reconciled = {"verified": True, "input_atomic": 1000000, "output_atomic": 500000}
        self.prepared = 0

    def prepare(self, intent): self.prepared += 1
    def current_block_height(self): return 1000
    def build(self, intent): return valid_build()
    def compile(self, build):
        build_hash = validate_build(valid_intent(), build, WALLET, 1000).build_hash
        return {"message_hash": "message-hash", "build_hash": build_hash, "payload": object()}
    def simulate(self, compiled): return self.simulation
    def sign(self, compiled): return {"message_hash": "message-hash", "signed": object()}
    def send(self, signed): self.sent += 1; return "signature-one"
    def confirm(self, signature, last_valid_block_height): return self.confirmed
    def reconcile(self, signature, intent, min_output_atomic): return self.reconciled


class SignerCoreTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = OrderStore(Path(tmp.name) / "orders.db")
        self.backend = FakeBackend()
        self.core = signer_core.SignerCore(
            wallet_pubkey=WALLET,
            approved_mints={USDC, JUP},
            order_store=self.store,
            backend=self.backend,
        )

    def test_full_verified_flow_marks_reconciled(self):
        result = self.core.execute(
            valid_intent(), pre_treasury_nzd="81.23", projected_post_nzd="80.90",
            fee_reserve_sol="0.030", required_fee_reserve_sol="0.020",
            treasury_verified=True, treasury_age_seconds=0,
        )
        self.assertEqual(result["state"], "RECONCILED")
        self.assertEqual(self.backend.prepared, 1)
        self.assertEqual(self.backend.sent, 1)
        self.assertEqual(self.store.get(valid_intent().order_id)["state"], "RECONCILED")

    def test_failed_simulation_never_signs_or_sends(self):
        self.backend.simulation = {"ok": False, "message_hash": "message-hash", "error": "program error"}
        with self.assertRaisesRegex(PolicyDenied, "simulation"):
            self.core.execute(valid_intent(), "81.23", "80.90", "0.030", "0.020", treasury_verified=True, treasury_age_seconds=0)
        self.assertEqual(self.backend.sent, 0)

    def test_simulated_and_signed_message_must_match(self):
        original = self.backend.sign
        self.backend.sign = lambda compiled: {"message_hash": "changed", "signed": object()}
        with self.assertRaisesRegex(PolicyDenied, "payload"):
            self.core.execute(valid_intent(), "81.23", "80.90", "0.030", "0.020", treasury_verified=True, treasury_age_seconds=0)
        self.assertEqual(self.backend.sent, 0)
        self.backend.sign = original

    def test_build_hash_cannot_be_bypassed_by_reusing_it_as_message_hash(self):
        original_compile = self.backend.compile
        approved_hash = validate_build(valid_intent(), valid_build(), WALLET, 1000).build_hash
        self.backend.compile = lambda build: {
            "message_hash": approved_hash,
            "build_hash": "wrong-build-hash",
            "payload": object(),
        }
        with self.assertRaisesRegex(PolicyDenied, "bound"):
            self.core.execute(valid_intent(), "81.23", "80.90", "0.030", "0.020", treasury_verified=True, treasury_age_seconds=0)
        self.assertEqual(self.backend.sent, 0)
        self.backend.compile = original_compile

    def test_stale_treasury_evidence_is_denied_before_backend_work(self):
        with self.assertRaisesRegex(PolicyDenied, "treasury"):
            self.core.execute(
                valid_intent(), "81.23", "80.90", "0.030", "0.020",
                treasury_verified=True, treasury_age_seconds=301,
            )
        self.assertEqual(self.backend.prepared, 0)

    def test_duplicate_order_never_resends(self):
        self.core.execute(valid_intent(), "81.23", "80.90", "0.030", "0.020", treasury_verified=True, treasury_age_seconds=0)
        with self.assertRaisesRegex(PolicyDenied, "duplicate"):
            self.core.execute(valid_intent(), "81.23", "80.90", "0.030", "0.020", treasury_verified=True, treasury_age_seconds=0)
        self.assertEqual(self.backend.sent, 1)

    def test_failed_confirmation_becomes_human_required_and_never_resends(self):
        self.backend.confirmed = False
        with self.assertRaisesRegex(PolicyDenied, "confirmation"):
            self.core.execute(valid_intent(), "81.23", "80.90", "0.030", "0.020", treasury_verified=True, treasury_age_seconds=0)
        self.assertEqual(self.store.get(valid_intent().order_id)["state"], "REQUIRES_HUMAN")
        with self.assertRaisesRegex(PolicyDenied, "duplicate"):
            self.core.execute(valid_intent(), "81.23", "80.90", "0.030", "0.020", treasury_verified=True, treasury_age_seconds=0)
        self.assertEqual(self.backend.sent, 1)

    def test_failed_reconciliation_locks_order(self):
        self.backend.reconciled = {"verified": False, "reason": "balance mismatch"}
        with self.assertRaisesRegex(PolicyDenied, "reconciliation"):
            self.core.execute(valid_intent(), "81.23", "80.90", "0.030", "0.020", treasury_verified=True, treasury_age_seconds=0)
        self.assertEqual(self.store.get(valid_intent().order_id)["state"], "REQUIRES_HUMAN")


if __name__ == "__main__":
    unittest.main()
