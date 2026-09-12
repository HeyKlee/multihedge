"""
MultiHedge REASONER TRADER (Layer 2, SLOW swing) — multi-coin.

Generalises AutoHedge's reasoner_trader to EVERY coin. Each coin gets its own
reasoner account + one open reasoner position at a time (LONG or SHORT) with a
LONG horizon (TP +2.5% / SL -1.5% / MAX_HOLD 2h), trading on the per-coin
`mh_news_bias` direction written by mh_news.py. It NEVER calls the LLM itself;
it reads the bias the news reasoner produced, then sizes from that coin's own
reasoner equity.

State per coin: mh_reasoner_positions (open). Equity is the SINGLE shared USDC
treasury (same account.py pool the fast engine uses). Closed trades go to
mh_trades with source='reasoner-<coin>'.
"""

import os
import json
import sqlite3
import sys
import time
from pathlib import Path

import pricefeed
import paper
from config import COINS, CFG_PATH

CUR_DIR = Path(__file__).parent
DB_PATH = CUR_DIR / "multihedge.db"

# --- tunable reasoner params (may be overridden by config.yaml / DB optimizer) ---
DEFAULT_PARAMS = {
    "POSITION_FRACTION": 0.50,
    "TAKE_PROFIT": 0.025,
    "STOP_LOSS": 0.015,
    "MAX_HOLD_SECS": 2 * 3600,
    "TRAIL_ARM": 0.01,
    "TRAIL_DIST": 0.005,
    "CONFIDENCE_MIN": 0.55,
}

PARAMS_SCHEMA = """CREATE TABLE IF NOT EXISTS mh_reasoner_params (
    key TEXT PRIMARY KEY, value REAL)"""

def _load_params():
    """Read tunable params: DB table wins, then config.yaml 'reasoner:' block, then defaults."""
    p = dict(DEFAULT_PARAMS)
    try:
        import yaml
        cfg = yaml.safe_load(CFG_PATH.read_text(encoding="utf-8")) or {}
        r = cfg.get("reasoner", {})
        if isinstance(r, dict):
            for k in DEFAULT_PARAMS:
                if k in r:
                    p[k] = r[k]
    except Exception:
        pass
    # DB overrides (monthly optimizer writes here; survives image rebuilds)
    try:
        con = sqlite3.connect(DB_PATH, check_same_thread=False)
        con.execute(PARAMS_SCHEMA)
        for row in con.execute("SELECT key, value FROM mh_reasoner_params"):
            if row[0] in p:
                p[row[0]] = row[1]
        con.close()
    except Exception:
        pass
    return p

def refresh_params():
    """Re-read params (called each tick so a monthly optimization applies live)."""
    global _PARAMS, POSITION_FRACTION, TAKE_PROFIT, STOP_LOSS, MAX_HOLD_SECS
    global TRAIL_ARM, TRAIL_DIST, CONFIDENCE_MIN
    _PARAMS = _load_params()
    POSITION_FRACTION = P("POSITION_FRACTION")
    TAKE_PROFIT = P("TAKE_PROFIT")
    STOP_LOSS = P("STOP_LOSS")
    MAX_HOLD_SECS = P("MAX_HOLD_SECS")
    TRAIL_ARM = P("TRAIL_ARM")
    TRAIL_DIST = P("TRAIL_DIST")
    CONFIDENCE_MIN = P("CONFIDENCE_MIN")
    con = sqlite3.connect(DB_PATH, check_same_thread=False,
                          timeout=BUSY_TIMEOUT_SECONDS)
    try:
        con.execute("CREATE TABLE IF NOT EXISTS mh_parameter_application (trader TEXT PRIMARY KEY,settings_json TEXT,applied_ts REAL,checked_ts REAL)")
        settings = json.dumps(_PARAMS,sort_keys=True,allow_nan=False)
        con.execute("INSERT INTO mh_parameter_application VALUES(?,?,?,?) ON CONFLICT(trader) DO UPDATE SET applied_ts=CASE WHEN settings_json != excluded.settings_json THEN excluded.applied_ts ELSE applied_ts END, settings_json=excluded.settings_json,checked_ts=excluded.checked_ts", ("reasoner",settings,time.time(),time.time()))
        con.commit()
    finally:
        con.close()

_PARAMS = _load_params()

def P(key):
    return _PARAMS.get(key, DEFAULT_PARAMS.get(key))

# module-level constants mirroring config (kept for unchanged call sites)
POSITION_FRACTION = P("POSITION_FRACTION")
TAKE_PROFIT = P("TAKE_PROFIT")
STOP_LOSS = P("STOP_LOSS")
MAX_HOLD_SECS = P("MAX_HOLD_SECS")
TRAIL_ARM = P("TRAIL_ARM")
TRAIL_DIST = P("TRAIL_DIST")
CONFIDENCE_MIN = P("CONFIDENCE_MIN")


BUSY_TIMEOUT_SECONDS = 10.0


def _connect():
    """Open a connection with a BOUNDED wait for the write lock.

    This module opens several connections per tick. The CREATE TABLE statements
    below are not the problem (IF NOT EXISTS on an existing table performs no
    write), but the WAIT is: a writer that cannot get the lock holds SQLite's
    PENDING byte, which blocks new readers, and then waits for existing readers to
    clear before taking EXCLUSIVE. On 2026-09-12 this loop was found holding
    PENDING in /proc/locks while the busy handler spun, and nothing in the stack
    could write for minutes. A bounded wait makes that a single failed operation
    that retries next tick instead of a system-wide stall.
    """
    con = sqlite3.connect(DB_PATH, check_same_thread=False,
                          timeout=BUSY_TIMEOUT_SECONDS)
    con.row_factory = sqlite3.Row
    con.execute("""
        CREATE TABLE IF NOT EXISTS mh_reasoner_accounts (
            symbol TEXT PRIMARY KEY, equity REAL NOT NULL, started REAL NOT NULL)
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS mh_reasoner_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL, side TEXT NOT NULL, qty REAL NOT NULL,
            entry REAL NOT NULL, ts REAL NOT NULL, entry_signal REAL, peak REAL)
    """)
    # mh_news_bias is created by mh_news.py; create here too so the reasoner
    # is robust even if started before the news loop's first run.
    con.execute("""
        CREATE TABLE IF NOT EXISTS mh_news_bias (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT, direction TEXT, confidence REAL,
            rationale TEXT, headlines TEXT, provider TEXT, ts REAL)
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS mh_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            coin TEXT NOT NULL, symbol TEXT, setup TEXT, side TEXT NOT NULL,
            open_ts REAL, close_ts REAL, entry_px REAL, exit_px REAL,
            qty REAL, realized_pct REAL, realized_usd REAL, exit_reason TEXT)
    """)
    con.commit()
    return con


def ensure_account(symbol):
    """Reasoner trades from ITS OWN independent paper wallet (one per trader)."""
    paper.ensure_account(paper.TRADER_REASONER, paper.DEFAULT_EQUITY)


def _equity(symbol):
    return paper.equity(paper.TRADER_REASONER)


def _set_equity(symbol, v):
    paper.set_account(paper.TRADER_REASONER, v)


def _credit_reasoner(symbol, realized):
    """Credit realized P&L to the REASONER's wallet WITHOUT moving the start
    baseline (started_usd stays at seed so '% vs start' growth is accurate)."""
    con = _connect()
    con.execute("UPDATE mh_accounts SET equity_usd=equity_usd+? WHERE trader=?",
                (realized, paper.TRADER_REASONER))
    con.commit()
    con.close()


def _rpos(symbol):
    con = _connect()
    row = con.execute("SELECT * FROM mh_reasoner_positions WHERE symbol=? "
                      "ORDER BY id DESC LIMIT 1", (symbol,)).fetchone()
    con.close()
    return dict(row) if row else None


def _latest_bias(symbol):
    """Fresh bias from OUR mh_news_bias table (read-only). FLAT if none/stale."""
    con = _connect()
    row = con.execute("SELECT * FROM mh_news_bias WHERE symbol=? AND COALESCE(provider,'') NOT IN ('whale_tracker','memecoin_tracker') ORDER BY id DESC LIMIT 1",
                      (symbol,)).fetchone()
    con.close()
    if not row:
        return {"direction": "FLAT", "confidence": 0.0}
    if time.time() - row["ts"] > 900:
        return {"direction": "FLAT", "confidence": 0.0}
    return {"direction": row["direction"], "confidence": row["confidence"]}


# --- Tier-1 price-concurrence gate (Kelly / Council fix) ---
# The reasoner used to open purely on LLM-news-bias direction, which read
# near-uniform bullish and kept opening LONGs into a chopping/down tape.
# This gate requires price to actually CONFIRM the bias direction over a
# short window before committing, and lets a DOWN bias fire a SHORT (it was
# effectively bull-silenced before). Zero hand-tuned params: it just checks
# whether the coin's recent pxhist momentum agrees with the bias.
CONCUR_WINDOW_SECS = 1800          # 30 min of recent pxhist to judge against
CONCUR_ALLOW_SHORT = False         # Kelly: NO shorts allowed. DOWN bias => hold USDC (stay flat)

def _momentum_ok(symbol, px, side):
    """Require price to confirm the bias side over recent pxhist (0-param).

    LONG: current px must be >= the window's mean (not fading below the
          recent midpoint). SHORT: px must be <= the window mean. If pxhist
          is unavailable for the coin, fall back to True (allow entry) so the
          gate never silently starves the trader.
    Returns (ok:bool, momentum:str).
    """
    try:
        con = sqlite3.connect(DB_PATH, check_same_thread=False)
        rows = con.execute(
            "SELECT px FROM mh_pxhist WHERE coin=? AND ts >= ? ORDER BY ts DESC",
            (symbol, time.time() - CONCUR_WINDOW_SECS)).fetchall()
        con.close()
    except Exception:
        return False, "nopxhist"
    pxs = [r[0] for r in rows]
    if not pxs:
        return False, "nopxhist"
    mean = sum(pxs) / len(pxs)
    if side == "LONG":
        ok = px >= mean
    else:
        ok = px <= mean
    return ok, ("up" if px > mean else ("down" if px < mean else "flat"))


def closing_reason(px, pos):
    side = pos["side"]
    pct = (px - pos["entry"]) / pos["entry"] if side == "LONG" else \
          (pos["entry"] - px) / pos["entry"]
    if pct >= TAKE_PROFIT:
        return "take_profit"
    if pct <= -STOP_LOSS:
        return "stop_loss"
    if time.time() - pos["ts"] >= MAX_HOLD_SECS:
        return "max_hold"
    # trailing stop
    peak = pos.get("peak") or pos["entry"]
    peak_pct = (peak - pos["entry"]) / pos["entry"] if side == "LONG" else (pos["entry"] - peak) / pos["entry"]
    if peak_pct >= TRAIL_ARM:
        if side == "LONG" and (peak - px) / peak >= TRAIL_DIST:
            return "trail_stop"
        if side == "SHORT" and (px - peak) / peak >= TRAIL_DIST:
            return "trail_stop"
    return None


def _close(symbol, pos, px, reason):
    con = _connect()
    con.execute("BEGIN IMMEDIATE")
    current = con.execute("SELECT * FROM mh_reasoner_positions WHERE id=?", (pos["id"],)).fetchone()
    if current is None:
        con.close()
        return None
    pos = dict(current)
    side = pos["side"]
    if side == "LONG":
        realized = pos["qty"] * (px - pos["entry"])
    else:
        realized = pos["qty"] * (pos["entry"] - px)
    # P&L returns to THIS COIN's own wallet (releases its committed capital)
    con.execute("UPDATE mh_accounts SET equity_usd=equity_usd+? WHERE trader=?",
                (realized, paper.TRADER_REASONER))
    pct = (px - pos["entry"]) / pos["entry"] if side == "LONG" else \
          (pos["entry"] - px) / pos["entry"]
    # record into the shared mh_trades (source=reasoner-<coin>)
    con.execute(
        "INSERT INTO mh_trades(coin,symbol,setup,side,open_ts,close_ts,entry_px,exit_px,"
        "qty,realized_pct,realized_usd,exit_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (symbol, symbol, "reasoner", side, pos["ts"], time.time(), pos["entry"], px,
         pos["qty"], pct, realized, reason))
    con.execute("DELETE FROM mh_reasoner_positions WHERE id=?", (pos["id"],))
    con.commit()
    con.close()
    return {"pct": pct, "reason": reason, "usd": realized}


def _open(symbol, side, px):
    if paper.is_paused(paper.TRADER_REASONER):
        return False
    # size from the REASONER's own available wallet capital (not a shared pool)
    free = paper.available(paper.TRADER_REASONER)
    budget = free * POSITION_FRACTION
    if px <= 0 or not 0 < POSITION_FRACTION <= 1 or budget < 1.0 or side != "LONG":
        return False
    qty = budget / px
    con = _connect()
    con.execute(
        "INSERT INTO mh_reasoner_positions(symbol,side,qty,entry,ts,entry_signal,peak) "
        "VALUES(?,?,?,?,?,?,?)",
        (symbol, side, qty, px, time.time(), side, px))
    con.commit()
    con.close()
    return True


def tick_symbols(symbols=None):
    """One reasoner pass across coins: close expiring, then open if bullish bias."""
    symbols = symbols or list(COINS.keys())
    out = []
    refresh_params()   # pick up any monthly optimization written to config/DB
    for symbol in symbols:
        ensure_account(symbol)
        paper.kill_switch_check(paper.TRADER_REASONER)
        px = pricefeed.live_price(COINS[symbol]["mint"], symbol)
        if not px or px <= 0:
            out.append({"coin": symbol, "action": "no_price"})
            continue
        pos = _rpos(symbol)
        if pos:
            # persist peak
            side = pos["side"]
            peak = pos.get("peak") or pos["entry"]
            if (side == "LONG" and px > peak) or (side == "SHORT" and px < peak):
                peak = px
            con = _connect()
            con.execute("UPDATE mh_reasoner_positions SET peak=? WHERE id=?",
                        (peak, pos["id"]))
            con.commit()
            con.close()
            pos["peak"] = peak
            reason = closing_reason(px, pos)
            if reason:
                r = _close(symbol, pos, px, reason)
                out.append({"coin": symbol, "action": "close", "side": pos["side"],
                            "reason": reason, "pct": round(r["pct"], 4)})
            continue
        # no open pos: decide from current bias (read from OUR db path so this
        # module and mh_news agree on the same store)
        bias = _latest_bias(symbol)
        direction = bias.get("direction", "FLAT")
        confidence = float(bias.get("confidence", 0.0))
        if direction in ("UP", "DOWN") and confidence >= CONFIDENCE_MIN:
            side = "LONG" if direction == "UP" else "SHORT"
            # Tier-1 price-concurrence gate: only commit if recent pxhist
            # momentum confirms the bias; DOWN is allowed to SHORT now.
            ok, momentum = _momentum_ok(symbol, px, side)
            if not CONCUR_ALLOW_SHORT and side == "SHORT":
                ok = False
                momentum = "short-disabled"
            if ok and _open(symbol, side, px):
                out.append({"coin": symbol, "action": "open", "side": side,
                            "px": px, "conf": confidence, "momentum": momentum})
            else:
                out.append({"coin": symbol, "action": "gated",
                            "bias": direction, "conf": confidence,
                            "momentum": momentum})
        else:
            out.append({"coin": symbol, "action": "flat",
                        "bias": direction, "conf": confidence})
    return out


def reasoner_meta(symbol=None):
    con = _connect()
    if symbol:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM mh_reasoner_positions WHERE symbol=?", (symbol,)).fetchall()]
        eq = paper.equity(paper.TRADER_REASONER)
        con.close()
        return {"symbol": symbol, "equity": eq, "positions": rows}
    poss = [dict(r) for r in con.execute("SELECT * FROM mh_reasoner_positions").fetchall()]
    con.close()
    return {"accounts": [{"trader": paper.TRADER_REASONER,
                          "equity": paper.equity(paper.TRADER_REASONER),
                          "started": paper.DEFAULT_EQUITY}],
            "positions": poss}


if __name__ == "__main__":
    import json
    mode = sys.argv[1] if len(sys.argv) > 1 else "tick"
    if mode == "tick":
        print(json.dumps(tick_symbols(), indent=2, default=str))
    elif mode == "meta":
        print(json.dumps(reasoner_meta(), indent=2, default=str))
    else:
        print("usage: mh_reasoner.py {tick|meta}")