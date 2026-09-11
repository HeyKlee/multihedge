import unittest
import tempfile
import shutil
import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

# Add the current directory to sys.path so we can import live_bridge
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import live_bridge
import paper  # we need to mock paper.paper_gate_status as well
from execution_policy import PolicyDenied, TradeIntent


class TestLiveBridge(unittest.TestCase):
    def setUp(self):
        # Create a temporary directory for isolated test
        self.test_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.test_dir)

        # Paths for test files
        self.db_path = Path(self.test_dir) / "multihedge.db"
        self.confirm_flag = Path(self.test_dir) / "confirm_live.flag"

        # Patch DB_PATH and CONFIRM_FLAG in live_bridge
        self.db_patch = patch.object(live_bridge, 'DB_PATH', self.db_path)
        self.flag_patch = patch.object(live_bridge, 'CONFIRM_FLAG', self.confirm_flag)
        self.authority_patch = patch.object(
            live_bridge, 'SOVEREIGN_MAINNET_AUTHORITY_ENABLED', True
        )
        self.signer_patch = patch.object(live_bridge, 'ISOLATED_SIGNER_READY', True)
        self.db_patch.start()
        self.flag_patch.start()
        self.authority_patch.start()
        self.signer_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.addCleanup(self.flag_patch.stop)
        self.addCleanup(self.authority_patch.stop)
        self.addCleanup(self.signer_patch.stop)

        # Patch chain module: we'll replace sys.modules['chain'] with a MagicMock
        self.chain_patch = patch.dict('sys.modules')
        self.chain_patch.start()
        self.chain_mock = MagicMock()
        sys.modules['chain'] = self.chain_mock
        # Now set the live_bridge.chain to our mock (overrides the None from import)
        live_bridge.chain = self.chain_mock
        self.addCleanup(self.chain_patch.stop)

        # Mock paper module's paper_gate_status
        self.paper_patch = patch.object(paper, 'paper_gate_status', return_value={"eligible": True, "n": 10, "win_rate": 0.8})
        self.paper_patch.start()
        self.addCleanup(self.paper_patch.stop)

        # Sample config for tests
        self.cfg = {
            "coins": [
                {"symbol": "SOL", "mint": "So11111111111111111111111111111111111111112"},
                {"symbol": "BONK", "mint": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"}
            ]
        }

        # Set up default chain mock behavior for a ready wallet (overridden in specific tests)
        self.chain_mock.get_keypair.return_value = (MagicMock(), "dummy")
        self.chain_mock.current_network.return_value = "mainnet-beta"
        self.chain_mock.live_mode_enabled.return_value = True
        self.chain_mock.get_balance.return_value = 10.0

    def test_live_status_chain_missing(self):
        """When chain module is not available, live_status should fail closed."""
        # Temporarily set chain to None in live_bridge to simulate missing module
        original_chain = live_bridge.chain
        live_bridge.chain = None
        try:
            status = live_bridge.live_status(self.cfg)
            self.assertFalse(status["wallet_ready"])
            self.assertEqual(status["reason"], "chain module not available")
        finally:
            live_bridge.chain = original_chain

    def test_sovereign_authority_is_required(self):
        self.confirm_flag.touch()
        with patch.object(live_bridge, "SOVEREIGN_MAINNET_AUTHORITY_ENABLED", False):
            status = live_bridge.live_status(self.cfg)

        self.assertFalse(status["wallet_ready"])
        self.assertEqual(status["reason"], "sovereign authority disabled; shadow-only")

    def test_isolated_signer_is_required(self):
        self.confirm_flag.touch()
        with patch.object(live_bridge, "ISOLATED_SIGNER_READY", False):
            status = live_bridge.live_status(self.cfg)
        self.assertFalse(status["wallet_ready"])
        self.assertEqual(status["reason"], "isolated signer not deployed")

    def test_reserve_asset_is_usdc_and_sol_is_fees_only(self):
        self.assertEqual(live_bridge.RESERVE_SYMBOL, "USDC")
        self.assertEqual(live_bridge.RESERVE_MINT, "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")
        self.assertEqual(live_bridge.MIN_SOL_FEE_RESERVE, 0.01)

    def test_live_status_wallet_ready(self):
        """When all conditions are met, wallet_ready should be True."""
        # Ensure confirmation flag exists
        self.confirm_flag.touch()

        status = live_bridge.live_status(self.cfg)
        self.assertTrue(status["wallet_ready"])
        self.assertEqual(status["reason"], "wallet_ok")
        self.assertEqual(status["network"], "mainnet-beta")
        self.assertTrue(status["live_mode"])
        self.assertTrue(status["confirm_flag"])

    def test_live_status_not_live_mode(self):
        """If live_mode is not enabled, wallet_ready should be False."""
        self.chain_mock.live_mode_enabled.return_value = False  # not live
        self.confirm_flag.touch()

        status = live_bridge.live_status(self.cfg)
        self.assertFalse(status["wallet_ready"])
        self.assertIn("not live-safe", status["reason"])

    def test_live_status_rejects_non_mainnet_network(self):
        self.chain_mock.current_network.return_value = "devnet"
        self.confirm_flag.touch()

        status = live_bridge.live_status(self.cfg)

        self.assertFalse(status["wallet_ready"])
        self.assertEqual(status["reason"], "wrong network: devnet")

    def test_live_status_no_confirm_flag(self):
        """If confirmation flag is missing, wallet_ready should be False."""
        # Do not create confirmation flag
        status = live_bridge.live_status(self.cfg)
        self.assertFalse(status["wallet_ready"])
        self.assertIn("not live-safe", status["reason"])

    def test_live_status_unfunded(self):
        """If balance is too low, wallet_ready should be False."""
        self.chain_mock.get_balance.return_value = 0.02  # below REAL_SAFE_SOL + 0.005
        self.confirm_flag.touch()

        status = live_bridge.live_status(self.cfg)
        self.assertFalse(status["wallet_ready"])
        self.assertEqual(status["reason"], "unfunded")

    def test_assert_live_allowed_ok(self):
        """When all gates pass, assert_live_allowed should not raise."""
        self.confirm_flag.touch()
        # Should not raise
        live_bridge.assert_live_allowed("SOL", self.cfg)

    def test_assert_live_allowed_paper_gate_fail(self):
        """If paper gate fails, assert_live_allowed should raise."""
        # Make paper gate return ineligible
        paper_gate_mock = patch.object(paper, 'paper_gate_status', return_value={"eligible": False, "n": 0, "win_rate": 0.0})
        paper_gate_mock.start()
        try:
            self.confirm_flag.touch()
            with self.assertRaises(RuntimeError) as cm:
                live_bridge.assert_live_allowed("SOL", self.cfg)
            self.assertIn("live blocked for SOL", str(cm.exception))
        finally:
            paper_gate_mock.stop()

    def test_legacy_direct_swap_is_permanently_disabled(self):
        """The application process may never directly access the wallet signer."""
        self.chain_mock.jupiter_quote.return_value = {"some": "quote"}
        self.chain_mock.send_swap.return_value = {"signature": "txsig", "out_amount_lam": 5000000}

        self.confirm_flag.touch()
        self.assertTrue(self.confirm_flag.exists())

        # Execute swap
        coin_cfg = {"symbol": "SOL", "mint": "So11111111111111111111111111111111111111112"}
        with self.assertRaisesRegex(RuntimeError, "isolated signer"):
            live_bridge.execute_swap(coin_cfg, cfg=self.cfg)
        self.assertTrue(self.confirm_flag.exists())
        self.chain_mock.send_swap.assert_not_called()

    def test_execute_swap_no_flag_raises(self):
        """If confirmation flag is missing, execute_swap should raise."""
        # Do not create flag
        coin_cfg = {"symbol": "SOL", "mint": "So11111111111111111111111111111111111111112"}
        with self.assertRaises(RuntimeError) as cm:
            live_bridge.execute_swap(coin_cfg, cfg=self.cfg)
        self.assertIn("confirmation flag missing", str(cm.exception))

    def test_status(self):
        """status() should return live_status."""
        self.confirm_flag.touch()
        status = live_bridge.status(self.cfg)
        self.assertTrue(status["wallet_ready"])

    def test_explicit_buy_requires_strategy_authorization_before_key_load(self):
        intent = TradeIntent(
            order_id="auto:1:JUP:BUY", side="BUY", input_mint=live_bridge.RESERVE_MINT,
            output_mint="JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",
            amount_atomic=1_000_000, slippage_bps=50,
            strategy="deepseek_v4_flash_autonomous", expected_reward_nzd="0.30",
            expected_loss_nzd="0.10",
        )
        self.confirm_flag.touch()
        with self.assertRaisesRegex(PolicyDenied, "strategy evidence"):
            live_bridge.execute_live_intent(self.cfg, intent, entry_authorized=False)
        self.chain_mock.get_keypair.assert_not_called()

    def test_order_ledger_path_is_durable_not_tmp(self):
        self.assertNotIn("/tmp", str(live_bridge.LIVE_ORDER_DB))


if __name__ == '__main__':
    unittest.main()