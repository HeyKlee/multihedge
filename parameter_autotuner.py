"""Autonomous parameter autotuner for the XORA-SURVIVAL scalp strategy.

Reads closed paper-trade excursion history (per-trade peak / trough / hold
time) and searches for TP / SL / max-hold params per coin class (MEME vs
SERIOUS) that beat the currently applied params on a walk-forward basis.
It is deliberately conservative:

  * Never tunes on fewer than MIN_SAMPLE_CLOSED closed trades per class.
  * Only adopts a candidate if it strictly beats the incumbent on an
    out-of-sample holdout (chronological split) by a margin and stays within
    sane bounds.
  * Persists via live_inventory.set_risk_params_override, whose SQLite CHECK
    constraint rejects degenerate params outright.
  * With insufficient evidence it returns the incumbent (no write).

This module never signs or submits; it only proposes exit parameters, and the
same override store is what risk_params()/forced_exit() actually consume.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3

from live_inventory import (
    _default_params,
    _risk_params_override,
    mode_for_mint,
    set_risk_params_override,
    MEME_TAKE_PROFIT_PCT,
    MEME_STOP_LOSS_PCT,
    MEME_MAX_HOLD_SECONDS,
    SERIOUS_TAKE_PROFIT_PCT,
    SERIOUS_STOP_LOSS_PCT,
    SERIOUS_MAX_HOLD_SECONDS,
)

MIN_SAMPLE_CLOSED = 30              # closed trades per class before any tuning
HOLDOUT_FRACTION = 0.25             # chronological tail held out for validation
IMPROVEMENT_MARGIN = 0.02           # candidate must beat incumbent expectancy by 2%
BIG_HOLD_S = 14 * 3600              # "day trade" hold floor for SERIOUS (14h)

# Candidate grids (proportion / seconds). Deliberately modest so the search
# stays near the current defaults and cannot explore absurd values.
MEME_TP_GRID = (0.15, 0.20, 0.25)
MEME_SL_GRID = (-0.12, -0.10, -0.08)
MEME_HOLD_GRID = (600, 900, 1800)

SERIOUS_TP_GRID = (0.04, 0.05, 0.06)
SERIOUS_SL_GRID = (-0.03, -0.025, -0.02)
SERIOUS_HOLD_GRID = (BIG_HOLD_S, SERIOUS_MAX_HOLD_SECONDS)


def _connect(path):
    con = sqlite3.connect(Path(path), timeout=30)
    con.row_factory = sqlite3.Row
    return con


def load_excursions(db_path, mode: str) -> list[dict]:
    """Closed paper-trade excursion rows for one class, oldest first."""
    try:
        with _connect(db_path) as con:
            rows = con.execute(
                "SELECT mint,ticker,mode,entry_usd,peak_usd,trough_usd,"
                "open_ts,close_ts,hold_seconds,realized_pct,exit_reason "
                "FROM mh_scalp_excursions WHERE mode=? ORDER BY close_ts ASC",
                (mode,),
            ).fetchall()
    except sqlite3.Error:
        return []
    return [dict(r) for r in rows]


def _expectancy(excursions: list[dict], params: dict, *, notional_usd: float = 1.0) -> float:
    """Simulate each closed trade under a candidate TP/SL/max-hold using its
    real observed peak/trough extremes.

    Conservative ordering: if both TP and SL were pierced we cannot know the
    true order, so we count the stop (adverse) first. That biases against
    optimistic TP claims, which is the safe direction.
    """
    tp = float(params["take_profit_pct"])
    sl = float(params["stop_loss_pct"])
    hold = float(params["max_hold_seconds"])
    total = 0.0
    for x in excursions:
        entry = float(x["entry_usd"])
        peak_pct = float(x["peak_usd"]) / entry - 1.0
        trough_pct = float(x["trough_usd"]) / entry - 1.0
        hit_tp = peak_pct >= tp
        hit_sl = trough_pct <= sl
        if hit_sl:
            # Stop hit (worst case, could be before TP or not at all).
            pct = sl
        elif hit_tp:
            pct = tp
        elif float(x["hold_seconds"]) >= hold:
            # Never reached either target before max-hold; realised whatever
            # the market gave within the allowed window.
            pct = float(x["realized_pct"])
        else:
            continue  # trade still open under this hold -> contributes nothing
        total += pct
    return total / len(excursions) if excursions else 0.0


def _win_rate(excursions, params) -> float:
    tp = float(params["take_profit_pct"])
    sl = float(params["stop_loss_pct"])
    if not excursions:
        return 0.0
    wins = 0
    for x in excursions:
        entry = float(x["entry_usd"])
        if (float(x["peak_usd"]) / entry - 1.0) >= tp:
            wins += 1
        elif (float(x["trough_usd"]) / entry - 1.0) <= sl:
            continue
        elif float(x["realized_pct"]) > 0:
            wins += 1
    return wins / len(excursions)


def _grid(mode: str):
    if mode == "SERIOUS":
        return SERIOUS_TP_GRID, SERIOUS_SL_GRID, SERIOUS_HOLD_GRID
    return MEME_TP_GRID, MEME_SL_GRID, MEME_HOLD_GRID


def _walk_forward(excursions, params, *, holdout: float) -> tuple[float, float]:
    """Chronological split; return (train_expectancy, holdout_expectancy)."""
    n = len(excursions)
    cut = int(n * (1.0 - holdout))
    train = excursions[:cut]
    test = excursions[cut:]
    return _expectancy(train, params), _expectancy(test, params)


def maybe_tune(db_path, cfg, *, now=None) -> dict:
    """Run the autonomous tuning pass. Returns a summary dict; persists an
    override only when evidence supports a strict improvement."""
    import time as _time
    now = _time.time() if now is None else now
    db_path = Path(db_path)
    report = {"state": "NO_CHANGE", "tuned": {}, "evaluation": {}}
    for mode in ("MEME", "SERIOUS"):
        excursions = load_excursions(db_path, mode)
        if len(excursions) < MIN_SAMPLE_CLOSED:
            report["evaluation"][mode] = {
                "state": "INSUFFICIENT_HISTORY",
                "closed": len(excursions), "required": MIN_SAMPLE_CLOSED,
            }
            continue
        incumbent = _risk_params_override(db_path, mode) or _default_params(mode)
        inc_train, inc_hold = _walk_forward(
            excursions, incumbent, holdout=HOLDOUT_FRACTION)
        best = None
        best_train = -1e18
        best_hold = -1e18
        for tp in _grid(mode)[0]:
            for sl in _grid(mode)[1]:
                for hold in _grid(mode)[2]:
                    cand = {
                        "take_profit_pct": tp, "stop_loss_pct": sl,
                        "trail_arm_pct": incumbent.get("trail_arm_pct", 0.02),
                        "trail_distance_pct": incumbent.get("trail_distance_pct", 0.01),
                        "max_hold_seconds": hold, "mode": mode,
                    }
                    tr, ho = _walk_forward(excursions, cand, holdout=HOLDOUT_FRACTION)
                    if tr > best_train:
                        best_train, best_hold, best = tr, ho, cand
        if best is None:
            continue
        adopted = (
            best_hold > inc_hold + IMPROVEMENT_MARGIN
            and best_train > inc_train + IMPROVEMENT_MARGIN
            and _win_rate(excursions, best) >= 0.40  # not a degenerate SL-fed policy
        )
        if adopted:
            set_risk_params_override(
                db_path, best, source=f"autotuner@{int(now)}", sample_n=len(excursions))
            report["state"] = "TUNED"
            report["tuned"][mode] = {
                **{k: best[k] for k in
                   ("take_profit_pct", "stop_loss_pct", "max_hold_seconds", "mode")},
                "source": "autotuner",
            }
        report["evaluation"][mode] = {
            "state": "TUNED" if adopted else "NOT_IMPROVED",
            "closed": len(excursions),
            "incumbent_holdout_expectancy": round(inc_hold, 6),
            "candidate_holdout_expectancy": round(best_hold, 6),
            "candidate_train_expectancy": round(best_train, 6),
            "win_rate": round(_win_rate(excursions, best), 4),
        }
    return report


if __name__ == "__main__":
    import yaml as _yaml
    root = Path(__file__).resolve().parent
    cfg = _yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
    db = Path(os.getenv("MULTIHEDGE_EVIDENCE_DB", str(root / "multihedge.db")))
    print(json.dumps(maybe_tune(db, cfg), sort_keys=True))