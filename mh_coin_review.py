"""Per-coin trade review for the XORA-SURVIVAL shadow incubator.

Reviews the most recent closed trades of EACH coin individually, never grouped,
diagnoses what happened and why from the recorded price path, and then lets a
deterministic gate decide whether a bounded exit-parameter change may be applied
to that coin alone.

Boundaries this module respects deliberately:

  * It observes and diagnoses only. Any proposed change is validated by replaying
    the coin's own recorded price paths, not by opinion.
  * An applied change is per-mint (the canonical asset identity), paper/shadow
    only, bounded by the mh_coin_risk_params CHECK constraints, rate-limited by a
    cooldown, and never touches live risk. Live callers of risk_params() pass
    allow_tuned=False and are unaffected.
  * Every review is written to mh_coin_review_log, accepted or rejected, so a
    silent no-op is visible next to a refusal.

CLI:
  python3 mh_coin_review.py --collect [--limit 5]
  python3 mh_coin_review.py --apply-file proposals.json
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from pathlib import Path

SETUP = "dynamic_scalper"
TRADES_PER_COIN = 5
MIN_PATHS_FOR_PROPOSAL = 5
REPLAY_MARGIN = 0.02
MIN_REPLAY_WIN_RATE = 0.40
REVIEW_COOLDOWN_SECONDS = 6 * 3600
HOLDOUT_FRACTION = 0.25

PROPOSABLE_KEYS = (
    "take_profit_pct", "stop_loss_pct", "trail_arm_pct",
    "trail_distance_pct", "max_hold_seconds",
)

BOUNDS = {
    "take_profit_pct": (0.005, 0.60),
    "stop_loss_pct": (-0.35, -0.005),
    "trail_arm_pct": (0.001, 0.60),
    "trail_distance_pct": (0.001, 0.60),
    "max_hold_seconds": (60.0, 604800.0),
}


def _connect(path) -> sqlite3.Connection:
    con = sqlite3.connect(Path(path), timeout=30)
    con.row_factory = sqlite3.Row
    return con


# ------------------------------------------------------------------ evidence --

def recent_trades(db_path, mint: str, limit: int = TRADES_PER_COIN) -> list[dict]:
    """The most recent closed trades for ONE mint, newest first. Not grouped."""
    try:
        with _connect(db_path) as con:
            rows = con.execute(
                "SELECT id,coin,symbol,setup,side,open_ts,close_ts,entry_px,exit_px,"
                "qty,realized_pct,realized_usd,exit_reason FROM mh_trades "
                "WHERE coin=? ORDER BY close_ts DESC LIMIT ?",
                (str(mint), int(limit)),
            ).fetchall()
    except sqlite3.Error:
        return []
    return [dict(r) for r in rows]


def _excursion_for(db_path, mint: str, open_ts: float) -> dict | None:
    try:
        with _connect(db_path) as con:
            row = con.execute(
                "SELECT peak_usd,trough_usd,hold_seconds,exit_reason,entry_usd "
                "FROM mh_scalp_excursions WHERE mint=? AND open_ts=?",
                (str(mint), float(open_ts)),
            ).fetchone()
    except sqlite3.Error:
        return None
    return dict(row) if row else None


def price_path(db_path, mint: str, open_ts: float) -> list[dict]:
    """Recorded price samples for one position, oldest first."""
    try:
        with _connect(db_path) as con:
            rows = con.execute(
                "SELECT sample_ts,price_usd FROM mh_scalp_price_samples "
                "WHERE mint=? AND opened_ts=? ORDER BY sample_ts ASC",
                (str(mint), float(open_ts)),
            ).fetchall()
    except sqlite3.Error:
        return []
    return [{"sample_ts": float(r["sample_ts"]), "price_usd": float(r["price_usd"])}
            for r in rows]


def trackable(db_path, mint: str) -> list[dict]:
    """Replayable excursion rows for one coin, oldest first."""
    try:
        with _connect(db_path) as con:
            rows = con.execute(
                "SELECT mint,ticker,mode,entry_usd,peak_usd,trough_usd,open_ts,close_ts,"
                "hold_seconds,realized_pct,exit_reason FROM mh_scalp_excursions "
                "WHERE mint=? ORDER BY close_ts ASC",
                (str(mint),),
            ).fetchall()
    except sqlite3.Error:
        return []
    out = []
    for row in rows:
        item = dict(row)
        item["samples"] = price_path(db_path, mint, item["open_ts"])
        if item["samples"] and abs(item["samples"][0]["sample_ts"] - item["open_ts"]) <= 1:
            out.append(item)
    return out


# ---------------------------------------------------------------- diagnosis --

def metrics(trades: list[dict]) -> dict:
    """Per-coin window metrics. Small n is reported honestly, not smoothed."""
    n = len(trades)
    if n == 0:
        return {"n": 0}
    wins = [t for t in trades if (t.get("realized_usd") or 0) > 0]
    losses = [t for t in trades if (t.get("realized_usd") or 0) < 0]
    avg_win = sum(t["realized_usd"] for t in wins) / len(wins) if wins else 0.0
    avg_loss = sum(t["realized_usd"] for t in losses) / len(losses) if losses else 0.0
    exits: dict[str, int] = {}
    for t in trades:
        key = str(t.get("exit_reason") or "unknown")
        exits[key] = exits.get(key, 0) + 1
    return {
        "n": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / n, 4),
        "sum_pct": round(sum(t["realized_pct"] for t in trades), 6),
        "sum_usd": round(sum(t["realized_usd"] for t in trades), 6),
        "expectancy_usd": round(sum(t["realized_usd"] for t in trades) / n, 6),
        "avg_win_usd": round(avg_win, 6),
        "avg_loss_usd": round(avg_loss, 6),
        "payoff_ratio": round(abs(avg_win / avg_loss), 3) if avg_loss else None,
        "exits": exits,
        "window_span_seconds": round(
            max(t["close_ts"] for t in trades) - min(t["open_ts"] for t in trades), 1),
    }


def diagnose(case: dict) -> list[dict]:
    """Deterministic 'what happened and why' findings for ONE coin.

    Each finding names the observation, the mechanism, and the parameter that
    would address it, so a reviewer (or a council) reasons from recorded facts.
    """
    findings: list[dict] = []
    m = case["metrics"]
    if m.get("n", 0) == 0:
        return [{"finding": "no_closed_trades", "detail": "this coin has no closed shadow trades yet"}]
    params = case["params"]

    if m["payoff_ratio"] is not None and m["payoff_ratio"] < 1.0 and m["win_rate"] < 0.667:
        findings.append({
            "finding": "negative_expectancy_from_payoff",
            "detail": (f"win rate {m['win_rate']:.3f} with payoff ratio {m['payoff_ratio']:.3f}: "
                       f"average win ${m['avg_win_usd']:.4f} against average loss "
                       f"${abs(m['avg_loss_usd']):.4f}. Winning more often than losing is not "
                       f"enough at this ratio."),
            "addresses": ["trail_distance_pct", "take_profit_pct", "stop_loss_pct"],
        })

    # How much of the recorded peak move each exit actually captured.
    captures = []
    for trade in case["trades"]:
        peak, entry, realized = trade.get("peak_usd"), trade.get("entry_px"), trade.get("realized_pct")
        if not peak or not entry or realized is None or entry <= 0:
            continue
        peak_move = peak / entry - 1
        if peak_move > 0.01:
            captures.append({"exit_reason": trade["exit_reason"], "captured": realized / peak_move,
                             "peak_move": peak_move, "realized": realized})
    if captures:
        worst = sorted(captures, key=lambda c: c["captured"])[:3]
        avg_capture = sum(c["captured"] for c in captures) / len(captures)
        if avg_capture < 0.50:
            findings.append({
                "finding": "gives_back_run",
                "detail": (f"captured {avg_capture:.0%} of the recorded peak move on average across "
                           f"{len(captures)} trades; worst: " +
                           ", ".join(f"{c['exit_reason']} kept {c['captured']:.0%} of a "
                                     f"{c['peak_move']:.1%} move" for c in worst)),
                "addresses": ["trail_arm_pct", "trail_distance_pct", "take_profit_pct"],
            })

    stop = float(params["stop_loss_pct"])
    overshoot = [t for t in case["trades"]
                 if t["exit_reason"] == "stop_loss" and t["realized_pct"] < stop - 0.01]
    if overshoot:
        worst = min(t["realized_pct"] for t in overshoot)
        findings.append({
            "finding": "stop_not_honoured",
            "detail": (f"{len(overshoot)} of {m['exits'].get('stop_loss', 0)} stop exits landed beyond "
                       f"the configured {stop:.1%} stop, worst {worst:.2%}. Exits are evaluated once "
                       f"per cycle, so a fast move is found late."),
            "addresses": ["stop_loss_pct"],
        })

    dead = [t for t in case["trades"] if t["exit_reason"] == "max_hold" and abs(t["realized_pct"]) < 0.01]
    if len(dead) >= 2:
        findings.append({
            "finding": "dead_max_hold_exits",
            "detail": (f"{len(dead)} of {m['n']} trades expired on the max-hold fallback moving less "
                       f"than 1%, paying the round trip for nothing."),
            "addresses": ["max_hold_seconds", "take_profit_pct"],
        })

    hold = float(params["max_hold_seconds"])
    for trade in case["trades"]:
        if trade.get("hold_seconds") and trade["hold_seconds"] > hold * 1.5:
            findings.append({
                "finding": "hold_beyond_timer",
                "detail": (f"a position was held {trade['hold_seconds']:.0f}s against a "
                           f"{hold:.0f}s max hold; the timer only closes a position whose peak "
                           f"already crossed take profit, so it is not a hard time stop."),
                "addresses": ["max_hold_seconds"],
            })
            break
    return findings


def coin_case(db_path, cfg, mint: str, ticker: str | None = None,
              limit: int = TRADES_PER_COIN) -> dict:
    from live_inventory import mode_for_mint, risk_params
    trades = recent_trades(db_path, mint, limit)
    for trade in trades:
        ex = _excursion_for(db_path, mint, trade["open_ts"])
        if ex:
            trade["peak_usd"] = ex["peak_usd"]
            trade["trough_usd"] = ex["trough_usd"]
            trade["hold_seconds"] = ex["hold_seconds"]
    params = risk_params(mint, cfg, db_path=db_path, allow_tuned=True)
    case = {
        "mint": mint,
        "ticker": (ticker or (trades[0].get("symbol") if trades else None) or "UNKNOWN"),
        "mode": mode_for_mint(mint, cfg),
        "params": {k: params[k] for k in PROPOSABLE_KEYS},
        "params_source": params.get("source", "defaults"),
        "trades": trades,
        "metrics": metrics(trades),
    }
    case["paths_available"] = len(trackable(db_path, mint))
    case["findings"] = diagnose(case)
    return case


def collect(db_path, cfg, limit: int = TRADES_PER_COIN, now: float | None = None) -> dict:
    """One review bundle: every coin with shadow activity, reviewed separately."""
    now = time.time() if now is None else now
    with _connect(db_path) as con:
        rows = con.execute(
            "SELECT coin, COALESCE(MAX(symbol),'UNKNOWN') AS ticker, COUNT(*) AS n, MAX(close_ts) AS last "
            "FROM mh_trades WHERE setup=? GROUP BY coin ORDER BY last DESC",
            (SETUP,),
        ).fetchall()
    coins = [coin_case(db_path, cfg, r["coin"], r["ticker"], limit) for r in rows]
    return {
        "generated_unix": now,
        "setup": SETUP,
        "trades_per_coin": limit,
        "cooldown_seconds": REVIEW_COOLDOWN_SECONDS,
        "coins": coins,
    }


# --------------------------------------------------------------- apply gate --

def _validate_proposal(proposal: dict) -> tuple[dict | None, str]:
    """Shape and bounds. Returns (clean_params, "") or (None, reason)."""
    unknown = set(proposal) - set(PROPOSABLE_KEYS) - {"mint", "rationale"}
    if unknown:
        return None, f"unsupported keys: {sorted(unknown)}"
    missing = [k for k in PROPOSABLE_KEYS if k not in proposal]
    if missing:
        return None, f"missing parameters: {missing}"
    clean = {}
    for key in PROPOSABLE_KEYS:
        try:
            value = float(proposal[key])
        except (TypeError, ValueError):
            return None, f"{key} is not numeric"
        low, high = BOUNDS[key]
        if not (low <= value <= high):
            return None, f"{key}={value} outside [{low}, {high}]"
        clean[key] = value
    if clean["trail_distance_pct"] >= clean["trail_arm_pct"]:
        return None, "trail distance must be below its own arm"
    return clean, ""


def _split(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    cut = int(len(rows) * (1.0 - HOLDOUT_FRACTION))
    return rows[:cut], rows[cut:]


def replay_verdict(db_path, cfg, mint: str, candidate: dict, *, cost_pct: float) -> dict:
    """Deterministic authorisation: does the candidate beat the incumbent on the
    coin's OWN recorded price paths, out of sample?"""
    import parameter_autotuner as pa
    from live_inventory import risk_params

    rows = trackable(db_path, mint)
    incumbent = risk_params(mint, cfg, db_path=db_path, allow_tuned=True)
    if len(rows) < MIN_PATHS_FOR_PROPOSAL:
        return {"authorized": False, "reason": "insufficient_replayable_paths",
                "paths": len(rows), "required": MIN_PATHS_FOR_PROPOSAL}
    train, holdout = _split(rows)
    if not train or not holdout:
        return {"authorized": False, "reason": "chronological_split_too_small", "paths": len(rows)}
    inc_train = pa._expectancy(train, incumbent, round_trip_cost_pct=cost_pct)
    inc_hold = pa._expectancy(holdout, incumbent, round_trip_cost_pct=cost_pct)
    cand_train = pa._expectancy(train, candidate, round_trip_cost_pct=cost_pct)
    cand_hold = pa._expectancy(holdout, candidate, round_trip_cost_pct=cost_pct)
    if None in (inc_train, inc_hold, cand_train, cand_hold):
        return {"authorized": False, "reason": "path_censored_under_replay",
                "paths": len(rows)}
    assert inc_train is not None and inc_hold is not None
    assert cand_train is not None and cand_hold is not None
    win_rate = pa._win_rate(rows, candidate, round_trip_cost_pct=cost_pct)
    beats = (cand_train > inc_train + REPLAY_MARGIN
             and cand_hold > inc_hold + REPLAY_MARGIN
             and win_rate >= MIN_REPLAY_WIN_RATE)
    return {
        "authorized": bool(beats),
        "reason": "beats_incumbent_out_of_sample" if beats else "no_out_of_sample_improvement",
        "paths": len(rows), "train_n": len(train), "holdout_n": len(holdout),
        "incumbent_train": round(inc_train, 6), "incumbent_holdout": round(inc_hold, 6),
        "candidate_train": round(cand_train, 6), "candidate_holdout": round(cand_hold, 6),
        "candidate_win_rate": round(win_rate, 4), "margin_required": REPLAY_MARGIN,
        "round_trip_cost_pct": round(cost_pct, 6),
    }


def _log(db_path, mint: str, ticker: str, decision: str, reason: str,
         payload: dict, applied: bool) -> None:
    with _connect(db_path) as con:
        con.execute(
            "CREATE TABLE IF NOT EXISTS mh_coin_review_log ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, mint TEXT NOT NULL,"
            "ticker TEXT NOT NULL, decision TEXT NOT NULL, reason TEXT NOT NULL,"
            "payload TEXT NOT NULL, applied INTEGER NOT NULL)"
        )
        con.execute(
            "INSERT INTO mh_coin_review_log(ts,mint,ticker,decision,reason,payload,applied) "
            "VALUES(?,?,?,?,?,?,?)",
            (time.time(), str(mint), str(ticker)[:24], decision, reason,
             json.dumps(payload, sort_keys=True, default=str), 1 if applied else 0),
        )


def _last_applied_ts(db_path, mint: str) -> float | None:
    try:
        with _connect(db_path) as con:
            row = con.execute(
                "SELECT applied_ts FROM mh_coin_risk_params WHERE mint=?", (str(mint),)
            ).fetchone()
    except sqlite3.Error:
        return None
    return float(row["applied_ts"]) if row else None


def review_coin(db_path, cfg, proposal: dict, *, now: float | None = None) -> dict:
    """Apply one per-coin proposal if and only if the deterministic gate passes."""
    from live_inventory import mode_for_mint, set_coin_risk_params_override

    now = time.time() if now is None else now
    mint = str(proposal.get("mint") or "")
    if not mint:
        return {"mint": None, "applied": False, "reason": "missing_mint"}
    clean, reason = _validate_proposal(proposal)
    case = coin_case(db_path, cfg, mint)
    ticker = case["ticker"]
    if clean is None:
        _log(db_path, mint, ticker, "rejected", reason, {"proposal": proposal}, False)
        return {"mint": mint, "ticker": ticker, "applied": False, "reason": reason}

    last = _last_applied_ts(db_path, mint)
    if last is not None and (now - last) < REVIEW_COOLDOWN_SECONDS:
        reason = f"cooldown_active ({int(REVIEW_COOLDOWN_SECONDS - (now - last))}s remaining)"
        _log(db_path, mint, ticker, "rejected", reason, {"proposal": clean}, False)
        return {"mint": mint, "ticker": ticker, "applied": False, "reason": reason}

    quote_bps = float((cfg or {}).get("paper", {}).get("quote_bps", 40))
    verdict = replay_verdict(db_path, cfg, mint, clean, cost_pct=2 * quote_bps / 10000.0)
    if not verdict["authorized"]:
        _log(db_path, mint, ticker, "rejected", verdict["reason"],
             {"proposal": clean, "verdict": verdict}, False)
        return {"mint": mint, "ticker": ticker, "applied": False,
                "reason": verdict["reason"], "verdict": verdict}

    n = verdict["paths"]
    confidence = "low" if n < 10 else ("medium" if n < 20 else "high")
    params = dict(clean)
    params["mode"] = mode_for_mint(mint, cfg)
    params["ticker"] = ticker
    set_coin_risk_params_override(
        db_path, mint, params, source=f"coin_review@{int(now)}",
        sample_n=n, confidence=confidence)
    _log(db_path, mint, ticker, "applied", verdict["reason"],
         {"proposal": clean, "verdict": verdict, "confidence": confidence}, True)
    return {"mint": mint, "ticker": ticker, "applied": True, "reason": verdict["reason"],
            "confidence": confidence, "applied_params": clean, "verdict": verdict}


def review(db_path, cfg, proposals: list[dict], *, now: float | None = None) -> list[dict]:
    now = time.time() if now is None else now
    return [review_coin(db_path, cfg, p, now=now) for p in proposals if isinstance(p, dict)]


def _load_cfg() -> dict:
    import yaml
    root = Path(__file__).resolve().parent
    return yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8")) or {}


def _default_db() -> str:
    """Container path when present, otherwise the host's authoritative DB.

    The cron prompt invokes this module without --db, so the default must work on
    both the host and inside the image.
    """
    env = os.getenv("MULTIHEDGE_EVIDENCE_DB")
    if env:
        return env
    root = Path(__file__).resolve().parent
    host = root / "deploy" / "data" / "multihedge.db"
    return str(host if host.exists() else root / "multihedge.db")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=_default_db())
    parser.add_argument("--limit", type=int, default=TRADES_PER_COIN)
    parser.add_argument("--collect", action="store_true")
    parser.add_argument("--apply-file")
    args = parser.parse_args(argv)
    db = Path(args.db)
    cfg = _load_cfg()

    if args.apply_file:
        payload = json.loads(Path(args.apply_file).read_text(encoding="utf-8"))
        proposals = payload.get("proposals") if isinstance(payload, dict) else payload
        results = review(db, cfg, proposals or [])
        print(json.dumps({"state": "COIN_REVIEW_COMPLETE",
                          "proposed": len(proposals or []),
                          "applied": sum(1 for r in results if r.get("applied")),
                          "results": results}, sort_keys=True, default=str))
        return 0

    print(json.dumps(collect(db, cfg, args.limit), sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
