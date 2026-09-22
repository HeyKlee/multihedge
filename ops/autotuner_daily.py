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
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import yaml
from parameter_autotuner import maybe_tune


def main() -> int:
    db_path = Path(os.getenv("MULTIHEDGE_EVIDENCE_DB", str(ROOT / "deploy/data/multihedge.db")))
    cfg_path = ROOT / "config.yaml"
    
    if not db_path.exists():
        print(f"ERROR: Database not found at {db_path}", file=sys.stderr)
        return 1
    if not cfg_path.exists():
        print(f"ERROR: Config not found at {cfg_path}", file=sys.stderr)
        return 1
    
    with cfg_path.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    
    # Run the autotuner
    now = time.time()
    report = maybe_tune(db_path, cfg, now=now)
    
    # Log the result
    log_dir = ROOT / "autotuner-logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    log_file = log_dir / f"autotune_{stamp}.json"
    
    with log_file.open("w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, sort_keys=True, allow_nan=False, default=str)
    
    # Print summary
    state = report.get("state", "UNKNOWN")
    print(f"[{stamp}] Autotuner run: {state}")
    for mode in ("MEME", "SERIOUS"):
        eval_data = report.get("evaluation", {}).get(mode, {})
        ev_state = eval_data.get("state", "UNKNOWN")
        closed = eval_data.get("closed", 0)
        inc_hold = eval_data.get("incumbent_holdout_expectancy", "N/A")
        cand_hold = eval_data.get("candidate_holdout_expectancy", "N/A")
        wr = eval_data.get("win_rate", "N/A")
        print(f"  {mode}: {ev_state} | closed={closed} | inc_hold={inc_hold} | cand_hold={cand_hold} | wr={wr}")
        if "tuned" in report.get("tuned", {}) and mode in report["tuned"]:
            tuned = report["tuned"][mode]
            print(f"    → TUNED: TP={tuned['take_profit_pct']:.1%} SL={tuned['stop_loss_pct']:.1%} "
                  f"Trail={tuned['trail_arm_pct']:.1%}/{tuned['trail_distance_pct']:.1%} "
                  f"Hold={tuned['max_hold_seconds']}s")
    
    print(f"Log saved: {log_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())