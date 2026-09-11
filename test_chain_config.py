import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
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

    def test_database_path_is_multihedge_not_autohedge(self):
        self.assertEqual(chain.DB_PATH.name, "multihedge.db")

    def test_live_mode_missing_config_table_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "multihedge.db"
            sqlite3.connect(db_path).close()
            with patch.object(chain, "DB_PATH", db_path), patch.dict(
                os.environ, {}, clear=True
            ):
                self.assertFalse(chain.live_mode_enabled())

    def test_live_mode_requires_explicit_environment_or_database_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "multihedge.db"
            con = sqlite3.connect(db_path)
            con.execute("CREATE TABLE config(key TEXT PRIMARY KEY, value TEXT)")
            con.execute("INSERT INTO config(key,value) VALUES('live_mode','1')")
            con.commit()
            con.close()
            with patch.object(chain, "DB_PATH", db_path), patch.dict(
                os.environ, {}, clear=True
            ):
                self.assertTrue(chain.live_mode_enabled())
            with patch.object(chain, "DB_PATH", db_path), patch.dict(
                os.environ, {"MULTIHEDGE_LIVE_MODE": "0"}, clear=True
            ):
                self.assertFalse(chain.live_mode_enabled())


if __name__ == "__main__":
    unittest.main()
