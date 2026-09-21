"""Mint-keyed paper incubator for the dynamic Solana scalp strategy."""

from __future__ import annotations

import sqlite3
import math
from pathlib import Path
import json
import os
import time

import pricefeed  # for RSI/volume computation

TRADE_RECORDS_DIR = Path(__file__).parent / "trade_records"

def _persist_trade_record(mint: str, ticker: str, entry_usd: float, exit_usd: float,
                          qty: float, realized_pct: float, realized_usd: float,
                          exit_reason: str, hold_seconds: float):
    """Save a closed trade record to disk for autotuner analysis."""
    try:
        TRADE_RECORDS_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            "mint": mint,
            "ticker": ticker,
            "entry_usd": entry_usd,
            "exit_usd": exit_usd,
            "qty": qty,
            "realized_pct": realized_pct,
            "realized_usd": realized_usd,
            "exit_reason": exit_reason,
            "hold_seconds": hold_seconds,
            "closed_ts": time.time()
        }
        path = TRADE_RECORDS_DIR / f"{mint}_{int(time.time())}.json"
        path.write_text(json.dumps(data, indent=2))
    except Exception as e:
        print(f"[dynamic_shadow_scalper] persist trade record failed for {mint}: {e}", flush=True)

SETUP = "dynamic_scalper"
PAPER_NOTIONAL_USD = 1.0
MIN_ENTRY_5M_PCT = 1.0
MAX_ENTRY_5M_PCT = 8.0
MIN_BUY_SELL_RATIO = 1.05
INITIAL_EQUITY_USD = 10.0  # wallet seeded with $10, compounds from there
POSITION_FRACTION = 0.15   # use 15% of available wallet per proven coin
MIN_COMPOUND_COIN_TRADES = 5
MIN_COMPOUND_COIN_WIN_RATE = 0.60
STOP_LOSS_REENTRY_COOLDOWN_SECONDS = 30 * 60

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
    con.execute(
        "CREATE TABLE IF NOT EXISTS mh_scalp_price_samples ("
        "mint TEXT NOT NULL,opened_ts REAL NOT NULL,sample_ts REAL NOT NULL,"
        "price_usd REAL NOT NULL,PRIMARY KEY(mint,opened_ts,sample_ts))"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS mh_scalp_policy_evidence ("
        "mint TEXT NOT NULL,opened_ts REAL NOT NULL,policy_json TEXT NOT NULL,"
        "mixed INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(mint,opened_ts))"
    )
    return con


def _exit_reason(position, price: float, now: float, params: dict) -> str | None:
    entry = float(position["entry_usd"])
    peak = max(float(position["peak_usd"]), price)
    change = price / entry - 1
    if change >= params["take_profit_pct"]:
        return "take_profit"
    if change <= params["stop_loss_pct"]:
        return "stop_loss"
    if (now - float(position["opened_ts"]) >= params["max_hold_seconds"]
            and peak / entry - 1 >= params["take_profit_pct"]):
        return "max_hold"
    if (peak / entry - 1 >= params["trail_arm_pct"]
            and price / peak - 1 <= -params["trail_distance_pct"]):
        return "trail_stop"
    return None


def _entry_signal(row: dict, now: float) -> bool:
    mint = row.get("mint")
    if not mint:
        return False
    market = row.get("market") or {}
    try:
        change5 = float(market.get("return_5m_pct", 0))
        change1h = float(market.get("return_1h_pct", 0))
        buy = float(market.get("buy_volume_5m_usd", 0))
        sell = float(market.get("sell_volume_5m_usd", 0))
        # Compute RSI(14) and 20-period volume average from pricefeed
        rsi_15m = pricefeed.compute_rsi_14(mint, now)
        vol_avg_20 = pricefeed.compute_volume_avg_20(mint)
        # If not enough history, fall back to market-provided placeholders (or neutral)
        if rsi_15m is None:
            rsi_15m = float(market.get("rsi_15m", 50))
        if vol_avg_20 is None:
            vol_5m = float(market.get("volume_5m_usd", buy + sell))
            vol_avg_20 = vol_5m  # fallback: use current volume as average
        vol_5m = float(market.get("volume_5m_usd", buy + sell))
    except (KeyError, TypeError, ValueError):
        return False
    if not all(math.isfinite(v) for v in (change5, change1h, buy, sell, rsi_15m, vol_5m, vol_avg_20)):
        return False
    if sell <= 0:
        return False
    # Reject overbought: RSI(15m) >= 80 means don't chase (start conservative as per user request)
    if rsi_15m >= 80:
        return False
    # Volume surge: require at least 1.2× the 20-period average (loosened per user test run)
    if vol_5m < 1.2 * vol_avg_20:
        return False
    # Bullish momentum condition (existing)
    bullish_momentum = (
        MIN_ENTRY_5M_PCT <= change5 <= MAX_ENTRY_5M_PCT
        and change1h > 0
        and buy / sell >= MIN_BUY_SELL_RATIO
    )
    # Reversal evidence condition: sharp 5m rebound from a downtrend
    reversal_evidence = (
        change5 >= 1.0  # at least 1% 5m return
        and change1h < 0  # 1h negative indicates prior downtrend
        and buy / sell >= 2.0  # very strong buying pressure
    )
    return bullish_momentum or reversal_evidence


def _coin_has_proven_profit_history(con: sqlite3.Connection, mint: str) -> bool:
    """Allow compounding only after this exact mint has proven profitable.

    The proof is deliberately conservative: at least five valid closed paper
    trades for this mint, net positive realized P&L, and >=60% winners. Invalid
    historical ledger rows fail closed to the original $1 entry size.
    """
    rows = con.execute(
        "SELECT qty,entry_px,realized_pct,realized_usd FROM mh_trades "
        "WHERE setup=? AND coin=? ORDER BY close_ts ASC",
        (SETUP, str(mint)),
    ).fetchall()
    if len(rows) < MIN_COMPOUND_COIN_TRADES:
        return False
    valid = []
    for row in rows:
        try:
            qty, entry, pct, usd = (float(row["qty"]), float(row["entry_px"]),
                                    float(row["realized_pct"]), float(row["realized_usd"]))
        except (KeyError, TypeError, ValueError):
            return False
        if (not all(math.isfinite(v) for v in (qty, entry, pct, usd))
                or qty <= 0 or entry <= 0
                or abs(qty * entry * pct - usd) > 1e-7):
            return False
        valid.append(usd)
    wins = sum(1 for usd in valid if usd > 0)
    return sum(valid) > 0 and wins / len(valid) >= MIN_COMPOUND_COIN_WIN_RATE


def _target_notional(con: sqlite3.Connection, mint: str, wallet_free: float) -> float:
    if wallet_free < PAPER_NOTIONAL_USD:
        return 0.0
    if _coin_has_proven_profit_history(con, mint):
        return min(wallet_free, wallet_free * POSITION_FRACTION)
    return PAPER_NOTIONAL_USD


def _coin_is_quarantined(con: sqlite3.Connection, mint: str) -> bool:
    """Fail closed on an established mint whose paper ledger is unprofitable.

    A fresh coin remains eligible for its $1 probationary entry. Once a mint has
    five closes, malformed accounting or a negative cumulative realised P&L
    blocks further shadow entries. This never affects live inventory or exits.
    """
    rows = con.execute(
        "SELECT qty,entry_px,realized_pct,realized_usd FROM mh_trades "
        "WHERE setup=? AND coin=? ORDER BY close_ts ASC",
        (SETUP, str(mint)),
    ).fetchall()
    if len(rows) < MIN_COMPOUND_COIN_TRADES:
        return False
    realized = []
    for row in rows:
        try:
            qty, entry, pct, usd = (float(row["qty"]), float(row["entry_px"]),
                                    float(row["realized_pct"]), float(row["realized_usd"]))
        except (KeyError, TypeError, ValueError):
            return True
        if (not all(math.isfinite(value) for value in (qty, entry, pct, usd))
                or qty <= 0 or entry <= 0
                or not math.isclose(qty * entry * pct, usd, rel_tol=1e-9, abs_tol=1e-7)):
            return True
        realized.append(usd)
    return sum(realized) < 0


def _in_stop_loss_cooldown(con: sqlite3.Connection, mint: str, now: float) -> bool:
    """Block immediate same-mint re-entry after a realised stop loss."""
    row = con.execute(
        "SELECT MAX(close_ts) FROM mh_trades "
        "WHERE setup=? AND coin=? AND exit_reason='stop_loss'",
        (SETUP, str(mint)),
    ).fetchone()
    if row is None or row[0] is None:
        return False
    try:
        closed = float(row[0])
    except (TypeError, ValueError):
        return True
    return not math.isfinite(closed) or now - closed < STOP_LOSS_REENTRY_COOLDOWN_SECONDS


def tick(db_path: Path, candidates: list[dict], *, now: float, cfg: dict | None = None) -> dict:
    """Advance paper positions once using one immutable candidate snapshot.

    Exit thresholds are per-coin (via live_inventory.risk_params): memecoins
    scalp fast; backed coins day-trade over hours.
    """
    from live_inventory import risk_params, mode_for_mint
    from parameter_autotuner import policy_signature
    by_mint = {row.get("mint"): row for row in candidates if isinstance(row, dict)}
    opened = 0
    closed = 0
    reasons: dict[str, int] = {}
    closed_mints = set()
    # Resolve policy before acquiring the writer lock: risk_params opens its
    # own connection and may initialise override tables on a fresh database.
    with _connect(db_path) as policy_con:
        observed_mints = {r[0] for r in policy_con.execute('SELECT mint FROM mh_dynamic_scalp_positions')}
    observed_mints.update(m for m in by_mint if m)
    policies = {mint: risk_params(mint, cfg, db_path=db_path, allow_tuned=True)
                for mint in observed_mints}
    with _connect(db_path) as con:
        # Keep wallet, closes and allocations in one transaction on the explicit DB.
        con.execute("BEGIN IMMEDIATE")
        con.execute("CREATE TABLE IF NOT EXISTS mh_accounts (trader TEXT PRIMARY KEY, "
                    "equity_usd REAL NOT NULL, started_usd REAL NOT NULL)")
        account = con.execute("SELECT equity_usd FROM mh_accounts WHERE trader=?", (SETUP,)).fetchone()
        if account is None:
            history = con.execute("SELECT qty,entry_px,realized_pct,realized_usd FROM mh_trades WHERE setup=?", (SETUP,)).fetchall()
            for trade in history:
                values = [float(v) for v in trade]
                if (not all(math.isfinite(v) for v in values)
                        or abs(values[0] * values[1] * values[2] - values[3]) > 1e-7):
                    raise ValueError("invalid incubator history; wallet reconciliation blocked")
            equity = INITIAL_EQUITY_USD + sum(float(t["realized_usd"]) for t in history)
            con.execute("INSERT INTO mh_accounts(trader,equity_usd,started_usd) VALUES(?,?,?)",
                        (SETUP, equity, INITIAL_EQUITY_USD))
        positions = con.execute("SELECT * FROM mh_dynamic_scalp_positions").fetchall()
        for position in positions:
            row = by_mint.get(position["mint"])
            if not row:
                continue
            try:
                price = float(row["market"]["latest_usd"])
            except (KeyError, TypeError, ValueError):
                continue
            if not math.isfinite(price) or price <= 0:
                continue
            params = policies[position["mint"]]
            # Never label legacy positions retrospectively. A policy change
            # during observation makes the whole path ineligible for adoption.
            con.execute("UPDATE mh_scalp_policy_evidence SET mixed=1 "
                        "WHERE mint=? AND opened_ts=? AND policy_json<>?",
                        (position["mint"], position["opened_ts"], policy_signature(params)))
            con.execute(
                "INSERT OR IGNORE INTO mh_scalp_price_samples(mint,opened_ts,sample_ts,price_usd) "
                "VALUES(?,?,?,?)",
                (position["mint"], position["opened_ts"], now, price),
            )
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
            # Compute actual realized P&L from quantity, not from fixed notional
            entry_qty = float(position["qty"])
            realized_usd = entry_qty * entry * realized_pct
            # Credit realized P&L to the dynamic_scalper wallet for compounding
            con.execute(
                "UPDATE mh_accounts SET equity_usd=equity_usd+? WHERE trader=?",
                (realized_usd, SETUP),
            )
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
            # Persist trade record to disk
            _persist_trade_record(
                mint=position["mint"],
                ticker=position["ticker"],
                entry_usd=entry,
                exit_usd=price,
                qty=position["qty"],
                realized_pct=realized_pct,
                realized_usd=realized_usd,
                exit_reason=reason,
                hold_seconds=now - position["opened_ts"]
            )

        existing = {
            row[0] for row in con.execute("SELECT mint FROM mh_dynamic_scalp_positions").fetchall()
        }
        equity = float(con.execute("SELECT equity_usd FROM mh_accounts WHERE trader=?", (SETUP,)).fetchone()[0])
        committed = float(con.execute("SELECT COALESCE(SUM(qty*entry_usd),0) FROM mh_dynamic_scalp_positions").fetchone()[0])
        if not math.isfinite(equity) or not math.isfinite(committed):
            raise ValueError("invalid incubator wallet balance")
        wallet_free = max(0.0, equity - committed)
        for row in candidates:
            mint = row.get("mint")
            if (not mint or mint in existing or mint in closed_mints
                    or not _entry_signal(row, now) or _coin_is_quarantined(con, mint)
                    or _in_stop_loss_cooldown(con, mint, now)):
                continue
            try:
                price = float(row["market"]["latest_usd"])
                decimals = int(row["decimals"])
            except (KeyError, TypeError, ValueError):
                continue
            if not math.isfinite(price) or price <= 0:
                continue
            target_notional = _target_notional(con, mint, wallet_free)
            if target_notional < 0.25:
                continue
            raw_qty = target_notional / price
            if raw_qty * price < 0.25:  # skip if position would be under $0.25
                continue
            con.execute(
                "INSERT INTO mh_dynamic_scalp_positions(mint,ticker,decimals,entry_usd,qty,"
                "opened_ts,peak_usd,trough_usd) VALUES(?,?,?,?,?,?,?,?)",
                (mint, str(row.get("ticker") or "UNKNOWN")[:24], decimals, price,
                 raw_qty, now, price, price),
            )
            con.execute(
                "INSERT INTO mh_scalp_price_samples(mint,opened_ts,sample_ts,price_usd) "
                "VALUES(?,?,?,?)", (mint, now, now, price),
            )
            params = policies[mint]
            con.execute("INSERT INTO mh_scalp_policy_evidence(mint,opened_ts,policy_json,mixed) VALUES(?,?,?,0)",
                        (mint, now, policy_signature(params)))
            wallet_free -= raw_qty * price
            existing.add(mint)
            opened += 1
            # Mark this observation as having opened a position
            con.execute(
                "UPDATE mh_shadow_entry_observations SET entry_opened=1 "
                "WHERE mint=? AND observed_ts=? AND entry_signal=1",
                (mint, now)
            )
    return {"state": "SHADOW_SCALP_COMPLETE", "opened": opened, "closed": closed,
            "reasons": reasons, "candidates": len(candidates)}


def run_cycle(cfg: dict, db_path: Path, *, now: float, api_key: str, get=None,
              forced_path: Path | None = None) -> tuple[dict, int]:
    """Run one shadow scalp cycle and return (result, exit_code).

    An upstream metadata outage degrades honestly instead of crashing: no new
    risk is opened, exit evaluation is reported as unevaluated, and any pending
    forced exit is left untouched so a real decision can still reach the signer.
    """
    import httpx
    from live_inventory import forced_exit, list_holdings
    from solana_token_universe import (
        TokenDenied, UpstreamUnavailable, discover_candidates, resolve_holdings,
    )

    get = httpx.get if get is None else get
    try:
        candidates = discover_candidates(cfg, api_key=api_key, now=now, get=get)
        holdings = list_holdings(db_path)
        with _connect(db_path) as con:
            paper_mints = [row[0] for row in con.execute(
                "SELECT mint FROM mh_dynamic_scalp_positions"
            ).fetchall()]
        resolved = resolve_holdings(
            [row["mint"] for row in holdings] + paper_mints, api_key=api_key, now=now, get=get
        )
    except (UpstreamUnavailable, TokenDenied) as exc:
        # Denied covers UpstreamUnavailable (it is a TokenDenied subclass) plus
        # the missing/invalid-Jupiter-key raise in solana_token_universe._headers.
        # Aborting here returns exit code 1, and the ops launcher stops the whole
        # cycle on that, skipping the agent and signer steps so a genuine forced
        # exit would go unsettled. Degrade instead: no new risk is opened, exits
        # are reported unevaluated, and the signer still gets its turn.
        return {"state": "SHADOW_SCALP_DEGRADED", "reason": str(exc),
                "new_risk_blocked": True, "exits_unevaluated": True,
                "opened": 0, "closed": 0, "candidates": 0}, 0
    all_tokens = {row["mint"]: row for row in resolved}
    all_tokens.update({row["mint"]: row for row in candidates})
    resolved_by_mint = {row["mint"]: row for row in resolved}
    # Log observations for evidence gate
    with _connect(db_path) as con:
        for row in candidates:
            mint = row.get("mint")
            if not mint:
                continue
            market = row.get("market") or {}
            try:
                entry_sig = _entry_signal(row, now)
            except Exception:
                entry_sig = False
            # Update price history for RSI/volume calculations using market data
            price = float(market.get("latest_usd", 0.0))
            buy_vol = float(market.get("buy_volume_5m_usd", 0.0))
            sell_vol = float(market.get("sell_volume_5m_usd", 0.0))
            pricefeed._update_price_history(mint, now, price, buy_vol, sell_vol)
            # Also update price history for resolved holdings (open positions)
            resolved_mint = resolved_by_mint.get(mint)
            if resolved_mint:
                resolved_price = float(resolved_mint.get("market", {}).get("latest_usd", 0.0))
                if resolved_price > 0:
                    pricefeed._update_price_history(mint, now, resolved_price, 0, 0)
            # Compute RSI and 20-period volume average from pricefeed
            rsi_15m = pricefeed.compute_rsi_14(mint, now)
            vol_avg_20 = pricefeed.compute_volume_avg_20(mint)
            if rsi_15m is None:
                rsi_15m = float(market.get("rsi_15m", 50.0))
            if vol_avg_20 is None:
                vol_5m = float(market.get("volume_5m_usd", buy_vol + sell_vol))
                vol_avg_20 = vol_5m  # fallback: use current volume as average
            con.execute(
                "INSERT OR IGNORE INTO mh_shadow_entry_observations(observed_ts,mint,latest_usd,return_5m_pct,return_1h_pct,buy_volume_5m_usd,sell_volume_5m_usd,entry_signal,entry_opened,rsi_15m,volume_5m_avg_20) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    now,
                    mint,
                    float(market.get("latest_usd", 0)),
                    float(market.get("return_5m_pct", 0)),
                    float(market.get("return_1h_pct", 0)),
                    float(market.get("buy_volume_5m_usd", 0)),
                    float(market.get("sell_volume_5m_usd", 0)),
                    1 if entry_sig else 0,
                    0,  # entry_opened determined later by tick
                    rsi_15m,
                    vol_avg_20,
                ),
            )
    # Update price history for open positions that are not candidates
    for row in resolved:
        mint = row.get("mint")
        if not mint or mint in {r["mint"] for r in candidates if r.get("mint")}:
            continue
        market = row.get("market") or {}
        price = float(market.get("latest_usd", 0.0))
        if price > 0:
            pricefeed._update_price_history(mint, now, price, 0, 0)
    exit_decision = forced_exit(
        db_path, {mint: row["market"]["latest_usd"] for mint, row in all_tokens.items()}, now=now,
        cfg=cfg,
    )
    forced_path = (Path(os.getenv("MULTIHEDGE_FORCED_EXIT", "/tmp/multihedge_forced_exit.json"))
                   if forced_path is None else Path(forced_path))
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
    return result, 0


if __name__ == "__main__":
    import yaml

    root = Path(__file__).resolve().parent
    result, code = run_cycle(
        yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8")),
        Path(os.getenv("MULTIHEDGE_EVIDENCE_DB", str(root / "multihedge.db"))),
        now=time.time(),
        api_key=os.getenv("JUPITER_API_KEY", ""),
    )
    print(json.dumps(result, sort_keys=True))
    raise SystemExit(code)
