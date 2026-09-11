import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import survival_probe
from survival_policy import AuditStore


class SurvivalProbeTests(unittest.TestCase):
    def _audit(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return AuditStore(Path(tmp.name) / "audit.db")

    def test_snapshot_is_evidence_only_and_never_claims_live_ready(self):
        store = self._audit()
        with patch.object(survival_probe, "AUDIT_STORE", store), patch.object(
            survival_probe, "bridge_status",
            return_value={"wallet_ready": False, "reason": "sovereign authority disabled; shadow-only"},
        ), patch.object(survival_probe, "focused_tests", return_value={"ok": True, "tests": 3}), patch.object(
            survival_probe, "git_state", return_value={"clean": True, "head": "abc1234"}
        ):
            result = survival_probe.build_snapshot()

        self.assertEqual(result["schema"], "xora-survival-probe/v1")
        self.assertTrue(result["audit_chain_valid"])
        self.assertFalse(result["bridge"]["wallet_ready"])
        self.assertEqual(result["authority"], "SHADOW_ONLY")
        self.assertNotIn("treasury_nzd", result)
        self.assertNotIn("strategy_qualified", result)

    def test_invalid_audit_chain_fails_closed_without_other_probes(self):
        store = self._audit()
        with patch.object(store, "verify_chain", return_value=False), patch.object(
            survival_probe, "AUDIT_STORE", store
        ), patch.object(survival_probe, "bridge_status") as bridge:
            result = survival_probe.build_snapshot()

        self.assertEqual(result["authority"], "FAIL_CLOSED")
        self.assertFalse(result["audit_chain_valid"])
        bridge.assert_not_called()

    def test_json_output_round_trips(self):
        payload = {"schema": "xora-survival-probe/v1", "authority": "SHADOW_ONLY"}
        encoded = survival_probe.encode_snapshot(payload)
        self.assertEqual(json.loads(encoded), payload)
        self.assertNotIn("SOLANA_PRIVATE_KEY", encoded)


if __name__ == "__main__":
    unittest.main()
