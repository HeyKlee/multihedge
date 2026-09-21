#!/usr/bin/env python3
"""Entry-blocker diagnosis: read-only audit of why signal-passed candidates don't open.

This script examines the exact deterministic conditions that block entry after
the signal passes, using the current database state. No writes, pure diagnosis.
"""

import sqlite3
import time
from pathlib import Path

DB_PATH = Path("/home/kelly/multihedge/deploy/data/multihedge.db")
SETUP = "dynamic_scalper"
MIN_COMPOUND_COIN_TRADES = 5
MIN_COMPOUND_COIN_WIN_RATE = 0.60
STOP_LOSS_REENTRY_COOLDOWN_SECONDS = 30 * 60

def _coin_is_quarantined(con: sqlite3.Connection, mint: str) -> bool:
    """Check if mint is quarantined (unprofitable paper ledger)."""
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
        if (not all(v is not None and abs(v) != float('inf') and v == v for v in (qty, entry, pct, usd))
                or qty <= 0 or entry <= 0
                or not abs(qty * entry * pct - usd) < 1e-7):
            return True
        realized.append(usd)
    return sum(realized) < 0

def _in_stop_loss_cooldown(con: sqlite3.Connection, mint: str, now: float) -> bool:
    """Check if mint is in stop-loss re-entry cooldown."""
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
    return not (closed == closed and closed != float('inf')) or now - closed < STOP_LOSS_REENTRY_COOLDOWN_SECONDS

def _coin_has_proven_profit_history(con: sqlite3.Connection, mint: str) -> bool:
    """Check if mint has proven profit history for compounding."""
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
        if (not all(v is not None and abs(v) != float('inf') and v == v for v in (qty, entry, pct, usd))
                or qty <= 0 or entry <= 0
                or abs(qty * entry * pct - usd) > 1e-7):
            return False
        valid.append(usd)
    wins = sum(1 for usd in valid if usd > 0)
    return sum(valid) > 0 and wins / len(valid) >= MIN_COMPOUND_COIN_WIN_RATE

def main():
    now = time.time()
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    
    # Get recent shadow observations with entry_signal=1
    recent_obs = con.execute("""
        SELECT observed_ts, mint, latest_usd, return_5m_pct, return_1h_pct,
               buy_volume_5m_usd, sell_volume_5m_usd, entry_signal, entry_opened
        FROM mh_shadow_entry_observations
        WHERE entry_signal = 1
        ORDER BY observed_ts DESC
        LIMIT 50
    """).fetchall()
    
    # Get currently open positions
    open_mints = {row[0] for row in con.execute(
        "SELECT mint FROM mh_dynamic_scalp_positions"
    ).fetchall()}
    
    # Get recently closed mints (this cycle)
    # We'll check the last 30 seconds
    recently_closed = {row[0] for row in con.execute(
        "SELECT mint FROM mh_shadow_entry_outcomes WHERE close_ts > ?",
        (now - 30,)
    ).fetchall()}
    
    print(f"=== Entry Blocker Diagnosis (as of {time.ctime(now)}) ===\n")
    print(f"Open positions: {len(open_mints)}")
    print(f"Recently closed (<=30s): {len(recently_closed)}")
    print(f"Signal-passed observations (last 50): {len(recent_obs)}")
    print()
    
    blockers = {
        "already_open": 0,
        "recently_closed": 0,
        "quarantined": 0,
        "stop_loss_cooldown": 0,
        "no_signal": 0,
        "opened": 0,
    }
    
    for obs in recent_obs:
        mint = obs["mint"]
        opened = obs["entry_opened"]
        blocked_by = []
        
        if mint in open_mints:
            blocked_by.append("already_open")
        if mint in recently_closed:
            blocked_by.append("recently_closed")
        if _coin_is_quarantined(con, mint):
            blocked_by.append("quarantined")
        if _in_stop_loss_cooldown(con, mint, now):
            blocked_by.append("stop_loss_cooldown")
        
        if opened:
            blockers["opened"] += 1
            print(f"✅ OPENED: {mint[:8]}... (signal passed, no blockers)")
        else:
            if blocked_by:
                for b in blocked_by:
                    blockers[b] += 1
                print(f"🚫 BLOCKED: {mint[:8]}... — {', '.join(blocked_by)}")
            else:
                blockers["no_signal"] += 1
                print(f"❓ BLOCKED: {mint[:8]}... — no signal? (entry_signal=1 but no blockers found)")
    
    print("\n=== Blocker Summary ===")
    for blocker, count in blockers.items():
        if count > 0:
            print(f"  {blocker}: {count}")
    
    # Also check quarantine details
    print("\n=== Quarantine Details ===")
    all_mints_in_trades = con.execute("""
        SELECT DISTINCT coin FROM mh_trades WHERE setup=?
    """, (SETUP,)).fetchall()
    for row in all_mints_in_trades:
        mint = row[0]
        trade_count = con.execute(
            "SELECT COUNT(*) FROM mh_trades WHERE setup=? AND coin=?", 
            (SETUP, mint)
        ).fetchone()[0]
        if trade_count >= MIN_COMPOUND_COIN_TRADES:
            quarantined = _coin_is_quarantined(con, mint)
            proven = _coin_has_proven_profit_history(con, mint)
            net_pnl = con.execute(
                "SELECT SUM(realized_usd) FROM mh_trades WHERE setup=? AND coin=?", 
                (SETUP, mint)
            ).fetchone()[0]
            print(f"  {mint[:8]}...: trades={trade_count}, net_pnl={net_pnl:.4f}, quarantined={quarantined}, proven_profit={proven}")
    
    con.close()

if __name__ == "__main__":
    main()