import copy
import unittest
from unittest.mock import patch

try:
    from solders.keypair import Keypair
    import solana_signer_backend as ssb
except ImportError as exc:
    raise unittest.SkipTest(f"Solana dependencies unavailable: {exc}")

from execution_policy import validate_build
from test_execution_policy import JUP, SOL, valid_build, valid_intent


class SolanaSignerBackendTests(unittest.TestCase):
    def setUp(self):
        self.keypair = Keypair()
        self.wallet = str(self.keypair.pubkey())
        self.backend = ssb.SolanaSignerBackend(
            wallet_pubkey=self.wallet,
            rpc_url="https://api.mainnet-beta.solana.com",
            api_key="test-only",
            keypair_loader=lambda: self.keypair,
        )
        self.build = copy.deepcopy(valid_build())
        for group in ("computeBudgetInstructions", "setupInstructions"):
            for instruction in self.build[group]:
                for account in instruction["accounts"]:
                    account["pubkey"] = self.wallet
        for account in self.build["swapInstruction"]["accounts"]:
            account["pubkey"] = self.wallet

    def test_compile_binds_exact_validated_build_and_message(self):
        approved = validate_build(valid_intent(), self.build, self.wallet, 1000)
        compiled = self.backend.compile(self.build)
        self.assertEqual(compiled["build_hash"], approved.build_hash)
        self.assertEqual(len(compiled["message_hash"]), 64)
        self.assertEqual(compiled["unsigned"].message, compiled["message"])

    def test_sign_preserves_exact_message_hash(self):
        compiled = self.backend.compile(self.build)
        signed = self.backend.sign(compiled)
        self.assertEqual(signed["message_hash"], compiled["message_hash"])
        self.assertTrue(signed["transaction"].verify_with_results()[0])

    def test_wrong_key_is_denied(self):
        self.backend.keypair_loader = lambda: Keypair()
        compiled = self.backend.compile(self.build)
        with self.assertRaisesRegex(RuntimeError, "wallet identity"):
            self.backend.sign(compiled)

    def test_build_caps_account_count_and_expiry(self):
        class Response:
            status_code = 200
            def json(self): return {"ok": True}
        with patch.object(ssb.httpx, "get", return_value=Response()) as get:
            self.backend.build(valid_intent())
        params = get.call_args.kwargs["params"]
        self.assertEqual(params["maxAccounts"], "32")
        self.assertEqual(params["blockhashSlotsToExpiry"], "100")


if __name__ == "__main__":
    unittest.main()
