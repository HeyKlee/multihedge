"""Contract tests for the single effective-policy resolver (Project ATLAS Rule C).

The defect: four layers defined exit thresholds and disagreed. `mh_reasoner_params` (a bare
key/value table with no author, timestamp or approval) was in force at TP=5.0%, while
`live_inventory.py` declared 1.5%. Editing the source changed nothing. That is FINDING 004
and the exact "I change it and nothing applies" symptom.

These tests assert the resolver makes that structurally impossible:
  - declared defaults are stated once
  - an UNAPPROVED override is reported but NOT applied
  - an APPROVED override is applied
  - an out-of-bounds override is refused, never clamped
  - conflicts between layers are reported, not hidden
  - the resolved value matches the evidence in FINDING 011
"""
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import policy
from policy import PolicyError, effective_policy, declared_defaults

REPO = Path(__file__).resolve().parent


def make_db(legacy_rows=(), approved_rows=(), *, with_approved_table=True) -> str:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    con = sqlite3.connect(tmp.name)
    if legacy_rows:
        con.execute("CREATE TABLE mh_reasoner_params (key TEXT PRIMARY KEY, value REAL)")
        con.executemany("INSERT OR REPLACE INTO mh_reasoner_params VALUES(?,?)",
                        [(k, v) for k, v in legacy_rows])
    if with_approved_table and approved_rows:
        con.execute("""CREATE TABLE mh_policy_overrides (
            key TEXT PRIMARY KEY, value REAL NOT NULL, source TEXT NOT NULL,
            approved_by TEXT NOT NULL, approved_at TEXT NOT NULL,
            approved INTEGER NOT NULL DEFAULT 0)""")
        con.executemany(
            "INSERT OR REPLACE INTO mh_policy_overrides VALUES(?,?,?,?,?,1)",
            [(k, v, s, who, when) for k, v, s, who, when in approved_rows])
    con.commit()
    con.close()
    return tmp.name


class DeclaredDefaultsAreSingleSourced(unittest.TestCase):
    def test_meme_defaults_match_the_measured_evidence(self):
        p = declared_defaults("MEME")
        # FINDING 011: 1.5% is reachable in 42% of 30-min and 64% of 60-min windows.
        self.assertEqual(p.take_profit_pct, 0.015)
        # Kelly's stated 30-60 minute band; 1800s is the floor of it.
        self.assertGreaterEqual(p.max_hold_seconds, 1800)
        self.assertLessEqual(p.max_hold_seconds, 3600)

    def test_take_profit_is_not_the_unreachable_twenty_percent(self):
        """AGENTS.md:63 says +20%. FINDING 011 shows 0.0% of 5-min and 1.1% of 60-min."""
        for mode in ("MEME", "SERIOUS"):
            self.assertLess(
                declared_defaults(mode).take_profit_pct, 0.05,
                "a 5%+ intraday target was measured unreachable inside the hold window",
            )

    def test_unknown_mode_is_refused(self):
        with self.assertRaises(PolicyError):
            declared_defaults("SCAM_COINS")


class UnapprovedOverridesAreNotApplied(unittest.TestCase):
    """The core fix: the legacy table has no provenance, so it cannot move risk."""

    def test_legacy_five_percent_row_is_refused(self):
        db = make_db(legacy_rows=[("TAKE_PROFIT", 0.05), ("MAX_HOLD_SECS", 7200.0)])
        try:
            p = effective_policy("MEME", db_path=db)
            self.assertEqual(
                p.take_profit_pct, 0.015,
                "an unapproved override was applied; this is the FINDING 004 defect",
            )
            self.assertNotEqual(p.max_hold_seconds, 7200)
        finally:
            Path(db).unlink(missing_ok=True)

    def test_unapplied_override_is_still_reported(self):
        """Refusing silently would hide the disagreement, which is the original problem."""
        db = make_db(legacy_rows=[("TAKE_PROFIT", 0.05)])
        try:
            p = effective_policy("MEME", db_path=db)
            keys = {c.get("key") for c in p.overrides_considered}
            self.assertIn("TAKE_PROFIT", keys)
            reasons = " ".join(str(c.get("reason", "")) for c in p.overrides_considered)
            self.assertIn("no source", reasons + " " + str(p.overrides_considered))
        finally:
            Path(db).unlink(missing_ok=True)

    def test_missing_database_yields_the_declared_default(self):
        p = effective_policy("MEME", db_path=Path("/tmp/definitely_absent.db"))
        self.assertEqual(p.take_profit_pct, 0.015)


class ApprovedOverridesAreApplied(unittest.TestCase):
    def test_approved_override_with_provenance_is_applied(self):
        db = make_db(approved_rows=[("TAKE_PROFIT", 0.02, "manual review", "kelly", "2026-09-28")])
        try:
            p = effective_policy("MEME", db_path=db)
            self.assertEqual(p.take_profit_pct, 0.02)
            self.assertIn("manual review", p.source)
            self.assertEqual(p.approved_by, "kelly")
        finally:
            Path(db).unlink(missing_ok=True)

    def test_unapproved_flagged_row_is_not_applied(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        con = sqlite3.connect(tmp.name)
        con.execute("""CREATE TABLE mh_policy_overrides (
            key TEXT PRIMARY KEY, value REAL, source TEXT, approved_by TEXT,
            approved_at TEXT, approved INTEGER DEFAULT 0)""")
        con.execute("INSERT INTO mh_policy_overrides VALUES(?,?,?,?,?,0)",
                    ("TAKE_PROFIT", 0.03, "draft", "nobody", "2026-09-28"))
        con.commit()
        con.close()
        try:
            p = effective_policy("MEME", db_path=tmp.name)
            self.assertEqual(p.take_profit_pct, 0.015, "approved=0 row was applied")
        finally:
            Path(tmp.name).unlink(missing_ok=True)

    def test_escape_hatch_requires_explicit_env(self):
        db = make_db(legacy_rows=[("TAKE_PROFIT", 0.05)])
        try:
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop(policy.APPROVAL_ENV, None)
                self.assertEqual(effective_policy("MEME", db_path=db).take_profit_pct, 0.015)
            with patch.dict(os.environ, {policy.APPROVAL_ENV: "1"}):
                self.assertEqual(effective_policy("MEME", db_path=db).take_profit_pct, 0.05)
        finally:
            Path(db).unlink(missing_ok=True)


class BadValuesAreRefusedNotClamped(unittest.TestCase):
    def test_out_of_bounds_override_is_refused(self):
        db = make_db(approved_rows=[("TAKE_PROFIT", 0.5, "typo", "kelly", "2026-09-28")])
        try:
            p = effective_policy("MEME", db_path=db)
            self.assertEqual(p.take_profit_pct, 0.015,
                             "an out-of-bounds value was accepted or silently clamped")
        finally:
            Path(db).unlink(missing_ok=True)

    def test_negative_take_profit_is_refused(self):
        db = make_db(approved_rows=[("TAKE_PROFIT", -0.5, "typo", "kelly", "2026-09-28")])
        try:
            self.assertEqual(effective_policy("MEME", db_path=db).take_profit_pct, 0.015)
        finally:
            Path(db).unlink(missing_ok=True)

    def test_validation_rejects_non_numeric_and_nan(self):
        for bad in (True, "0.5", None, float("nan"), float("inf")):
            with self.subTest(bad=bad):
                with self.assertRaises(PolicyError):
                    policy._validate("take_profit_pct", bad)


class ConflictsAreVisible(unittest.TestCase):
    def test_conflicts_name_the_disagreeing_layers(self):
        rows = policy.policy_conflicts()
        self.assertTrue(rows)
        names = {r["layer"] for r in rows}
        self.assertTrue(any("policy.py" in n for n in names))

    def test_describe_reports_version_and_both_modes(self):
        d = policy.describe()
        self.assertEqual(d["policy_version"], policy.POLICY_VERSION)
        self.assertIn("MEME", d["modes"])
        self.assertIn("SERIOUS", d["modes"])
        self.assertIn("conflicts", d)

    def test_resolved_policy_serialises_for_the_api(self):
        import json
        d = effective_policy("MEME").as_dict()
        json.dumps(d, allow_nan=False)  # must not emit NaN/Infinity


if __name__ == "__main__":
    unittest.main()
