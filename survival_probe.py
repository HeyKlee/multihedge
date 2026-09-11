"""Deterministic, read-only evidence probe for scheduled XORA cycles."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

from survival_policy import AuditStore

ROOT = Path(__file__).resolve().parent


class ReadOnlyAuditVerifier:
    def __init__(self, path: Path):
        self.path = Path(path)

    def verify_chain(self) -> bool:
        try:
            uri = f"file:{self.path.resolve()}?mode=ro"
            with sqlite3.connect(uri, uri=True) as con:
                rows = con.execute(
                    "SELECT id,ts,payload,previous_hash,record_hash "
                    "FROM survival_cycles ORDER BY id"
                ).fetchall()
        except sqlite3.DatabaseError:
            return False
        return AuditStore._rows_valid(rows)


AUDIT_STORE = ReadOnlyAuditVerifier(ROOT / "survival" / "survival_audit.db")


def _run(command: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def bridge_status() -> dict:
    result = _run([sys.executable, "live_bridge.py", "status"])
    if result.returncode != 0:
        return {"wallet_ready": False, "reason": "status_probe_failed", "exit_code": result.returncode}
    try:
        parsed = json.loads(result.stdout)
    except (TypeError, ValueError):
        return {"wallet_ready": False, "reason": "status_probe_invalid_json", "exit_code": result.returncode}
    return {
        "wallet_ready": bool(parsed.get("wallet_ready", False)),
        "reason": str(parsed.get("reason", "missing_reason"))[:160],
        "network": str(parsed.get("network", "unknown"))[:40],
        "live_mode": bool(parsed.get("live_mode", False)),
        "confirm_flag": bool(parsed.get("confirm_flag", False)),
    }


def focused_tests() -> dict:
    suites = [
        "test_agents.py",
        "test_live_bridge.py",
        "test_execution_policy.py",
        "test_signer_core.py",
        "test_survival_probe.py",
    ]
    result = _run([sys.executable, "-m", "unittest", "-q", *suites], timeout=180)
    combined = result.stdout + "\n" + result.stderr
    marker = "Ran "
    count = None
    if marker in combined:
        try:
            count = int(combined.split(marker, 1)[1].split(" test", 1)[0])
        except (ValueError, IndexError):
            count = None
    return {
        "ok": result.returncode == 0,
        "tests": count,
        "suites": suites,
        "exit_code": result.returncode,
    }


def git_state() -> dict:
    status = _run(["git", "status", "--porcelain=v1"])
    head = _run(["git", "rev-parse", "--short", "HEAD"])
    return {
        "clean": status.returncode == 0 and not status.stdout.strip(),
        "head": head.stdout.strip() if head.returncode == 0 else None,
        "status_exit_code": status.returncode,
    }


def build_snapshot() -> dict:
    valid = AUDIT_STORE.verify_chain()
    snapshot = {
        "schema": "xora-survival-probe/v1",
        "generated_unix": int(time.time()),
        "audit_chain_valid": bool(valid),
        "authority": "FAIL_CLOSED" if not valid else "SHADOW_ONLY",
    }
    if not valid:
        return snapshot
    bridge = bridge_status()
    tests = focused_tests()
    git = git_state()
    snapshot.update({"bridge": bridge, "focused_tests": tests, "git": git})
    if bridge.get("wallet_ready"):
        snapshot["authority"] = "UNVERIFIED_LIVE_STATE"
    return snapshot


def encode_snapshot(snapshot: dict) -> str:
    return json.dumps(snapshot, sort_keys=True, separators=(",", ":"), allow_nan=False)


def main() -> int:
    snapshot = build_snapshot()
    print(encode_snapshot(snapshot))
    healthy = (
        snapshot.get("audit_chain_valid") is True
        and snapshot.get("authority") == "SHADOW_ONLY"
        and snapshot.get("bridge", {}).get("wallet_ready") is False
        and snapshot.get("focused_tests", {}).get("ok") is True
    )
    return 0 if healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
