"""
Self-tests for grid_trader.py. Uses an isolated temp DB so multihedge.db and
the paper wallets are never touched. Run: python test_grid_trader.py
"""

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

TMP_DB = Path(tempfile.mkdtemp()) / "grid_test.db"
# patch DB_PATH BEFORE importing grid_trader so all tables land in temp db
import grid_trader as gt  # noqa: E402
gt.DB_PATH = TMP_DB

CFG = {
    "paper": {"quote_bps": 0},   # zero fees so we can report pre-fee profit cleanly
    "grid": {
        "enabled": True,
        "starting_cash_nzd": 40,
        "usd_per_nzd": 0.60,
        "grid_levels": 8,
        "range_pct": 0.04,
        "min_trade_value_usd": 1.0,
        "max_drawdown_kill": 0.20,
        "flash_crash_drop": 0.94,
    },
}


def run_sine(n=200):
    import math
    t = 0.0
    for i in range(n):
        # sine oscillating between ~100 and ~110, period 40 ticks
        px = 105 + 5 * math.sin(2 * math.pi * i / 40)
        gt.grid_tick(px, CFG)
        t += 1
    return t


def test_cycles_profit():
    n = run_sine()
    con = sqlite3.connect(TMP_DB)
    con.row_factory = sqlite3.Row
    sells = [dict(r) for r in con.execute(
        "SELECT * FROM grid_trades WHERE side='SELL'").fetchall()]
    realized = sum(s["realized_usd"] for s in sells if s["realized_usd"])
    w = dict(con.execute("SELECT * FROM grid_wallet WHERE id=1").fetchone())
    eq = w["cash_usd"] + w["sol_qty"] * 107.0   # mark mid-range
    con.close()
    print(f"[test-a] ticks={n} completed_sell_fills={len(sells)} "
          f"realized_usd={realized:.4f} wallet_cash={w['cash_usd']:.4f} "
          f"sol={w['sol_qty']:.4f} marked_equity~{eq:.4f} (started 24)")
    assert len(sells) > 0, "expected completed cycles"
    assert realized > 0, "expected positive realized cycle profit before fees"
    print("[test-a] PASS: cycles > 0 and realized profit > 0 before fees")


def test_kill_switch_pause():
    # simulate bag accumulation: force-buy inventory near the top, then dump price
    gt.grid_tick(110.0, CFG)
    con = sqlite3.connect(TMP_DB)
    con.execute("DELETE FROM grid_state")
    con.execute("INSERT INTO grid_state(id,last_reset_ts) VALUES(1,NULL)")
    # hand-load the wallet like a fully invested top-tick bag
    con.execute("UPDATE grid_wallet SET cash_usd=2.0, sol_qty=0.20, peak_equity=24.0,"
                " paused=0 WHERE id=1")
    con.commit()
    con.close()
    acts = []
    for px in (104.0, 96.0, 90.0, 84.0):   # -20%+ off peak -> should trip
        acts += gt.grid_tick(px, CFG)
    w = gt.wallet()
    print(f"[test-b] actions={acts} paused={w['paused']}")
    assert w["paused"] == 1, "kill switch should have flipped paused"
    # while paused, further ticks must not open new buys even on downward crosses
    more = gt.grid_tick(85.0, CFG)
    assert any(a.get("action") == "paused" for a in more), more
    print("[test-b] PASS: mark-to-market DD tripped pause; buys blocked while paused")


def test_stale_price_and_hysteresis():
    # stale price -> full no-op tick
    import time as _t
    acts = gt.grid_tick(105.0, CFG, px_ts=_t.time() - 120)
    assert acts == [{"action": "stale_price_skip", "age_s": acts[0]["age_s"]}], acts
    print(f"[test-c] stale price rejected: {acts}")
    # hysteresis: small breach beyond edge does NOT reset within cooldown;
    # force last_reset_ts far in past to prove breach threshold still applies
    con = sqlite3.connect(TMP_DB)
    con.execute("UPDATE grid_state SET last_reset_ts=0")
    con.commit()
    con.close()
    st = gt.state()
    just_past_hi = st["range_high"] + (st["range_high"] - st["range_low"]) * 0.04 * 0.10
    acts = gt.grid_tick(just_past_hi, CFG)
    assert not any(a.get("action") == "grid_reset" for a in acts), \
        "small breach must not reset (hysteresis)"
    deep = st["range_high"] + (st["range_high"] - st["range_low"]) * 0.04 * 0.30
    acts = gt.grid_tick(deep, CFG)
    assert any(a.get("action") == "grid_reset" for a in acts), \
        "deep breach beyond cooldown must reset"
    print("[test-c] PASS: staleness bound + reset hysteresis/cooldown behave")


if __name__ == "__main__":
    test_cycles_profit()
    test_kill_switch_pause()
    test_stale_price_and_hysteresis()
    print("ALL GRID SELF-TESTS PASSED")
