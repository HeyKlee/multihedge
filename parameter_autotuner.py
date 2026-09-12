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
            excursions = [dict(r) for r in rows]
            for item in excursions:
                try:
                    samples = con.execute(
                        "SELECT sample_ts,price_usd FROM mh_scalp_price_samples "
                        "WHERE mint=? AND opened_ts=? ORDER BY sample_ts ASC",
                        (item["mint"], item["open_ts"]),
                    ).fetchall()
                except sqlite3.Error:
                    samples = []
                item["samples"] = [
                    {"sample_ts": float(r[0]), "price_usd": float(r[1])} for r in samples
                ]
    except sqlite3.Error:
        return []
    return excursions


def _simulate_trade(x: dict, params: dict, *, round_trip_cost_pct: float = 0.0) -> float | None:
    """Replay one observed price path using the exact production exit order.

    None means the path cannot prove an exit for this candidate. In particular,
    a longer max-hold cannot borrow the original trade's earlier close price.
    Returns are net of the supplied round-trip execution-cost estimate.
    """
    tp = float(params["take_profit_pct"])
    sl = float(params["stop_loss_pct"])
    hold = float(params["max_hold_seconds"])
    trail_arm = float(params.get("trail_arm_pct", 0.08))
    trail_distance = float(params.get("trail_distance_pct", 0.04))
    entry = float(x["entry_usd"])
    opened = float(x["open_ts"])
    samples = x.get("samples") or []
    if entry <= 0 or not samples or abs(float(samples[0]["sample_ts"]) - opened) > 1:
        return None
    peak = entry
    for sample in samples:
        price = float(sample["price_usd"])
        sample_ts = float(sample["sample_ts"])
        if price <= 0 or sample_ts < opened:
            return None
        peak = max(peak, price)
        change = price / entry - 1.0
        if change >= tp:
            return tp - round_trip_cost_pct
        if change <= sl:
            return sl - round_trip_cost_pct
        if (sample_ts - opened >= hold
                and peak / entry - 1.0 >= tp):
            return change - round_trip_cost_pct
        if (peak / entry - 1.0 >= trail_arm
                and price / peak - 1.0 <= -trail_distance):
            return price / entry - 1.0 - round_trip_cost_pct
    return None


def _expectancy(excursions: list[dict], params: dict, *,
                round_trip_cost_pct: float = 0.0) -> float | None:
    """Mean replay return, or None if any path is insufficient/censored."""
    if not excursions:
        return None
    outcomes = [
        _simulate_trade(x, params, round_trip_cost_pct=round_trip_cost_pct)
        for x in excursions
    ]
    if any(value is None for value in outcomes):
        return None
    return sum(outcomes) / len(outcomes)


def _win_rate(excursions, params, *, round_trip_cost_pct: float = 0.0) -> float:
    if not excursions:
        return 0.0
    outcomes = [
        _simulate_trade(x, params, round_trip_cost_pct=round_trip_cost_pct)
        for x in excursions
    ]
    if any(value is None for value in outcomes):
        return 0.0
    return sum(1 for value in outcomes if value > 0) / len(outcomes)


def _grid(mode: str):
    if mode == "SERIOUS":
        return SERIOUS_TP_GRID, SERIOUS_SL_GRID, SERIOUS_HOLD_GRID
    return MEME_TP_GRID, MEME_SL_GRID, MEME_HOLD_GRID


def _walk_forward(excursions, params, *, holdout: float,
                  round_trip_cost_pct: float = 0.0) -> tuple[float | None, float | None]:
    """Chronological split; return (train_expectancy, holdout_expectancy)."""
    n = len(excursions)
    cut = int(n * (1.0 - holdout))
    train = excursions[:cut]
    test = excursions[cut:]
    return (
        _expectancy(train, params, round_trip_cost_pct=round_trip_cost_pct),
        _expectancy(test, params, round_trip_cost_pct=round_trip_cost_pct),
    )


def maybe_tune(db_path, cfg, *, now=None) -> dict:
    """Run the autonomous tuning pass. Returns a summary dict; persists an
    override only when evidence supports a strict improvement."""
    import time as _time
    now = _time.time() if now is None else now
    db_path = Path(db_path)
    quote_bps = float((cfg or {}).get("paper", {}).get("quote_bps", 40))
    if quote_bps < 0 or quote_bps > 500:
        raise ValueError("invalid paper quote cost")
    round_trip_cost_pct = 2 * quote_bps / 10000.0
    report = {"state": "NO_CHANGE", "tuned": {}, "evaluation": {}}
    for mode in ("MEME", "SERIOUS"):
        excursions = load_excursions(db_path, mode)
        if len(excursions) < MIN_SAMPLE_CLOSED:
            report["evaluation"][mode] = {
                "state": "INSUFFICIENT_HISTORY",
                "closed": len(excursions), "required": MIN_SAMPLE_CLOSED,
            }
            continue
        path_ready = [
            x for x in excursions if x.get("samples")
            and abs(float(x["samples"][0]["sample_ts"]) - float(x["open_ts"])) <= 1
        ]
        if len(path_ready) < MIN_SAMPLE_CLOSED:
            report["evaluation"][mode] = {
                "state": "INSUFFICIENT_PRICE_PATHS", "closed": len(excursions),
                "path_ready": len(path_ready), "required": MIN_SAMPLE_CLOSED,
            }
            continue
        excursions = path_ready
        incumbent = _risk_params_override(db_path, mode) or _default_params(mode)
        inc_train, inc_hold = _walk_forward(
            excursions, incumbent, holdout=HOLDOUT_FRACTION,
            round_trip_cost_pct=round_trip_cost_pct)
        if inc_train is None or inc_hold is None:
            report["evaluation"][mode] = {
                "state": "INCUMBENT_PATH_CENSORED", "closed": len(excursions),
            }
            continue
        best = None
        best_train = -1e18
        best_hold = -1e18
        for tp in _grid(mode)[0]:
            for sl in _grid(mode)[1]:
                for hold in _grid(mode)[2]:
                    cand = {
                        "take_profit_pct": tp, "stop_loss_pct": sl,
                        "trail_arm_pct": incumbent.get("trail_arm_pct", 0.08),
                        "trail_distance_pct": incumbent.get("trail_distance_pct", 0.04),
                        "max_hold_seconds": hold, "mode": mode,
                    }
                    tr, ho = _walk_forward(
                        excursions, cand, holdout=HOLDOUT_FRACTION,
                        round_trip_cost_pct=round_trip_cost_pct)
                    if tr is None or ho is None:
                        continue
                    if tr > best_train:
                        best_train, best_hold, best = tr, ho, cand
        if best is None:
            continue
        adopted = (
            best_hold > inc_hold + IMPROVEMENT_MARGIN
            and best_train > inc_train + IMPROVEMENT_MARGIN
            and _win_rate(
                excursions, best, round_trip_cost_pct=round_trip_cost_pct
            ) >= 0.40
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
            "win_rate": round(_win_rate(
                excursions, best, round_trip_cost_pct=round_trip_cost_pct), 4),
            "round_trip_cost_pct": round(round_trip_cost_pct, 6),
        }
    return report


if __name__ == "__main__":
    import yaml as _yaml
    root = Path(__file__).resolve().parent
    cfg = _yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
    db = Path(os.getenv("MULTIHEDGE_EVIDENCE_DB", str(root / "multihedge.db")))
    print(json.dumps(maybe_tune(db, cfg), sort_keys=True))