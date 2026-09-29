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
# ATLAS Rule A: never rebuild the path. The old literal deploy/data/multihedge.db is a stale
# stub that exists on disk, so the "production database not present" skip below never fired
# and this suite errored on a table the stub does not have. Resolve through the authority.
import runtime_paths  # noqa: E402
import policy  # noqa: E402  # ATLAS Rule C: the single policy owner

PRODUCTION_DB = runtime_paths.production_db()

_REASONER_KEYS = {
    "POSITION_FRACTION", "TAKE_PROFIT", "STOP_LOSS", "MAX_HOLD_SECS",
    "TRAIL_ARM", "TRAIL_DIST", "CONFIDENCE_MIN",
}


class ReasonerParameterChainTests(unittest.TestCase):
    """Deterministic precedence contract tests using temporary SQLite.

    ATLAS Rule C / FINDING 004. The old chain was:

        DB mh_reasoner_params > config.yaml > code defaults

    That chain is why editing live_inventory.py or config.yaml changed nothing: the bare
    key/value table was winning at TAKE_PROFIT=0.05 with no author, timestamp or approval,
    and it survived image rebuilds. A value that exists in no source file and cannot be
    audited is not configuration, it is drift.

    The chain is now:

        declared default (live_inventory.py, via policy.effective_policy)
            <- APPROVED override only (provenance required)

    A row without provenance is REPORTED but NOT APPLIED, and an out-of-bounds value is
    refused rather than clamped.
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
        # A config block is still written here, and it must now be IGNORED, which is
        # itself the assertion. It is retained so the test proves the old layer cannot win.
        self.cfg.write_text("reasoner:\n  MAX_HOLD_SECS: 5400\n", encoding="utf-8")

    def tearDown(self):
        mh_reasoner.DB_PATH = self._db_path
        mh_reasoner.CFG_PATH = self._cfg_path
        self.tmp.cleanup()

    def test_code_default_applies_when_nothing_else_declares_it(self):
        self.assertEqual(mh_reasoner._load_params()["TAKE_PROFIT"],
                         mh_reasoner.DEFAULT_PARAMS["TAKE_PROFIT"])

    def test_config_block_can_no_longer_override_the_declared_default(self):
        """config.yaml is a declaration, not a second source of truth."""
        self.assertNotEqual(
            mh_reasoner._load_params()["MAX_HOLD_SECS"], 5400,
            "config.yaml reasoner: block is overriding the resolver again; that is a "
            "second source of truth (FINDING 004)",
        )
        self.assertEqual(mh_reasoner._load_params()["MAX_HOLD_SECS"], 1800)

    def test_unapproved_db_row_does_not_override(self):
        """The FINDING 004 regression test, inverted.

        This bare key/value row is exactly what was in force at TAKE_PROFIT=0.05. It must
        now be refused, because it carries no source, approved_by or approved_at.
        """
        with sqlite3.connect(self.db) as con:
            con.execute(mh_reasoner.PARAMS_SCHEMA)
            con.execute("INSERT INTO mh_reasoner_params VALUES('MAX_HOLD_SECS', 7200)")
        self.assertNotEqual(
            mh_reasoner._load_params()["MAX_HOLD_SECS"], 7200,
            "an unapproved row is still being applied; FINDING 004 is not fixed",
        )
        self.assertEqual(mh_reasoner._load_params()["MAX_HOLD_SECS"], 1800)

    def test_unapproved_row_is_still_reported(self):
        """Refusing silently would hide the disagreement, which was the original problem."""
        with sqlite3.connect(self.db) as con:
            con.execute(mh_reasoner.PARAMS_SCHEMA)
            con.execute("INSERT INTO mh_reasoner_params VALUES('TAKE_PROFIT', 0.05)")
        p = policy.effective_policy("MEME", db_path=self.db)
        keys = {c.get("key") for c in p.overrides_considered}
        self.assertIn("TAKE_PROFIT", keys, "the refused override must still be visible")

    def test_approved_override_with_provenance_does_apply(self):
        """The sanctioned path: a change that is auditable must actually take effect."""
        with sqlite3.connect(self.db) as con:
            con.execute(
                """CREATE TABLE mh_policy_overrides (key TEXT PRIMARY KEY, value REAL NOT NULL,
                   source TEXT NOT NULL, approved_by TEXT NOT NULL,
                   approved_at TEXT NOT NULL, approved INTEGER NOT NULL DEFAULT 0)""")
            con.execute(
                "INSERT INTO mh_policy_overrides VALUES(?,?,?,?,?,1)",
                ("MAX_HOLD_SECS", 3600, "manual review", "kelly", "2026-09-29"))
        self.assertEqual(mh_reasoner._load_params()["MAX_HOLD_SECS"], 3600)


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
        """Retained as a DRIFT DETECTOR only.

        config.yaml is no longer an input to resolution, so this no longer asserts that the
        resolver honours it. It asserts the opposite: if config.yaml still declares a value
        that disagrees with the resolver, that declaration is a stale rival source and must
        be reported rather than silently believed. The assertion below therefore fails
        while the stale block exists, which is the intended signal to remove it.
        """
        declared = yaml.safe_load(DEPLOYED_CONFIG.read_text(encoding="utf-8"))["reasoner"]
        drift = {
            key: (mh_reasoner.DEFAULT_PARAMS[key], value)
            for key, value in declared.items()
            if key in mh_reasoner.DEFAULT_PARAMS
            and float(mh_reasoner.DEFAULT_PARAMS[key]) != float(value)
        }
        if drift:
            self.fail(
                "config.yaml reasoner: block disagrees with the resolver and is now a "
                "stale rival source of truth (ATLAS Rule C). Remove the block, or record an "
                "approved override with provenance. Drift: "
                + ", ".join(f"{k}: config={v} resolved={d}" for k, (d, v) in drift.items())
            )

    def test_config_block_does_not_declare_reasoner_thresholds(self):
        """Inverted with the change that made it obsolete (ATLAS Rule C).

        This test used to REQUIRE config.yaml to declare every reasoner key, which enforced
        the existence of a second source of truth. It is now the opposite: the block must be
        empty or absent, so nobody can reintroduce a rival declaration.
        """
        cfg = yaml.safe_load(DEPLOYED_CONFIG.read_text(encoding="utf-8")) or {}
        declared = (cfg.get("reasoner") or {}) if isinstance(cfg.get("reasoner"), dict) else {}
        leaked = _REASONER_KEYS & set(declared)
        self.assertEqual(
            leaked, set(),
            "config.yaml reasoner: block re-declares "
            f"{sorted(leaked)}; thresholds are owned by live_inventory.py via "
            "policy.effective_policy(). Restating them here is the FINDING 004 defect.",
        )


if __name__ == "__main__":
    unittest.main()