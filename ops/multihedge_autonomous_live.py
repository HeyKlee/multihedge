#!/usr/bin/env python3
"""Run one key-isolated MultiHedge autonomous cycle."""

import fcntl
import json
import os
from pathlib import Path
import subprocess

ROOT = Path("/home/kelly/multihedge")
DATA = ROOT / "deploy/data"
IMAGE = "deploy-multihedge"


def run(command):
    return subprocess.run(command, text=True, capture_output=True, timeout=180, check=False)


def last_json(text):
    for line in reversed([line.strip() for line in text.splitlines() if line.strip()]):
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                return value
        except ValueError:
            continue
    return None


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
        agent = run([
            "docker", "run", "--rm", "--read-only", "--user", runtime_user,
            "--cap-drop=ALL", "--security-opt=no-new-privileges",
            "--env-file", str(DATA / "agent.env"),
            "-e", "MULTIHEDGE_EVIDENCE_DB=/data/multihedge.db",
            "-e", "MULTIHEDGE_LIVE_QUEUE=/queue",
            "-e", "MULTIHEDGE_AUTONOMOUS_LOG=/logs/cycles.jsonl",
            "-v", f"{DATA / 'multihedge.db'}:/data/multihedge.db:ro",
            "-v", f"{queue}:/queue:rw", "-v", f"{logs}:/logs:rw",
            IMAGE, "python", "/app/autonomous_live.py",
        ])
        agent_result = last_json(agent.stdout)
        if agent.returncode != 0 or agent_result is None:
            print(json.dumps({"state": "HOLD", "reason": "agent_cycle_failed",
                              "exit_code": agent.returncode}))
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
                              "agent": agent_result, "exit_code": signer.returncode}))
            return 1
        print(json.dumps({"state": "AUTONOMOUS_CYCLE_COMPLETE", "agent": agent_result,
                          "signer": signer_result}, sort_keys=True))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
