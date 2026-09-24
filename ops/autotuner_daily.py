#!/usr/bin/env python3
"""Daily autonomous parameter autotuner for XORA-SURVIVAL (paper-only, evidence-gated).

Runs parameter_autotuner.maybe_tune() on the live database, logs the evaluation
report, and never promotes to live trading. All changes remain paper-only
shadow parameters until evidence gates are satisfied.

This is the daily implementation of the "24h test adjustments, 6-day confirmation"
workflow requested by HeyKlee. The autotuner already enforces:
- MIN_SAMPLE_CLOSED = 30 closed trades per class before tuning
- Chronological walk-forward validation with 25% holdout
- IMPROVEMENT_MARGIN = 2pp over incumbent on holdout
- Win rate >= 40% requirement
- Round-trip cost modeling (quote_bps from config.yaml)

Live promotion requires separate evidence gate in config.yaml:
  autonomous.autotune_live_promotion_enabled: false (default)

Routing note (fail closed): the production database deploy/data/multihedge.db
is a WAL-mode SQLite file bind-mounted into the multihedge container as a
SINGLE FILE, not a directory. Container services keep their WAL sidecars
inside the container filesystem while host processes create separate sidecars
beside the host copy. Two transaction universes over one shared main file
silently corrupt host-side reads once the container checkpoints over host-WAL
pages (observed 2026-09-25: host readers got "database disk image is
malformed" while the container stayed healthy). Any host-side write against
the production DB must therefore execute inside the container instead. When
the production path is targeted and no safe container route exists, this
script refuses to run rather than falling back to a host-side write.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import yaml
from parameter_autotuner import maybe_tune

CONTAINER_NAME = "multihedge"
REPORT_SENTINEL = "__AUTOTUNE_REPORT__"

# Runs inside the container: uses the container's canonical config and DB so
# the evaluation row lands in the container's WAL universe (the only writer
# universe that checkpoints correctly into the shared main file).
BOOTSTRAP = """
import json, sys, time
sys.path.insert(0, "/app")
import yaml
from parameter_autotuner import maybe_tune
with open("/app/config.yaml", encoding="utf-8") as fh:
    cfg = yaml.safe_load(fh)
report = maybe_tune("/app/multihedge.db", cfg, now=time.time())
sys.stdout.write({sentinel!r} + json.dumps(report, sort_keys=True, allow_nan=False, default=str) + "\\n")
""".format(sentinel=REPORT_SENTINEL)


def _route_command(db_path: Path, root: Path, *, docker_exe: str | None,
                   container_running: bool, image_ready: bool) -> list[str] | None:
    """Decide how this run may touch db_path.

    Returns the argv to execute the tuning inside the container when db_path is
    the container-managed production DB. Returns None when a local run is
    correct (dev or temporary databases). Raises RuntimeError when the
    production DB is targeted but no safe route exists: a host-side write to
    the shared WAL file corrupts host readers, so refusing is the only safe
    outcome.
    """
    prod_db = (root / "deploy/data/multihedge.db").resolve()
    if Path(db_path).resolve() != prod_db:
        return None
    if not docker_exe or not container_running:
        raise RuntimeError(
            "production DB is container-managed but no running multihedge "
            "container is available; refusing host-side WAL write")
    if not image_ready:
        raise RuntimeError(
            "container lacks parameter_autotuner.py/config.yaml; "
            "refusing host-side WAL write")
    return [docker_exe, "exec", "-i", "-e",
            "MULTIHEDGE_EVIDENCE_DB=/app/multihedge.db",
            CONTAINER_NAME, "python3", "-"]


def _container_running(docker_exe: str, name: str) -> bool:
    try:
        probe = subprocess.run([docker_exe, "ps", "--format", "{{.Names}}"],
                               capture_output=True, text=True, timeout=15)
    except (subprocess.SubprocessError, OSError):
        return False
    return name in probe.stdout.split()


def _image_ready(docker_exe: str, name: str) -> bool:
    try:
        probe = subprocess.run(
            [docker_exe, "exec", name, "python3", "-c",
             "import os,sys; sys.exit(0 if os.path.exists("
             "'/app/parameter_autotuner.py') and os.path.exists("
             "'/app/config.yaml') else 1)"],
            capture_output=True, timeout=30)
    except (subprocess.SubprocessError, OSError):
        return False
    return probe.returncode == 0


def _print_summary(report: dict, log_file: Path) -> None:
    state = report.get("state", "UNKNOWN")
    print(f"Log saved: {log_file}")
    print(f"Autotuner run: {state}")
    for mode in ("MEME", "SERIOUS"):
        eval_data = report.get("evaluation", {}).get(mode, {})
        print(f"  {mode}: {eval_data.get('state', 'UNKNOWN')} "
              f"| closed={eval_data.get('closed', 0)} "
              f"| inc_hold={eval_data.get('incumbent_holdout_expectancy', 'N/A')} "
              f"| cand_hold={eval_data.get('candidate_holdout_expectancy', 'N/A')} "
              f"| wr={eval_data.get('win_rate', 'N/A')}")


def _save_report(report: dict) -> Path:
    log_dir = ROOT / "autotuner-logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"autotune_{time.strftime('%Y%m%d-%H%M%S')}.json"
    with log_file.open("w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, sort_keys=True, allow_nan=False, default=str)
    return log_file


def main() -> int:
    db_path = Path(os.getenv("MULTIHEDGE_EVIDENCE_DB", str(ROOT / "deploy/data/multihedge.db")))
    cfg_path = ROOT / "config.yaml"

    docker_exe = shutil.which("docker")
    container_running = bool(docker_exe) and _container_running(docker_exe, CONTAINER_NAME)
    try:
        cmd = _route_command(db_path, ROOT, docker_exe=docker_exe,
                             container_running=container_running,
                             image_ready=bool(docker_exe) and container_running
                             and _image_ready(docker_exe, CONTAINER_NAME))
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if cmd is not None:
        # Production DB: run the tuning inside the container so every write
        # lands in the container's WAL universe.
        try:
            proc = subprocess.run(cmd, input=BOOTSTRAP, capture_output=True,
                                  text=True, timeout=900)
        except subprocess.SubprocessError as exc:
            print(f"ERROR: container route failed: {exc}", file=sys.stderr)
            return 1
        visible = "\n".join(
            line for line in proc.stdout.splitlines()
            if not line.startswith(REPORT_SENTINEL))
        if visible:
            sys.stdout.write(visible + "\n")
        if proc.stderr:
            sys.stderr.write(proc.stderr)
        report = None
        for line in proc.stdout.splitlines():
            if line.startswith(REPORT_SENTINEL):
                try:
                    report = json.loads(line[len(REPORT_SENTINEL):])
                except json.JSONDecodeError:
                    report = None
        if report is None:
            print("ERROR: container run produced no parseable report", file=sys.stderr)
            return proc.returncode or 1
        log_file = _save_report(report)
        _print_summary(report, log_file)
        return proc.returncode

    if not db_path.exists():
        print(f"ERROR: Database not found at {db_path}", file=sys.stderr)
        return 1
    if not cfg_path.exists():
        print(f"ERROR: Config not found at {cfg_path}", file=sys.stderr)
        return 1

    with cfg_path.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    report = maybe_tune(db_path, cfg, now=time.time())
    log_file = _save_report(report)
    _print_summary(report, log_file)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())