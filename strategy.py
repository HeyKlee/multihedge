"""
MultiHedge STRATEGY LAYER — per-coin fresch strategy set with rotation.

Each coin runs its own independent rotation across a set of setup families.
A setup produces a signal (LONG / SHORT / FLAT) from a coin's price history.
Wins/losses feed per-(coin,setup) points and Beta posterior, exactly like
AutoHedge's strat_select, but keyed by coin too so each coin is judged on its
own strategy edge (a breakout coin and a reversion coin both discover and ride
their own best setup).

Rotation: explore each (coin,setup) until it has a minimum sample, then rank by
points; hottest setup per coin is "active" and gets traded first. Failing
setups fade and are eventually disabled.
"""

import math
import random
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).parent / "multihedge.db"

# Setup families available. Each implements sign(prices) -> "LONG"/"SHORT"/"FLAT"
# where prices is a deque/list of most-recent closes (newest last).
SETUP_FAMILIES = {
    "momentum_breakout": ("Momentum Breakout", "Rides breakouts above recent highs / below lows."),
    "mean_reversion": ("Mean Reversion", "Buys sharp dips, sells sharp spikes, bets on snap-back."),
    "rsi_oversold": ("RSI(2) Oversold", "Buys deep-oversold captures, sells deep-overbought."),
    "vwap_reversion": ("VWAP Reversion", "Fades large deviations from rolling volume-weighted price."),
}

# Council calibration (Aug-26): mean-reversion and VWAP dev triggers were 2.0%,
# but measured 5-min/1h ranges on SOL/JUP rarely exceed ~1-2%, so the scalper sat
# FLAT for days. Config override: paper.signal_dev_pct (default now 0.9%).
try:
    import yaml as _yaml
    _cfg = _yaml.safe_load((Path(__file__).parent / "config.yaml").read_text()) or {}
    DEV_TRIGGER = float(_cfg.get("paper", {}).get("signal_dev_pct", 0.009))
except Exception:
    DEV_TRIGGER = 0.009

EXPLORE_TRADES = 6
EPSILON = 0.12

SCHEMA_STATE = """
CREATE TABLE IF NOT EXISTS mh_strategy_state (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  window_trades INTEGER DEFAULT 0,
  window_wins INTEGER DEFAULT 0,
  window_losses INTEGER DEFAULT 0,
  window_start_ts REAL
)
"""
SCHEMA_POINTS = """
CREATE TABLE IF NOT EXISTS mh_strat_points (
  coin TEXT, setup TEXT,
  points INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (coin, setup)
)
"""
SCHEMA_BETA = """
CREATE TABLE IF NOT EXISTS mh_strategy_beta (
  coin TEXT, setup TEXT,
  wins INTEGER NOT NULL DEFAULT 0,
  losses INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (coin, setup)
)
"""


def _connect():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute(SCHEMA_STATE)
    con.execute(SCHEMA_POINTS)
    con.execute(SCHEMA_BETA)
    con.execute("CREATE TABLE IF NOT EXISTS mh_strategy_visits (coin TEXT, setup TEXT, visits INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(coin,setup))")
    con.execute("CREATE TABLE IF NOT EXISTS mh_strategy_selection (coin TEXT PRIMARY KEY, setup TEXT, reason TEXT, selected_ts REAL, selections INTEGER NOT NULL DEFAULT 1)")
    con.execute("CREATE TABLE IF NOT EXISTS mh_optimization_log (id INTEGER PRIMARY KEY, ts REAL, coin TEXT, setup TEXT, event TEXT, detail TEXT)")
    con.commit()
    return con


# ------------------------- signal implementations ----------------------------
FLASH_CRASH_DROP = 0.94   # single-candle collapse guard: skip LONG if close < prev*0.94


def _flash_crash(closes):
    """True when the latest candle is a collapse (close < prev close * 0.94).
    Long-side reversion signals stand aside: a -6% single-candle drop is more
    likely a news shock than a dip to fade."""
    return len(closes) >= 2 and closes[-1] < closes[-2] * FLASH_CRASH_DROP


def _rs2(closes):
    """RSI(2) raw value from a closes list (newest last)."""
    if len(closes) < 3:
        return 50.0
    closes = closes[-3:]
    gains = losses = 0.0
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        if d > 0:
            gains += d
        else:
            losses -= d
    if losses == 0:
        return 100.0 if gains > 0 else 50.0
    rs = (gains / len(closes)) / (losses / len(closes))
    return 100.0 - 100.0 / (1.0 + rs)


def _vwap(closes):
    if not closes:
        return None
    vol = [1.0] * len(closes)  # equal weight (real apps use volume; fine here)
    tp_total = sum(c * v for c, v in zip(closes, vol))
    v_total = sum(vol)
    return (tp_total / v_total) if v_total else None


def momentum_breakout_signal(prices):
    if len(prices) < 20:
        return "FLAT"
    ref_high = max(prices[-20:-1])
    ref_low = min(prices[-20:-1])
    last = prices[-1]
    if last > ref_high:
        return "LONG"
    if last < ref_low:
        return "SHORT"
    return "FLAT"


def mean_reversion_signal(prices):
    if len(prices) < 10:
        return "FLAT"
    last = prices[-1]
    mean = sum(prices[-10:]) / 10.0
    dev = (last - mean) / mean
    if dev < -DEV_TRIGGER:
        if _flash_crash(prices):
            return "FLAT"   # flash-crash guard: do not fade a collapse
        return "LONG"     # dipped hard, bet snap-back up
    if dev > DEV_TRIGGER:
        return "SHORT"
    return "FLAT"


def rsi_oversold_signal(prices):
    if _flash_crash(prices):
        return "FLAT"     # flash-crash guard: do not fade a collapse
    r = _rs2(prices)
    if r <= 15:
        return "LONG"
    if r >= 85:
        return "SHORT"
    return "FLAT"


def vwap_reversion_signal(prices):
    if len(prices) < 15:
        return "FLAT"
    last = prices[-1]
    vwp = _vwap(prices[-15:])
    if vwp is None:
        return "FLAT"
    dev = (last - vwp) / vwp
    if dev < -DEV_TRIGGER:
        if _flash_crash(prices):
            return "FLAT"   # flash-crash guard: do not fade a collapse
        return "LONG"
    if dev > DEV_TRIGGER:
        return "SHORT"
    return "FLAT"


# Map setup name -> signal fn
SETUP_SIGNALS = {
    "momentum_breakout": momentum_breakout_signal,
    "mean_reversion": mean_reversion_signal,
    "rsi_oversold": rsi_oversold_signal,
    "vwap_reversion": vwap_reversion_signal,
}


# --------------------------- Beta posterior --------------------------------
def _posterior_goal_prob(wins, losses):
    a = wins + 1.0
    b = losses + 1.0
    n = a + b
    mean = a / n
    var = (a * b) / (n * n * (n + 1.0))
    sd = math.sqrt(var) if var > 0 else 0.0
    if sd == 0:
        return 1.0 if mean > 0.5 else 0.0
    z = (mean - 0.5) / sd
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _ensure(coin, setup, con=None):
    own = con is None
    con = con if con is not None else _connect()
    con.execute("INSERT OR IGNORE INTO mh_strat_points(coin,setup,points) VALUES(?,?,0)",
                (coin, setup))
    con.execute("INSERT OR IGNORE INTO mh_strategy_beta(coin,setup,wins,losses) "
                "VALUES(?,?,0,0)", (coin, setup))
    if own:
        con.commit()
        con.close()


def _ranked(coin, setups):
    con = _connect()
    rows = {r["setup"]: r["points"] for r in con.execute(
        "SELECT setup, points FROM mh_strat_points WHERE coin=?", (coin,)).fetchall()}
    con.close()
    order = {s: i for i, s in enumerate(SETUP_FAMILIES)}
    return sorted(setups, key=lambda s: (-rows.get(s, 0), order.get(s, 999)))


def choose_setup(coin, setups=None, enabled=None):
    """Pick the next strategy for a coin (rotation). enabled is a set of setup names."""
    setups = setups or list(SETUP_SIGNALS.keys())
    enabled = enabled if enabled is not None else set(setups)
    setups = [s for s in setups if s in enabled]
    if not setups:
        return None
    # exploration floor: least-tried first
    con = _connect()
    totals = {r["setup"]: (r["wins"], r["losses"]) for r in con.execute(
        "SELECT setup, wins, losses FROM mh_strategy_beta WHERE coin=?", (coin,)).fetchall()}
    visits = {r["setup"]: r["visits"] for r in con.execute("SELECT setup, visits FROM mh_strategy_visits WHERE coin=?", (coin,))}
    con.close()
    under = [s for s in setups if sum(totals.get(s, (0, 0))) < EXPLORE_TRADES]
    if under:
        chosen = min(under, key=lambda s: (visits.get(s, 0), sum(totals.get(s, (0, 0)))))
        reason = "exploration"
    elif random.random() < EPSILON:
        chosen, reason = random.choice(setups), "epsilon_exploration"
    else:
        chosen, reason = _ranked(coin, setups)[0], "learned_points"
    con = _connect()
    con.execute("INSERT INTO mh_strategy_visits(coin,setup,visits) VALUES(?,?,1) ON CONFLICT(coin,setup) DO UPDATE SET visits=visits+1", (coin, chosen))
    con.execute("INSERT INTO mh_strategy_selection(coin,setup,reason,selected_ts,selections) VALUES(?,?,?,?,1) ON CONFLICT(coin) DO UPDATE SET setup=excluded.setup, reason=excluded.reason, selected_ts=excluded.selected_ts, selections=selections+1", (coin,chosen,reason,time.time()))
    con.commit()
    con.close()
    return chosen


def record_trade(coin, setup, realized_pct):
    _ensure(coin, setup)
    win = 1 if (realized_pct or 0) > 0 else 0
    loss = 1 - win
    con = _connect()
    con.execute("INSERT INTO mh_strat_points(coin,setup,points) VALUES(?,?,?) "
                "ON CONFLICT(coin,setup) DO UPDATE SET points=points+excluded.points",
                (coin, setup, (1 if win else -1)))
    con.execute("INSERT INTO mh_strategy_beta(coin,setup,wins,losses) VALUES(?,?,?,?) "
                "ON CONFLICT(coin,setup) DO UPDATE SET "
                "wins=wins+excluded.wins, losses=losses+excluded.losses",
                (coin, setup, win, loss))
    con.execute("INSERT INTO mh_optimization_log(ts,coin,setup,event,detail) VALUES(?,?,?,?,?)",
                (time.time(),coin,setup,"trade_feedback",str(realized_pct)))
    con.commit()
    con.close()


def summary(coin=None):
    con = _connect()
    rows = [dict(r) for r in con.execute(
        "SELECT * FROM mh_strategy_beta").fetchall()]
    pts = {r["coin"] + ":" + r["setup"]: r["points"] for r in con.execute(
        "SELECT coin, setup, points FROM mh_strat_points").fetchall()}
    con.close()
    out = []
    for r in rows:
        c, s = r["coin"], r["setup"]
        n = r["wins"] + r["losses"]
        gp = _posterior_goal_prob(r["wins"], r["losses"]) if n else None
        meta = SETUP_FAMILIES.get(s, (s, ""))
        out.append({
            "coin": c, "setup": s, "name": meta[0], "desc": meta[1],
            "points": pts.get(c + ":" + s, 0), "wins": r["wins"], "losses": r["losses"],
            "n": n, "win_rate": round((r["wins"] / n), 3) if n else 0.0,
            "goal_prob": round(gp, 3) if gp is not None else None,
        })
    if coin:
        out = [r for r in out if r["coin"] == coin]
    out.sort(key=lambda r: (r["n"] == 0, -r["points"]))
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(summary(), indent=2, default=str))