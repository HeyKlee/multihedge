"""Contract tests for the runtime identity manifest (Project ATLAS plan section 13).

The manifest exists because a successful build, a running container, all-RUNNING
supervisor status and HTTP 200 are all *health* signals, and every one of them is
compatible with running stale or wrong code. FINDING 005 is the proof: a reviewer-required
fix was committed and pushed, the deploy "succeeded", all health checks passed, and the fix
was never live.

These tests pin the two properties that make the endpoint trustworthy:
  1. It never raises, even when the database is missing or corrupt.
  2. It never opens a production database read-write.
"""
import json
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent


class ManifestContract(unittest.TestCase):
    def test_build_manifest_returns_json_serialisable_dict(self):
        import runtime_manifest
        manifest = runtime_manifest.build_manifest()
        self.assertIsInstance(manifest, dict)
        # Must be serialisable with strict settings: no NaN, no Infinity.
        json.dumps(manifest, allow_nan=False)

    def test_manifest_reports_required_identity_fields(self):
        import runtime_manifest
        m = runtime_manifest.build_manifest()
        for section in ("identity", "config", "database", "declared_exit_policy",
                        "entry_friction_gate", "generated_at"):
            self.assertIn(section, m, f"manifest is missing the {section} section")

    def test_manifest_never_leaks_secret_values(self):
        """Only names/hashes/counts may appear. A value that looks like a key is a bug."""
        import runtime_manifest
        blob = json.dumps(runtime_manifest.build_manifest())
        for marker in ("JUPITER_API_KEY", "PRIVATE_KEY", "OPENROUTER_API_KEY",
                       "WALLET_PUBKEY", "SOLANA_RPC_URL"):
            if marker in blob:
                # The NAME may legitimately be listed, but never a value for it.
                self.assertNotRegex(
                    blob, rf"{marker}\"?\s*[:=]\s*\"[^\"]{{8,}}",
                    f"manifest appears to expose a value for {marker}",
                )

    def test_missing_database_degrades_instead_of_raising(self):
        import runtime_manifest
        with patch.object(runtime_manifest, "DB_PATH", Path("/nonexistent/nope.db")):
            manifest = runtime_manifest.build_manifest()
        self.assertFalse(manifest["database"]["exists"])
        self.assertIn("error", manifest["database"])

    def test_corrupt_database_degrades_instead_of_raising(self):
        import runtime_manifest
        bad = REPO / ".tmp_manifest_corrupt.db"
        bad.write_bytes(b"SQLite format 3\x00" + b"\x00" * 200)  # header only, no pages
        try:
            import runtime_manifest as rm
            with patch.object(rm, "DB_PATH", bad):
                manifest = rm.build_manifest()
            self.assertIsInstance(manifest, dict)
            json.dumps(manifest, allow_nan=False)
        finally:
            bad.unlink(missing_ok=True)

    def test_database_is_only_ever_opened_read_only(self):
        """Regression guard: a host-side read-write open is what races the container WAL."""
        import runtime_manifest
        opened = []
        real_connect = sqlite3.connect

        def spy(target, *args, **kwargs):
            opened.append((str(target), kwargs.get("uri", False)))
            return real_connect(target, *args, **kwargs)

        with patch.object(sqlite3, "connect", spy):
            runtime_manifest.build_manifest()

        db_opens = [o for o in opened if ".db" in o[0]]
        self.assertTrue(db_opens, "expected the manifest to inspect a database")
        for target, is_uri in db_opens:
            self.assertTrue(is_uri, f"database opened without a URI: {target}")
            # The URI must itself request read-only access, not merely be a URI.
            # This is the guard that matters: a URI without mode=ro opens read-write.
            self.assertIn(
                "mode=ro", target,
                f"database opened without mode=ro: {target} (a host-side read-write open "
                "is what races the container's WAL and destroyed 9 pages on 2026-09-28)",
            )

    def test_reports_policy_source_not_just_value(self):
        """A bare number is not auditable; the source must be named too."""
        import runtime_manifest
        gate = runtime_manifest.build_manifest()["entry_friction_gate"]
        self.assertIn("source", gate)
        self.assertIn("maximum_friction_cost_pct", gate)


if __name__ == "__main__":
    unittest.main()
