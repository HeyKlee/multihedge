"""
MultiHedge GRID TRADER - standalone spot long-only geometric grid trader for SOL.

Virtual paper trader with its OWN wallet and tables in multihedge.db, fully
independent of the scalper/reasoner mh_accounts wallets:

  grid_wallet(cash_usd, sol_qty, peak_equity, paused, updated_ts)
  grid_trades(ts, side, level_px, qty, usd, cycle_id, realized_usd)
  grid_state(center_px, range_low, range_high, levels_json, resets)

Design:
  - Geometric grid of `grid.levels` levels spanning +/-`grid.range_pct` around a
    center price: level_k = center * (1+range_pct)**(k/(N//2)) for k in -N//2..N//2.
  - Each tick: if price crosses DOWN through a level with cash available, buy at
    that level (quote_bps slippage applied) and place a virtual sell one level up.
    When price crosses UP through a sell level, sell and realize the cycle profit
    back into the wallet.
  - Dynamic Grid Reset: if price exits the range by more than range_pct beyond an
    edge, rebuild the grid centered on current price; open inventory is kept and
    valued at market.
  - Wallet seeded ONCE (idempotent): starting_cash_nzd * usd_per_nzd USD cash.
  - Equity kill switch: track peak equity, pause new buys at >=20% drawdown
    (grid_wallet.paused flag), log loudly. Flash-crash guard: skip buys when the
    last close < prev close * flash_crash_drop.

CLI:
  python grid_trader.py tick [px]   # one tick at live price (or explicit px)
  python grid_trader.py status      # wallet + grid + recent trades as JSON
"""

import json
import math
import sqlite3
import sys
import time
from pathlib import Path

import yaml

DB_PATH = Path(__file__).parent / "multihedge.db"
CFG_PATH = Path(__file__).parent / "config.yaml"
SYMBOL = "SOL"

SCHEMA_WALLET = """
CREATE TABLE IF NOT EXISTS grid_wallet (
  id INTEGER PRIMARY KEY CHECK (id=1),
  cash_usd REAL NOT NULL,
  sol_qty REAL NOT NULL,
  peak_equity REAL NOT NULL,
  paused INTEGER NOT NULL DEFAULT 0,
  updated_ts REAL
)
"""
SCHEMA_TRADES = """
CREATE TABLE IF NOT EXISTS grid_trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  side TEXT NOT NULL,
  level_px REAL NOT NULL,
  qty REAL NOT NULL,
  usd REAL NOT NULL,
  cycle_id INTEGER,
  realized_usd REAL DEFAULT 0
)
"""
SCHEMA_STATE = """
CREATE TABLE IF NOT EXISTS grid_state (
  id INTEGER PRIMARY KEY CHECK (id=1),
  center_px REAL,
  range_low REAL,
  range_high REAL,
  levels_json TEXT,
  resets INTEGER NOT NULL DEFAULT 0,
  last_px REAL
)
"""


def _connect():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute(SCHEMA_WALLET)
    con.execute(SCHEMA_TRADES)
    con.execute(SCHEMA_STATE)
    # council amendment: reset cooldown tracking (safe migration)
    cols = [r[1] for r in con.execute("PRAGMA table_info(grid_state)").fetchall()]
    if "last_reset_ts" not in cols:
        con.execute("ALTER TABLE grid_state ADD COLUMN last_reset_ts REAL")
    con.commit()
    return con


def load_config():
    return yaml.safe_load(CFG_PATH.read_text(encoding="utf-8")) or {}


def cfg_grid(cfg=None):
    return {**{
        "enabled": True,
        "starting_cash_nzd": 40.0,
        "usd_per_nzd": 0.60,
        "grid_levels": 8,
        "range_pct": 0.04,
        "min_trade_value_usd": 1.0,
        "max_drawdown_kill": 0.20,
        "flash_crash_drop": 0.94,
        # go-live gate (grid-specific): min completed cycles, min profitable-cycle
        # rate over the window, and a net-realized-P&L floor (of starting capital).
        # fee-aware: gate_fixed_fee_usd_per_cycle models real per-cycle costs
        # (2 signatures: buy+sell; priority fee + base gas + any MEV). quote_bps
        # above already models slippage on both sides, so THIS is the added fixed
        # cost. Profitable-cycle rate and net realized are computed AFTER this fee.
        "gate_min_cycles": 30,
        "gate_min_profitable_rate": 0.55,
        "gate_min_realized_pct_of_cap": 0.01,   # >= +1% of starting capital
        "gate_fixed_fee_usd_per_cycle": 0.02,   # 2 txs, priority+gas ~$0.01 each
        "gate_enabled": True,
    }, **(cfg or {}).get("grid", {})}


# ------------------------------ seeding --------------------------------------
def seed_wallet(g):
    """Idempotent: create/seed the grid wallet exactly once."""
    start = float(g["starting_cash_nzd"]) * float(g["usd_per_nzd"])
    con = _connect()
    con.execute("INSERT OR IGNORE INTO grid_wallet(id,cash_usd,sol_qty,peak_equity,"
                "paused,updated_ts) VALUES(1,?,?,?,0,?)",
                (start, 0.0, start, time.time()))
    # also seed state row once so grid builds on first tick
    con.execute("INSERT OR IGNORE INTO grid_state(id) VALUES(1)")
    con.commit()
    con.close()
    return start


def wallet():
    con = _connect()
    r = con.execute("SELECT * FROM grid_wallet WHERE id=1").fetchone()
    con.close()
    return dict(r) if r else None


def state():
    con = _connect()
    r = con.execute("SELECT * FROM grid_state WHERE id=1").fetchone()
    con.close()
    return dict(r) if r else None


def equity(px):
    w = wallet()
    if not w:
        return 0.0
    return w["cash_usd"] + w["sol_qty"] * px


# ------------------------------ kill switch ----------------------------------
def kill_switch_check(px, g):
    """Update peak equity, pause new buys at >= max_drawdown_kill drawdown."""
    eq = equity(px)
    w = wallet()
    peak = max(float(w["peak_equity"]), eq)
    paused = w["paused"]
    dd = 1.0 - eq / peak if peak > 0 else 0.0
    if dd >= float(g["max_drawdown_kill"]) and not paused:
        paused = 1
        print(f"[kill-switch] grid: equity {eq:.2f} is {dd:.1%} off peak {peak:.2f} "
              f"(>= {float(g['max_drawdown_kill']):.0%}). NEW BUYS PAUSED.", flush=True)
    con = _connect()
    con.execute("UPDATE grid_wallet SET peak_equity=?, paused=?, updated_ts=? WHERE id=1",
                (peak, paused, time.time()))
    con.commit()
    con.close()
    return bool(paused), dd


def reset_kill_switch():
    """Manual un-pause + re-arm the peak at current equity."""
    con = _connect()
    con.execute("UPDATE grid_wallet SET paused=0, peak_equity=cash_usd+sol_qty*0, "
                "updated_ts=? WHERE id=1", (time.time(),))
    con.commit()
    con.close()


# ------------------------------ grid geometry --------------------------------
def build_levels(center, n_levels, range_pct):
    half = max(1, n_levels // 2)
    lo = center * (1.0 - range_pct)
    hi = center * (1.0 + range_pct)
    levels = []
    for k in range(-half, half + 1):
        px = center * ((1.0 + range_pct) ** (k / half))
        levels.append(round(px, 6))
    return sorted(set(levels)), lo, hi


def rebuild_grid(px, g):
    n = int(g["grid_levels"])
    rp = float(g["range_pct"])
    levels, lo, hi = build_levels(px, n, rp)
    con = _connect()
    cur = con.execute("SELECT resets FROM grid_state WHERE id=1").fetchone()
    resets = (cur["resets"] if cur else 0)
    con.execute("UPDATE grid_state SET center_px=?, range_low=?, range_high=?, "
                "levels_json=?, resets=?, last_px=?, last_reset_ts=? WHERE id=1",
                (px, lo, hi, json.dumps(levels), resets + 1, px, time.time()))
    con.commit()
    con.close()
    print(f"[grid] rebuilt around {px:.4f} ({len(levels)} levels, "
          f"{lo:.4f}-{hi:.4f}), reset #{resets + 1}", flush=True)
    return levels


def open_sells():
    """Open virtual sells: buy legs without a matching sell fill yet."""
    con = _connect()
    rows = [dict(r) for r in con.execute(
        "SELECT * FROM grid_trades WHERE side='BUY' ORDER BY id").fetchall()]
    sells = [dict(r) for r in con.execute(
        "SELECT * FROM grid_trades WHERE side='SELL' ORDER BY id").fetchall()]
    con.close()
    matched = set(s["cycle_id"] for s in sells)
    return [b for b in rows if b["cycle_id"] is None or b["cycle_id"] not in matched]


def next_cycle_id():
    con = _connect()
    r = con.execute("SELECT COALESCE(MAX(cycle_id),0)+1 AS c FROM grid_trades").fetchone()
    con.close()
    return int(r["c"])


# ------------------------------ core tick ------------------------------------
PRICE_MAX_AGE_S = 90.0       # council: reject prices older than this
RESET_COOLDOWN_S = 600.0     # council: min 10 min between dynamic resets
RESET_HYSTERESIS = 0.25      # council: must exceed edge by >=25% of range_pct


def _state_set(**kv):
    con = _connect()
    sets = ", ".join(f"{k}=?" for k in kv)
    con.execute(f"UPDATE grid_state SET {sets} WHERE id=1", tuple(kv.values()))
    con.commit()
    con.close()


def grid_tick(px, cfg=None, prev_close=None, px_ts=None):
    """One 30s-style evaluation pass at price `px`. Returns action dict list.

    Council guard: a stale or missing price (older than PRICE_MAX_AGE_S) makes
    the whole tick a no-op - no buys, no resets, no crash-guard evaluation."""
    cfg = cfg or {}
    g = cfg_grid(cfg)
    if not g.get("enabled", True):
        return [{"action": "disabled"}]
    if px is None or px <= 0:
        return [{"action": "no_price"}]
    now = time.time()
    if px_ts is None:
        px_ts = now
    if now - float(px_ts) > PRICE_MAX_AGE_S:
        return [{"action": "stale_price_skip", "age_s": round(now - float(px_ts), 1)}]
    seed_wallet(g)
    st = state()
    if prev_close is None:
        prev_close = st["last_px"]

    # first run / missing grid -> build centered on current price
    if not st["levels_json"]:
        rebuild_grid(px, g)
        st = state()
    levels = json.loads(st["levels_json"])
    acts = []

    # Dynamic Grid Reset with council hysteresis: price must be beyond the edge
    # by >= RESET_HYSTERESIS * range_pct of the range span, and at least
    # RESET_COOLDOWN_S must have passed since the last reset.
    span = st["range_high"] - st["range_low"]
    rp = float(g["range_pct"])
    breach_hi = px > st["range_high"] + span * rp * RESET_HYSTERESIS
    breach_lo = px < st["range_low"] - span * rp * RESET_HYSTERESIS
    last_reset = st.get("last_reset_ts") or 0.0
    if (breach_hi or breach_lo) and now - float(last_reset) >= RESET_COOLDOWN_S:
        rebuild_grid(px, g)
        st = state()
        levels = json.loads(st["levels_json"])
        acts.append({"action": "grid_reset"})

    # flash-crash guard input: caller may pass prev_close, else use last seen px
    if prev_close is None:
        prev_close = st["last_px"]
    flash = prev_close is not None and px < prev_close * float(g["flash_crash_drop"])

    # kill switch runs EVERY tick on marked equity (council amendment 1),
    # even during flash-crash ticks, so the pause fires on bag drawdown.
    paused, dd = kill_switch_check(px, g)

    # 1. check open virtual sells crossed upward -> sell, realize cycle profit
    for s in open_sells():
        target = s["level_px"] * (1.0 + float(g["range_pct"]) / max(1, int(g["grid_levels"]) // 2))
        if px >= target:
            w = wallet()
            gross = s["qty"] * px
            net = gross * (1.0 - float(cfg.get("paper", {}).get("quote_bps", 40)) / 10000.0)
            realized = net - s["usd"]
            con = _connect()
            con.execute("INSERT INTO grid_trades(ts,side,level_px,qty,usd,cycle_id,"
                        "realized_usd) VALUES(?,?,?,?,?,?,?)",
                        (time.time(), "SELL", px, s["qty"], net, s["cycle_id"],
                         realized))
            con.execute("UPDATE grid_wallet SET cash_usd=cash_usd+?, sol_qty=sol_qty-?, "
                        "updated_ts=? WHERE id=1",
                        (net,
                         s["qty"], time.time()))
            con.commit()
            con.close()
            acts.append({"action": "sell", "cycle": s["cycle_id"], "px": round(target, 4),
                         "realized_usd": round(realized, 4)})

    # 2. buys on downward crosses through levels (skip if paused / flash crash)
    if not paused and not flash:
        w = wallet()
        bps = float(cfg.get("paper", {}).get("quote_bps", 40)) / 10000.0
        bought_ids = {b["level_px"] / (1.0 + bps) for b in open_sells()}
        below = [lv for lv in levels if lv < st["last_px"] and px <= lv]
        for lv in below:
            if any(math.isclose(b, lv, rel_tol=1e-7, abs_tol=1e-6) for b in bought_ids):
                continue  # already filled this level (level_px may be stored as fill price), waiting for its sell leg
            spend = min(max(float(g["min_trade_value_usd"]), w["cash_usd"] / len(levels)), w["cash_usd"])
            if spend < float(g["min_trade_value_usd"]):
                break  # no cash
            bps = float(cfg.get("paper", {}).get("quote_bps", 40)) / 10000.0
            fill_px = lv * (1.0 + bps)
            qty = spend / fill_px
            cid = next_cycle_id()
            con = _connect()
            con.execute("INSERT INTO grid_trades(ts,side,level_px,qty,usd,cycle_id,"
                        "realized_usd) VALUES(?,?,?,?,?,?,0)",
                        (time.time(), "BUY", fill_px, qty, spend, cid))
            con.execute("UPDATE grid_wallet SET cash_usd=cash_usd-?, sol_qty=sol_qty+?, "
                        "updated_ts=? WHERE id=1", (spend, qty, time.time()))
            con.commit()
            con.close()
            w = wallet()
            acts.append({"action": "buy", "level": round(lv, 4), "qty": round(qty, 6),
                         "usd": round(spend, 2)})
    elif flash:
        acts.append({"action": "flash_crash_skip"})
    elif paused:
        acts.append({"action": "paused", "dd": round(dd, 4)})

    # 3. persist last seen price
    con = _connect()
    con.execute("UPDATE grid_state SET last_px=? WHERE id=1", (px,))
    con.commit()
    con.close()
    return acts


def status_json(px=None):
    g = cfg_grid(load_config())
    seed_wallet(g)
    w = wallet()
    st = state()
    con = _connect()
    trades = [dict(r) for r in con.execute(
        "SELECT * FROM grid_trades ORDER BY id DESC LIMIT 20").fetchall()]
    n_cycles = con.execute(
        "SELECT COUNT(*) c FROM grid_trades WHERE side='SELL'").fetchone()["c"]
    realized = con.execute(
        "SELECT COALESCE(SUM(realized_usd),0) s FROM grid_trades "
        "WHERE side='SELL' AND cycle_id IS NOT NULL").fetchone()["s"]
    con.close()
    mark = px or st["last_px"] or 0
    out = {
        "wallet": w, "equity_usd": round(equity(mark), 4),
        "grid": {k: st[k] for k in ("center_px", "range_low", "range_high",
                                    "resets", "last_px")},
        "open_sells": len(open_sells()),
        "cycles_completed": n_cycles,
        "realized_usd_total": round(realized, 4),
        "gate": grid_gate_status(load_config()),
        "recent_trades": trades,
    }
    return out


def grid_gate_status(cfg=None):
    """Grid go-live gate. Mirrors paper.paper_gate_status() but reads the grid's
    OWN table (grid_trades, realized_usd per completed SELL cycle) and applies
    grid-appropriate thresholds. Grid sells are mechanically set above buys, so a
    per-cycle profitable-rate alone would be near-tautological; the primary perf
    signal is NET REALIZED P&L over the window (cost-of-capital floor), with the
    profitable-cycle rate held as a hard secondary condition and the kill-switch
    arming as the safety backstop.

    REPORTING ONLY (paper reports; a future live grid engine must enforce via the
    same raise-write-nothing pattern as live_bridge.assert_live_allowed). This
    function never touches the wallet or changes trade behaviour on its own.
    """
    g = cfg_grid(cfg)
    if not g.get("gate_enabled", True):
        return {"eligible": True, "reason": "gate disabled"}
    w = wallet()
    kill_armed = bool(w and not w.get("paused"))
    min_cycles = int(g.get("gate_min_cycles", 30))
    fee = float(g.get("gate_fixed_fee_usd_per_cycle", 0.02))
    con = _connect()
    rows = [dict(r) for r in con.execute(
        "SELECT realized_usd FROM grid_trades "
        "WHERE side='SELL' AND cycle_id IS NOT NULL "
        "ORDER BY id DESC LIMIT ?", (min_cycles,)).fetchall()]
    con.close()
    n = len(rows)
    # NET realized: subtract modeled per-cycle fee (buy+sell signatures: priority
    # fee + base gas + MEV). A cycle is only a WIN if it nets positive after cost.
    gross = sum(r["realized_usd"] for r in rows)
    net_realized = gross - fee * n
    wins = sum(1 for r in rows if r["realized_usd"] > fee)
    rate = wins / n if n else 0.0
    start = float(g["starting_cash_nzd"]) * float(g["usd_per_nzd"])
    min_rate = float(g.get("gate_min_profitable_rate", 0.55))
    floor = start * float(g.get("gate_min_realized_pct_of_cap", 0.01))

    checks = {
        "completed_cycles": n, "min_cycles": min_cycles,
        "wins": wins, "profitable_rate": round(rate, 3),
        "min_profitable_rate": min_rate,
        "gross_realized_usd": round(gross, 4),
        "fee_usd_per_cycle": fee,
        "total_fee_usd": round(fee * n, 4),
        "net_realized_usd": round(net_realized, 4),
        "net_floor_usd": round(floor, 4),
        "kill_switch_armed": kill_armed,
    }
    eligible = (n >= min_cycles and rate >= min_rate
                and net_realized >= floor and kill_armed)
    reasons = []
    if n < min_cycles:
        reasons.append(f"need >= {min_cycles} completed cycles (have {n})")
    if rate < min_rate:
        reasons.append(f"need >= {min_rate:.2f} profitable-rate (have {round(rate,3)})")
    if net_realized < floor:
        reasons.append(
            f"need >= ${floor:.2f} NET realized after ${fee:.2f}/cycle fees "
            f"(have ${net_realized:.2f})")
    if not kill_armed:
        reasons.append("kill-switch paused (drawdown kill active)")
    return {"eligible": eligible, "reason": "READY for live" if eligible
            else "; ".join(reasons), **checks}


def _loom_log(msg):
    print("[%s] [grid] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


def run_loom_tick(cfg):
    """Fetch a live price and run one grid tick. Returns the action list."""
    import pricefeed
    coins = {c["symbol"]: c["mint"] for c in cfg.get("coins", [])}
    px = pricefeed.live_price(coins.get(SYMBOL, ""), SYMBOL)
    if not px:
        _loom_log("no price available, skipping tick")
        return []
    actions = grid_tick(px, cfg)
    for a in actions:
        if a.get("action") not in ("hold",):
            _loom_log(json.dumps(a, default=str))
    return actions


if __name__ == "__main__":
    cfg = load_config()
    mode = sys.argv[1] if len(sys.argv) > 1 else "status"
    if mode == "loom":
        # Daemon loop: tick on the shared cadence, like multihedge.py loom.
        cadence = int(cfg.get("paper", {}).get("trade_cadence_s", 30))
        _loom_log("grid loom started (cadence %ss)" % cadence)
        while True:
            try:
                run_loom_tick(cfg)
            except Exception as e:  # never die on a single bad tick
                _loom_log("loom tick error: %r" % e)
            time.sleep(cadence)
    elif mode == "tick":
        px = float(sys.argv[2]) if len(sys.argv) > 2 else None
        if px is None:
            import pricefeed
            coins = {c["symbol"]: c["mint"] for c in cfg.get("coins", [])}
            px = pricefeed.live_price(coins.get(SYMBOL, ""), SYMBOL)
        if px:
            print(json.dumps(grid_tick(px, cfg), indent=2, default=str))
        else:
            print("no price available")
    else:
        print(json.dumps(status_json(), indent=2, default=str))
