"""Mint-keyed paper incubator for the dynamic Solana scalp strategy."""

from __future__ import annotations

import sqlite3
from pathlib import Path
import json
import os
import time

SETUP = "dynamic_scalper"
PAPER_NOTIONAL_USD = 1.0
MIN_ENTRY_5M_PCT = 0.5
MAX_ENTRY_5M_PCT = 8.0
MIN_BUY_SELL_RATIO = 1.05

# Memecoin scalp (fast) defaults; per-coin params come from live_inventory.
TAKE_PROFIT_PCT = 0.20
STOP_LOSS_PCT = -0.10
TRAIL_ARM_PCT = 0.02
TRAIL_DISTANCE_PCT = 0.01
MAX_HOLD_SECONDS = 900


def _connect(path: Path):
    con = sqlite3.connect(Path(path), timeout=30)
    con.row_factory = sqlite3.Row
    con.execute(
        "CREATE TABLE IF NOT EXISTS mh_dynamic_scalp_positions ("
        "mint TEXT PRIMARY KEY,ticker TEXT NOT NULL,decimals INTEGER NOT NULL,"
        "entry_usd REAL NOT NULL,qty REAL NOT NULL,opened_ts REAL NOT NULL,"
        "peak_usd REAL NOT NULL,trough_usd REAL NOT NULL)"
    )
    # Backfill column for DBs created before trough tracking existed.
    try:
        con.execute("ALTER TABLE mh_dynamic_scalp_positions ADD COLUMN trough_usd REAL NOT NULL DEFAULT 1e18")
    except sqlite3.OperationalError:
        pass
    con.execute(
        "CREATE TABLE IF NOT EXISTS mh_trades ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,coin TEXT NOT NULL,symbol TEXT,"
        "setup TEXT NOT NULL,side TEXT NOT NULL,open_ts REAL NOT NULL,close_ts REAL NOT NULL,"
        "entry_px REAL NOT NULL,exit_px REAL NOT NULL,qty REAL NOT NULL,"
        "realized_pct REAL NOT NULL,realized_usd REAL NOT NULL,exit_reason TEXT NOT NULL)"
    )
    # Per-trade excursion history (peak/trough/hold) feeds the autonomous
    # parameter autotuner, which can only trust real observed extremes.
    con.execute(
        "CREATE TABLE IF NOT EXISTS mh_scalp_excursions ("
        "mint TEXT NOT NULL,ticker TEXT NOT NULL,mode TEXT NOT NULL,"
        "entry_usd REAL NOT NULL,peak_usd REAL NOT NULL,trough_usd REAL NOT NULL,"
        "open_ts REAL NOT NULL,close_ts REAL NOT NULL,"
        "hold_seconds REAL NOT NULL,realized_pct REAL NOT NULL,exit_reason TEXT NOT NULL)"
    )
    return con


def _exit_reason(position, price: float, now: float, params: dict) -> str | None:
    change = price / float(position["entry_usd"]) - 1
    if change >= params["take_profit_pct"]:
        return "take_profit"
    if change <= params["stop_loss_pct"]:
        return "stop_loss"
    if now - float(position["opened_ts"]) >= params["max_hold_seconds"]:
        return "max_hold"
    peak = max(float(position["peak_usd"]), price)
    if (peak / float(position["entry_usd"]) - 1 >= params["trail_arm_pct"]
            and price / peak - 1 <= -params["trail_distance_pct"]):
        return "trail_stop"
    return None


def _entry_signal(row: dict) -> bool:
    market = row.get("market") or {}
    try:
        change5 = float(market["return_5m_pct"])
        change1h = float(market["return_1h_pct"])
        buy = float(market["buy_volume_5m_usd"])
        sell = float(market["sell_volume_5m_usd"])
    except (KeyError, TypeError, ValueError):
        return False
    return (
        MIN_ENTRY_5M_PCT <= change5 <= MAX_ENTRY_5M_PCT
        and change1h > 0
        and sell > 0
        and buy / sell >= MIN_BUY_SELL_RATIO
    )


def tick(db_path: Path, candidates: list[dict], *, now: float, cfg: dict | None = None) -> dict:
    """Advance paper positions once using one immutable candidate snapshot.

    Exit thresholds are per-coin (via live_inventory.risk_params): memecoins
    scalp fast; backed coins day-trade over hours.
    """
    from live_inventory import risk_params, mode_for_mint
    by_mint = {row.get("mint"): row for row in candidates if isinstance(row, dict)}
    opened = 0
    closed = 0
    reasons: dict[str, int] = {}
    closed_mints = set()
    with _connect(db_path) as con:
        positions = con.execute("SELECT * FROM mh_dynamic_scalp_positions").fetchall()
        for position in positions:
            row = by_mint.get(position["mint"])
            if not row:
                continue
            try:
                price = float(row["market"]["latest_usd"])
            except (KeyError, TypeError, ValueError):
                continue
            if price <= 0:
                continue
            params = risk_params(position["mint"], cfg, db_path=db_path)
            reason = _exit_reason(position, price, now, params)
            peak = max(float(position["peak_usd"]), price)
            entry = float(position["entry_usd"])
            trough = float(position["trough_usd"]) if position["trough_usd"] is not None else entry
            trough = min(trough, price)
            if reason is None:
                con.execute(
                    "UPDATE mh_dynamic_scalp_positions SET peak_usd=?,trough_usd=? WHERE mint=?",
                    (peak, trough, position["mint"]),
                )
                continue
            realized_pct = price / entry - 1
            realized_usd = PAPER_NOTIONAL_USD * realized_pct
            con.execute(
                "INSERT INTO mh_trades(coin,symbol,setup,side,open_ts,close_ts,entry_px,"
                "exit_px,qty,realized_pct,realized_usd,exit_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (position["mint"], position["ticker"], SETUP, "LONG",
                 position["opened_ts"], now, entry, price,
                 position["qty"], realized_pct, realized_usd, reason),
            )
            con.execute(
                "INSERT INTO mh_scalp_excursions(mint,ticker,mode,entry_usd,peak_usd,"
                "trough_usd,open_ts,close_ts,hold_seconds,realized_pct,exit_reason) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (position["mint"], position["ticker"], mode_for_mint(position["mint"], cfg),
                 entry, peak, trough, position["opened_ts"], now,
                 now - position["opened_ts"], realized_pct, reason),
            )
            con.execute("DELETE FROM mh_dynamic_scalp_positions WHERE mint=?", (position["mint"],))
            closed += 1
            closed_mints.add(position["mint"])
            reasons[reason] = reasons.get(reason, 0) + 1

        existing = {
            row[0] for row in con.execute("SELECT mint FROM mh_dynamic_scalp_positions").fetchall()
        }
        for row in candidates:
            mint = row.get("mint")
            if not mint or mint in existing or mint in closed_mints or not _entry_signal(row):
                continue
            try:
                price = float(row["market"]["latest_usd"])
                decimals = int(row["decimals"])
            except (KeyError, TypeError, ValueError):
                continue
            if price <= 0:
                continue
            con.execute(
                "INSERT INTO mh_dynamic_scalp_positions(mint,ticker,decimals,entry_usd,qty,"
                "opened_ts,peak_usd,trough_usd) VALUES(?,?,?,?,?,?,?,?)",
                (mint, str(row.get("ticker") or "UNKNOWN")[:24], decimals, price,
                 PAPER_NOTIONAL_USD / price, now, price, price),
            )
            existing.add(mint)
            opened += 1
    return {"state": "SHADOW_SCALP_COMPLETE", "opened": opened, "closed": closed,
            "reasons": reasons, "candidates": len(candidates)}


if __name__ == "__main__":
    import yaml
    from live_inventory import forced_exit, list_holdings
    from solana_token_universe import discover_candidates, resolve_holdings

    root = Path(__file__).resolve().parent
    cfg = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
    now = time.time()
    candidates = discover_candidates(
        cfg, api_key=os.getenv("JUPITER_API_KEY", ""), now=now
    )
    db_path = Path(os.getenv("MULTIHEDGE_EVIDENCE_DB", str(root / "multihedge.db")))
    holdings = list_holdings(db_path)
    with _connect(db_path) as con:
        paper_mints = [row[0] for row in con.execute(
            "SELECT mint FROM mh_dynamic_scalp_positions"
        ).fetchall()]
    resolved = resolve_holdings(
        [row["mint"] for row in holdings] + paper_mints,
        api_key=os.getenv("JUPITER_API_KEY", ""), now=now
    )
    all_tokens = {row["mint"]: row for row in resolved}
    all_tokens.update({row["mint"]: row for row in candidates})
    exit_decision = forced_exit(
        db_path, {mint: row["market"]["latest_usd"] for mint, row in all_tokens.items()}, now=now,
        cfg=cfg,
    )
    forced_path = Path(os.getenv("MULTIHEDGE_FORCED_EXIT", "/tmp/multihedge_forced_exit.json"))
    forced_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = forced_path.with_name(f".{forced_path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(exit_decision or {}, sort_keys=True), encoding="utf-8")
    temporary.replace(forced_path)
    result = tick(db_path, list(all_tokens.values()), now=now, cfg=cfg)
    result["forced_exit"] = exit_decision and exit_decision["exit_reason"]
    # Autonomous parameter adaptation: propose TP/SL/max-hold changes from real
    # closed-trade excursions, adopt only when evidence supports improvement.
    try:
        from parameter_autotuner import maybe_tune
        result["autotune"] = maybe_tune(db_path, cfg, now=now)
    except Exception as exc:  # never let a tuning failure stop the cycle
        result["autotune"] = {"state": "TUNING_FAILED", "error": type(exc).__name__}
    print(json.dumps(result, sort_keys=True))
