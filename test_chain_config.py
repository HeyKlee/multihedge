import os
import unittest
from unittest.mock import patch

try:
    import chain
except ImportError as exc:
    raise unittest.SkipTest(f"Solana dependencies unavailable: {exc}")


class ChainConfigurationTests(unittest.TestCase):
    def test_current_network_uses_explicit_environment_setting(self):
        with patch.dict(os.environ, {"SOLANA_NETWORK": "devnet"}, clear=False):
            self.assertEqual(chain.current_network(), "devnet")

    def test_current_network_rejects_unknown_setting(self):
        with patch.dict(os.environ, {"SOLANA_NETWORK": "not-a-network"}, clear=False):
            with self.assertRaises(RuntimeError):
                chain.current_network()


if __name__ == "__main__":
    unittest.main()
