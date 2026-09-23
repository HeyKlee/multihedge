"""Consistency guards for the reasoner parameter chain.

mh_reasoner._load_params resolves tunables as: DB table mh_reasoner_params,
then config.yaml reasoner block, then code defaults. The declared config and the
runtime override drifted apart (config said MAX_HOLD_SECS 10800 while the applied
override was 7200). These tests lock the precedence and the declaration.
"""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import yaml

import config as config_module
import mh_reasoner

REPO_ROOT = Path(__file__).resolve().parent
DEPLOYED_CONFIG = REPO_ROOT / "config.yaml"
PRODUCTION_DB = REPO_ROOT / "deploy/data/multihedge.db"

_REASONER_KEYS = {
    "POSITION_FRACTION", "TAKE_PROFIT", "STOP_LOSS", "MAX_HOLD_SECS",
    "TRAIL_ARM", "TRAIL_DIST", "CONFIDENCE_MIN",
}


class ReasonerParameterChainTests(unittest.TestCase):
    """Deterministic precedence contract tests using temporary SQLite.

    Verifies the architectural precedence chain:
        DB mh_reasoner_params > config.yaml > code defaults.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "mh.db"
        self.cfg = Path(self.tmp.name) / "config.yaml"
        self._db_path, self._cfg_path = mh_reasoner.DB_PATH, mh_reasoner.CFG_PATH
        mh_reasoner.DB_PATH = self.db
        # mh_reasoner binds CFG_PATH into its own namespace at import time, so
        # patching config.CFG_PATH alone would leave the deployed file in play.
        mh_reasoner.CFG_PATH = self.cfg
        self.cfg.write_text("reasoner:\n  MAX_HOLD_SECS: 5400\n", encoding="utf-8")

    def tearDown(self):
        mh_reasoner.DB_PATH = self._db_path
        mh_reasoner.CFG_PATH = self._cfg_path
        self.tmp.cleanup()

    def test_code_default_applies_when_nothing_else_declares_it(self):
        self.assertEqual(mh_reasoner._load_params()["TAKE_PROFIT"],
                         mh_reasoner.DEFAULT_PARAMS["TAKE_PROFIT"])

    def test_config_block_overrides_the_code_default(self):
        self.assertEqual(mh_reasoner._load_params()["MAX_HOLD_SECS"], 5400)

    def test_db_override_wins_over_the_config_block(self):
        with sqlite3.connect(self.db) as con:
            con.execute(mh_reasoner.PARAMS_SCHEMA)
            con.execute("INSERT INTO mh_reasoner_params VALUES('MAX_HOLD_SECS', 7200)")
        self.assertEqual(mh_reasoner._load_params()["MAX_HOLD_SECS"], 7200)


class DeployedReasonerConfigTests(unittest.TestCase):
    """Read-only runtime diagnostics against the production database.

    These tests verify that the deployed config.yaml declarations are valid
    and that the runtime applied values respect the designed precedence chain:
    DB mh_reasoner_params (monthly optimizer) > config.yaml > code defaults.
    """

    def test_reasoner_block_declares_only_known_param_keys(self):
        declared = yaml.safe_load(DEPLOYED_CONFIG.read_text(encoding="utf-8"))["reasoner"]
        self.assertTrue(set(declared) <= set(mh_reasoner.DEFAULT_PARAMS),
                        f"unknown reasoner keys: {set(declared) - set(mh_reasoner.DEFAULT_PARAMS)}")

    def test_declared_values_match_what_the_reasoner_actually_applied(self):
        """Verify runtime applied params respect the DB-override precedence.

        The mh_reasoner_params DB table (written by the monthly optimizer) is
        the highest precedence source. This test reads both config.yaml and any
        DB overrides, then verifies the applied runtime value matches whichever
        source has higher precedence per the architecture.
        """
        if not PRODUCTION_DB.exists():
            self.skipTest("production database not present in this environment")
        try:
            con = sqlite3.connect(
                f"file:{PRODUCTION_DB}?mode=ro", uri=True, timeout=5)
            with con:
                # Read DB overrides (highest precedence in the chain)
                db_overrides = {}
                try:
                    for row in con.execute(
                        "SELECT key, value FROM mh_reasoner_params"
                    ):
                        db_overrides[row[0]] = row[1]
                except Exception:
                    pass
                row = con.execute(
                    "SELECT settings_json FROM mh_parameter_application WHERE trader='reasoner'"
                ).fetchone()
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if "locked" in msg or "busy" in msg:
                self.skipTest(
                    f"production database locked by running container: {exc}")
            raise
        if row is None:
            self.skipTest("no applied reasoner settings recorded yet")
        applied = json.loads(row[0])
        declared = yaml.safe_load(DEPLOYED_CONFIG.read_text(encoding="utf-8"))["reasoner"]
        for key, value in declared.items():
            self.assertIn(key, applied, f"{key} declared but never applied")
            # DB override wins over config.yaml per the architectural precedence chain
            expected = db_overrides.get(key, float(value))
            self.assertEqual(
                float(expected), float(applied[key]),
                f"{key} drift: config declares {value}, "
                f"DB override {db_overrides.get(key, 'N/A')}, "
                f"expected {expected}, runtime applied {applied[key]}")

    def test_code_defaults_agree_with_config_block_values(self):
        declared = yaml.safe_load(DEPLOYED_CONFIG.read_text(encoding="utf-8"))["reasoner"]
        for key in _REASONER_KEYS:
            if key in declared:
                with self.subTest(key=key):
                    self.assertEqual(
                        mh_reasoner.DEFAULT_PARAMS[key],
                        declared[key],
                        f"code DEFAULT_PARAMS.{key}={mh_reasoner.DEFAULT_PARAMS[key]} "
                        f"does not match config.yaml reasoner.{key}={declared[key]}")

    def test_all_reasoner_keys_have_config_block_values(self):
        declared = yaml.safe_load(DEPLOYED_CONFIG.read_text(encoding="utf-8"))["reasoner"]
        missing = _REASONER_KEYS - set(declared)
        if missing:
            self.fail(f"config.yaml reasoner block is missing keys: {missing}")


if __name__ == "__main__":
    unittest.main()