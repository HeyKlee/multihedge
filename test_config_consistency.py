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


class ReasonerParameterChainTests(unittest.TestCase):
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
    def test_reasoner_block_declares_only_known_param_keys(self):
        declared = yaml.safe_load(DEPLOYED_CONFIG.read_text(encoding="utf-8"))["reasoner"]
        self.assertTrue(set(declared) <= set(mh_reasoner.DEFAULT_PARAMS),
                        f"unknown reasoner keys: {set(declared) - set(mh_reasoner.DEFAULT_PARAMS)}")

    def test_declared_values_match_what_the_reasoner_actually_applied(self):
        if not PRODUCTION_DB.exists():
            self.skipTest("production database not present in this environment")
        with sqlite3.connect(f"file:{PRODUCTION_DB}?mode=ro", uri=True) as con:
            row = con.execute(
                "SELECT settings_json FROM mh_parameter_application WHERE trader='reasoner'"
            ).fetchone()
        if row is None:
            self.skipTest("no applied reasoner settings recorded yet")
        applied = json.loads(row[0])
        declared = yaml.safe_load(DEPLOYED_CONFIG.read_text(encoding="utf-8"))["reasoner"]
        for key, value in declared.items():
            self.assertIn(key, applied, f"{key} declared but never applied")
            self.assertEqual(float(value), float(applied[key]),
                             f"{key} drift: config declares {value}, runtime applied {applied[key]}")


if __name__ == "__main__":
    unittest.main()
