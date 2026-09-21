#!/usr/bin/env python3
"""Run one key-isolated MultiHedge autonomous cycle."""

import fcntl
import json
import os
from pathlib import Path
import subprocess
import time

ROOT = Path("/home/kelly/multihedge")
DATA = ROOT / "deploy/data"
IMAGE = "deploy-multihedge"


LOCKED_MARKERS = ("database is locked", "database is busy")


def run(command, attempts=3, delay=5):
    """Run a step, retrying only on transient SQLite contention.

    In delete-journal mode a writer's commit briefly holds an EXCLUSIVE lock,
    so a kelly one-shot step can wedge on "database is locked" when the
    persistent multihedge daemon writes at the same moment. Retry only that
    condition with backoff; never retry real failures (bad env, missing
    files, auth), which should surface immediately instead of being masked.
    Each docker run reads current DB state and aborts before its write
    commits on a lock, so re-running is safe and idempotent.
    """
    last = None
    for attempt in range(attempts):
        last = subprocess.run(command, text=True, capture_output=True,
                              timeout=180, check=False)
        if last.returncode == 0 or not any(
                marker in (last.stderr or "") for marker in LOCKED_MARKERS):
            return last
        if attempt < attempts - 1:
            time.sleep(delay * (attempt + 1))
    return last


def last_json(text):
    for line in reversed([line.strip() for line in text.splitlines() if line.strip()]):
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                return value
        except ValueError:
            continue
    return None


def tail(text, limit=400):
    """Keep the last stderr/stdout lines so a failed step explains itself."""
    lines = [line for line in (text or "").splitlines() if line.strip()]
    return "\n".join(lines[-6:])[-limit:]


def main():
    lock_path = DATA / "autonomous_live.lock"
    lock_path.touch(mode=0o600, exist_ok=True)
    with lock_path.open("r+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"state": "HOLD", "reason": "cycle_already_running"}))
            return 0

        required = [DATA / "agent.env", DATA / "signer.env", ROOT / "confirm_live.flag",
                    DATA / "multihedge.db"]
        if any(not path.exists() for path in required):
            print(json.dumps({"state": "HOLD", "reason": "deployment_prerequisite_missing"}))
            return 1
        if any((path.stat().st_mode & 0o077) != 0 for path in required[:2]):
            print(json.dumps({"state": "HOLD", "reason": "credential_permissions_unsafe"}))
            return 1

        queue = DATA / "live_queue"
        logs = DATA / "agent_logs"
        state = DATA / "signer_state"
        for directory in (queue, logs, state):
            directory.mkdir(mode=0o700, exist_ok=True)

        runtime_user = f"{os.getuid()}:{os.getgid()}"
        # Run shadow scalper inside the existing multihedge container to avoid DB lock contention
        # Source .env to get JUPITER_API_KEY and OPENROUTER_API_KEY
        shadow = run([
            "docker", "exec", "--user", runtime_user,
            "--env-file", str(DATA / "agent.env"),
            "multihedge", "bash", "-c",
            "set -a && source /app/.env && set +a && python /app/dynamic_shadow_scalper.py",
        ])
        shadow_result = last_json(shadow.stdout)
        if shadow.returncode != 0 or shadow_result is None:
            print(json.dumps({"state": "HOLD", "reason": "shadow_scalper_failed",
                              "exit_code": shadow.returncode,
                              "stderr": tail(shadow.stderr)}))
            return 1

        forced_path = logs / "forced_exit.json"
        try:
            forced_exit = json.loads(forced_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            forced_exit = {}
        interval = 300
        bucket = int(__import__("time").time() // interval)
        marker = logs / "last_entry_bucket"
        last_bucket = marker.read_text(encoding="utf-8").strip() if marker.exists() else ""
        if not forced_exit and last_bucket == str(bucket):
            print(json.dumps({"state": "SCALP_MONITOR_COMPLETE", "shadow": shadow_result,
                              "agent": {"state": "NOT_DUE"}, "signer": {"handled": []}},
                             sort_keys=True))
            return 0
        marker.write_text(str(bucket), encoding="utf-8")
        os.chmod(marker, 0o600)

        agent = run([
            "docker", "run", "--rm", "--read-only", "--user", runtime_user,
            "--cap-drop=ALL", "--security-opt=no-new-privileges",
            "--env-file", str(DATA / "agent.env"),
            "-e", "MULTIHEDGE_EVIDENCE_DB=/data/multihedge.db",
            "-e", "MULTIHEDGE_LIVE_QUEUE=/queue",
            "-e", "MULTIHEDGE_AUTONOMOUS_LOG=/logs/cycles.jsonl",
            "-e", "MULTIHEDGE_FORCED_EXIT=/logs/forced_exit.json",
            "-v", f"{DATA / 'multihedge.db'}:/data/multihedge.db:ro",
            "-v", f"{queue}:/queue:rw", "-v", f"{logs}:/logs:rw",
            IMAGE, "python", "/app/autonomous_live.py",
        ])
        agent_result = last_json(agent.stdout)
        if agent.returncode != 0 or agent_result is None:
            print(json.dumps({"state": "HOLD", "reason": "agent_cycle_failed",
                              "exit_code": agent.returncode,
                              "stderr": tail(agent.stderr)}))
            return 1

        signer = run([
            "docker", "run", "--rm", "--read-only", "--user", runtime_user,
            "--cap-drop=ALL", "--security-opt=no-new-privileges",
            "--env-file", str(DATA / "signer.env"),
            "-e", "MULTIHEDGE_EVIDENCE_DB=/app/multihedge.db",
            "-e", "MULTIHEDGE_LIVE_QUEUE=/queue",
            "-e", "MULTIHEDGE_LIVE_ORDERS_DB=/state/live_orders.db",
            "-v", f"{DATA / 'multihedge.db'}:/app/multihedge.db:rw",
            "-v", f"{queue}:/queue:rw", "-v", f"{state}:/state:rw",
            "-v", f"{ROOT / 'confirm_live.flag'}:/app/confirm_live.flag:ro",
            IMAGE, "python", "/app/live_signer_worker.py",
        ])
        signer_result = last_json(signer.stdout)
        if signer.returncode != 0 or signer_result is None:
            print(json.dumps({"state": "HOLD", "reason": "signer_cycle_failed",
                              "agent": agent_result, "exit_code": signer.returncode,
                              "stderr": tail(signer.stderr)}))
            return 1
        print(json.dumps({"state": "AUTONOMOUS_CYCLE_COMPLETE", "shadow": shadow_result,
                          "agent": agent_result, "signer": signer_result}, sort_keys=True))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
