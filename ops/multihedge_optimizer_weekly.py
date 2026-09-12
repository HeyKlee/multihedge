#!/usr/bin/env python3
"""Weekly read-only MultiHedge optimiser run.

Closes the self-improvement loop that has no scheduler: continuous_optimizer.py
was only ever run by hand, so its challenger and promotion gates produced no
periodic record.

This runner snapshots the authoritative SQLite database with the backup API (a
running writer cannot produce a torn read), runs the read-only evaluator over
the snapshot, and writes optimization-results/<timestamp>/{baseline.json,report.md}.

It never writes to the trading database and never changes configuration,
parameters, positions, or the live gate. A rejected challenger is a valid,
successful result and exits 0; only a genuine execution failure exits non-zero.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def snapshot(source: Path, dest: Path) -> None:
    """Copy a live SQLite database consistently, without opening it for write."""
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=30)
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(ROOT / "deploy/data/multihedge.db"))
    parser.add_argument("--output-dir", default=str(ROOT / "optimization-results"))
    parser.add_argument("--per-side-bps", type=float, default=40.0)
    parser.add_argument("--fixed-cost-usd", type=float, default=0.0)
    args = parser.parse_args(argv)

    import continuous_optimizer as opt

    source = Path(args.db)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = Path(args.output_dir) / stamp
    out.mkdir(parents=True, exist_ok=True)
    snap = out / "authoritative-prechange.db"
    snapshot(source, snap)

    report = opt.analyse(snap, args.per_side_bps, args.fixed_cost_usd)
    (out / "baseline.json").write_text(
        json.dumps(report, indent=2, sort_keys=True, allow_nan=False), encoding="utf-8")
    (out / "report.md").write_text(opt.render_markdown(report), encoding="utf-8")

    gate = report.get("challenger", {}).get("promotion_gate", {})
    print(json.dumps({
        "state": "OPTIMIZER_RUN_COMPLETE",
        "output_dir": str(out.resolve()),
        "report_md": str((out / "report.md").resolve()),
        "source_db": str(source),
        "deployment_state": report.get("deployment_state"),
        "promotion_gate_accepted": gate.get("accepted"),
        "promotion_gate_reasons": gate.get("reasons"),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
