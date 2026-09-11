"""
MultiHedge PAPER ENGINE - virtual multi-trader ledger with REAL Jupiter fills,
NO wallet writes.

PER-TRADER INDEPENDENT WALLETS. Each TRADER owns its OWN paper wallet (an
mh_accounts row keyed by trader: 'scalper' = fast engine, 'reasoner' = slow
news-swing trader). A trader sizes every coin position it opens from ITS OWN
wallet AVAILABLE capital and returns realized P&L back to ITS OWN wallet. There
is NO shared treasury and NO per-coin accounting: the scalper wallet and the
reasoner wallet are fully independent, so a blow-up in one trader cannot touch
the other. This is how we run each trader as a clean experiment and pick a
winner to promote to the real wallet.

The two traders keep their positions in separate tables (scalper ->
mh_positions, reasoner -> mh_reasoner_positions), so 'committed(trader)' sums
the correct table across ALL coins owned by that trader.

Each tick the engine:
  1. pulls the latest price history,
  2. asks strategy.choose_setup for the active setup,
  3. runs that setup's signal,
  4. if LONG/SHORT and no open position, opens a virtual position sized from
     ITS OWN trader wallet AVAILABLE capital, priced with a REAL Jupiter quote
     (plus slippage) but NEVER submitted to a wallet,
  5. holds, and closes at the trailing stop / take-profit / time-out, realizing
     PnL back into its own trader wallet and feeding it to strategy.record_trade.

Results live in multihedge.db (mh_positions, mh_trades). The LIVE GATE is read
here so paper knows whether a trader would currently pass the 3:1/7:1 rule, but
this file NEVER writes on-chain.
"""

import sqlite3
import math
import time
from pathlib import Path

import strategy as strat
import pricefeed  # noqa: F401

DB_PATH = Path(__file__).parent / "multihedge.db"
DEFAULT_EQUITY = 24.0           # per-trader default (~NZ$40 / US$24 each)
MAX_HOLD_S = 3600          # 1 hour virtual max-hold per trade
TP_PCT = 0.025             # take profit +2.5%
SL_PCT = -0.015            # stop loss -1.5%
TRAIL_ARM_PCT = 0.012      # arm a trailing stop once up +1.2%
TRAIL_DIST_PCT = 0.006     # trail 0.6% behind the peak once armed

TRADER_SCALPER = "scalper"
TRADER_REASONER = "reasoner"
TRADER_WHALE_TRADER = "whale_trader"
TRADER_MEMECOIN = "memecoin_trader"
TRADERS = (TRADER_SCALPER, TRADER_REASONER, TRADER_WHALE_TRADER, TRADER_MEMECOIN)

SCHEMA_POS = """
CREATE TABLE IF NOT EXISTS mh_positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  coin TEXT NOT NULL,
  symbol TEXT,
  setup TEXT NOT NULL,
  side TEXT NOT NULL,
  open_ts REAL NOT NULL,
  entry_px REAL NOT NULL,
  qty REAL NOT NULL,
  peak_px REAL,
  trail_armed INTEGER DEFAULT 0
)
"""
SCHEMA_TRADES = """
CREATE TABLE IF NOT EXISTS mh_trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  coin TEXT NOT NULL,
  symbol TEXT,
  setup TEXT NOT NULL,
  side TEXT NOT NULL,
  open_ts REAL NOT NULL,
  close_ts REAL NOT NULL,
  entry_px REAL NOT NULL,
  exit_px REAL NOT NULL,
  qty REAL NOT NULL,
  realized_pct REAL NOT NULL,
  realized_usd REAL NOT NULL,
  exit_reason TEXT NOT NULL
)
"""
SCHEMA_ACCOUNTS = """
CREATE TABLE IF NOT EXISTS mh_accounts (
  trader TEXT PRIMARY KEY,
  equity_usd REAL NOT NULL,
  started_usd REAL NOT NULL
)
"""
# Equity kill switch state (running peak + paused flag) per trader.
KILL_DD = 0.20              # pause new entries if equity falls >= 20% from peak
SCHEMA_RISK = """
CREATE TABLE IF NOT EXISTS mh_risk_state (
  trader TEXT PRIMARY KEY,
  peak_equity REAL NOT NULL,
  paused INTEGER NOT NULL DEFAULT 0
)
"""


def _connect():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute(SCHEMA_POS)
    con.execute(SCHEMA_TRADES)
    con.execute(SCHEMA_ACCOUNTS)
    con.execute(SCHEMA_RISK)
    con.commit()
    return con


# ------------------------------ per-trader wallets ---------------------------
def ensure_account(trader, starting):
    """Ensure THIS trader's own wallet exists (seeded at `starting` if new)."""
    con = _connect()
    con.execute("INSERT OR IGNORE INTO mh_accounts(trader,equity_usd,started_usd) "
                "VALUES(?,?,?)", (trader, starting, starting))
    con.commit()
    con.close()


def set_equity(trader, equity_usd, started_usd=None):
    """Hard-set this trader's wallet. Used for resets."""
    if started_usd is None:
        started_usd = equity_usd
    con = _connect()
    con.execute("INSERT INTO mh_accounts(trader,equity_usd,started_usd) VALUES(?,?,?) "
                "ON CONFLICT(trader) DO UPDATE SET equity_usd=excluded.equity_usd, "
                "started_usd=excluded.started_usd",
                (trader, float(equity_usd), float(started_usd)))
    con.commit()
    con.close()


def set_account(trader, equity_usd, started_usd=None):
    set_equity(trader, equity_usd, started_usd)


def equity(trader):
    """Return THIS trader's own wallet equity."""
    con = _connect()
    row = con.execute("SELECT equity_usd FROM mh_accounts WHERE trader=?", (trader,)).fetchone()
    con.close()
    return row["equity_usd"] if row else DEFAULT_EQUITY


def _committed_from(table, coin_col, px_col):
    """Sum committed entry*qty over one positions table (all coins of a trader)."""
    con = _connect()
    tot = 0.0
    try:
        for r in con.execute(
                f"SELECT {px_col} AS px, qty FROM {table}").fetchall():
            tot += (r["px"] or 0.0) * (r["qty"] or 0.0)
    except Exception:
        pass
    con.close()
    return tot


def committed(trader):
    """Capital currently committed to THIS trader's open positions across ALL
    its coins. Scalper positions live in mh_positions; reasoner positions live
    in mh_reasoner_positions. Wallets are isolated per trader."""
    if trader == TRADER_SCALPER:
        return _committed_from("mh_positions", "coin", "entry_px")
    if trader == TRADER_REASONER:
        return _committed_from("mh_reasoner_positions", "symbol", "entry")
    if trader == TRADER_WHALE_TRADER:
        return _committed_from("mh_whale_positions", "symbol", "entry")
    if trader == TRADER_MEMECOIN:
        return _committed_from("mh_memecoin_positions", "symbol", "entry")
    # unknown trader: treat as zero to stay safe
    return 0.0


def available(trader):
    """This trader's wallet capital not already committed to an open position."""
    return max(0.0, equity(trader) - committed(trader))


def open_position(coin, entry_px, qty, side, setup):
    con = _connect()
    con.execute(
        "INSERT INTO mh_positions(coin,symbol,setup,side,open_ts,entry_px,qty,peak_px,trail_armed) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (coin, coin, setup, side, time.time(), entry_px, qty, entry_px, 0))
    con.commit()
    con.close()


def open_positions(coin=None):
    con = _connect()
    if coin:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM mh_positions WHERE coin=?", (coin,)).fetchall()]
    else:
        rows = [dict(r) for r in con.execute("SELECT * FROM mh_positions").fetchall()]
    con.close()
    return rows


def update_peak(pos_id, peak_px, trail_armed):
    con = _connect()
    con.execute("UPDATE mh_positions SET peak_px=?, trail_armed=? WHERE id=?",
                (peak_px, trail_armed, pos_id))
    con.commit()
    con.close()


def close_position(pos, exit_px, reason):
    if not math.isfinite(exit_px) or exit_px <= 0:
        raise ValueError("exit price must be finite and positive")
    con = _connect()
    con.execute("BEGIN IMMEDIATE")
    current = con.execute("SELECT * FROM mh_positions WHERE id=?", (pos["id"],)).fetchone()
    if current is None:
        con.close()
        return None
    pos = dict(current)
    side = pos["side"]
    qty = pos["qty"]
    entry = pos["entry_px"]
    if entry <= 0:
        con.close()
        return None
    if side == "LONG":
        pct = (exit_px - entry) / entry
    else:
        pct = (entry - exit_px) / entry
    realized_usd = qty * entry * pct
    con.execute(
        "INSERT INTO mh_trades(coin,symbol,setup,side,open_ts,close_ts,entry_px,exit_px,"
        "qty,realized_pct,realized_usd,exit_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (pos["coin"], pos.get("symbol"), pos["setup"], side, pos["open_ts"],
         time.time(), entry, exit_px, qty, pct, realized_usd, reason))
    # P&L flows back into THE SCALPER's own wallet (mh_positions are scalper-only)
    con.execute("UPDATE mh_accounts SET equity_usd=equity_usd+? WHERE trader=?",
                (realized_usd, TRADER_SCALPER))
    con.execute("DELETE FROM mh_positions WHERE id=?", (pos["id"],))
    con.commit()
    con.close()
    # feed strategy rotation
    strat.record_trade(pos["coin"], pos["setup"], pct)
    return {"pct": pct, "usd": realized_usd, "reason": reason}


# ------------------------------ equity kill switch ---------------------------
def kill_switch_check(trader, max_dd=KILL_DD):
    """Update the running equity peak for THIS trader and flip the paused flag
    when equity has fallen >= max_dd from that peak. Returns (paused, drawdown).
    Pausing blocks NEW entries only; open positions still manage their exits."""
    eq = equity(trader)
    con = _connect()
    con.execute("INSERT OR IGNORE INTO mh_risk_state(trader,peak_equity,paused) "
                "VALUES(?,?,0)", (trader, eq))
    row = con.execute("SELECT peak_equity, paused FROM mh_risk_state WHERE trader=?",
                      (trader,)).fetchone()
    peak = max(float(row["peak_equity"]), eq)
    dd = 1.0 - eq / peak if peak > 0 else 0.0
    paused = row["paused"]
    if dd >= max_dd and not paused:
        paused = 1
        print(f"[kill-switch] {trader}: equity {eq:.2f} is {dd:.1%} off peak {peak:.2f} "
              f"(>= {max_dd:.0%}). NEW ENTRIES PAUSED.", flush=True)
    con.execute("UPDATE mh_risk_state SET peak_equity=?, paused=? WHERE trader=?",
                (peak, paused, trader))
    con.commit()
    con.close()
    return bool(paused), dd


def is_paused(trader):
    con = _connect()
    row = con.execute("SELECT paused FROM mh_risk_state WHERE trader=?",
                      (trader,)).fetchone()
    con.close()
    return bool(row["paused"]) if row else False


def reset_kill_switch(trader):
    """Manual un-pause + re-arm the peak at current equity."""
    eq = equity(trader)
    con = _connect()
    con.execute("INSERT INTO mh_risk_state(trader,peak_equity,paused) VALUES(?,?,0) "
                "ON CONFLICT(trader) DO UPDATE SET peak_equity=excluded.peak_equity, paused=0",
                (trader, eq))
    con.commit()
    con.close()


# ------------------------------ position sizing ------------------------------
def size_trade(trader, entry_px, fraction=0.20, min_usd=1.0):
    """Size a trade off THIS trader's own available wallet capital."""
    if not all(math.isfinite(v) for v in (entry_px, fraction, min_usd)) or not 0 < fraction <= 1 or entry_px <= 0 or min_usd < 0:
        return 0.0
    free = available(trader)
    budget = free * fraction
    if budget < min_usd:
        return 0.0
    return budget / entry_px if entry_px > 0 else 0.0


# ------------------------------ live gate check ------------------------------
def paper_gate_status(trader, cfg):
    """Does this trader pass the live gate? Paper only reports; live_bridge enforces."""
    live = cfg.get("live", {})
    if not live.get("gate_enabled", True):
        return {"eligible": True, "reason": "gate disabled"}
    min_wr = live.get("min_win_rate", 0.75)
    min_n = live.get("min_closed_trades", 20)
    con = _connect()
    wins = losses = 0
    # source-of-truth for which trades belong to a trader:
    #   scalper  = mh_positions history rows with setup NOT IN other traders
    #   reasoner = setup == 'reasoner'
    #   whale_trader = setup == 'whale_trader'
    if trader == TRADER_SCALPER:
        where = "setup NOT IN ('reasoner','whale_trader','memecoin_trader')"
    elif trader == TRADER_WHALE_TRADER:
        where = "setup = 'whale_trader'"
    elif trader == TRADER_MEMECOIN:
        where = "setup = 'memecoin_trader'"
    else:
        where = "setup = 'reasoner'"
    for r in con.execute(
            f"SELECT realized_pct FROM mh_trades WHERE {where}").fetchall():
        if r["realized_pct"] > 0:
            wins += 1
        else:
            losses += 1
    con.close()
    n = wins + losses
    wr = wins / n if n else 0.0
    eligible = n >= min_n and wr >= min_wr
    return {
        "trader": trader, "n": n, "wins": wins, "losses": losses,
        "win_rate": round(wr, 3),
        "eligible": eligible,
        "reason": ("READY for live" if eligible else
                   f"needs >= {min_n} trades (have {n}) and >= {min_wr} win-rate (have {round(wr,3)})"),
    }


def close_checks(pos, px, cfg):
    """Return exit reason or None to hold. Includes trailing stop."""
    side = pos["side"]
    if side == "LONG":
        pct = (px - pos["entry_px"]) / pos["entry_px"]
    else:
        pct = (pos["entry_px"] - px) / pos["entry_px"]
    peak = pos.get("peak_px") or pos["entry_px"]
    # update peak
    if (side == "LONG" and px > peak) or (side == "SHORT" and px < peak):
        peak = px
    if pct >= TP_PCT:
        return "take_profit"
    if pct <= SL_PCT:
        return "stop_loss"
    if time.time() - pos["open_ts"] >= MAX_HOLD_S:
        return "max_hold"
    # trailing stop: once up past arm, trail behind peak
    armed = pos.get("trail_armed")
    if not armed and pct >= TRAIL_ARM_PCT:
        armed = 1
    if armed:
        if side == "LONG" and (peak - px) / peak >= TRAIL_DIST_PCT:
            return "trail_stop"
        if side == "SHORT" and (px - peak) / peak >= TRAIL_DIST_PCT:
            return "trail_stop"
    return None
