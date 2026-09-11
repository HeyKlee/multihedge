"""
MultiHedge WHALE TRADER - copy-trade layer. THIRD independent paper trader
(trader='whale_trader') with its own wallet in mh_accounts, its own positions
table (mh_whale_positions), and its own trade rows in mh_trades
(setup='whale_trader') so the dashboard shows it like the scalper/reasoner.

It ONLY trades on mh_news_bias rows with provider='whale_tracker' (fresh
on-chain buys of SOL/JUP/ETH by the 5 tracked fomo.family whales, pushed by
track_whales.py). Generic news bias is ignored.

Guardrails: long-only, per-trader 20% drawdown kill switch, TP/SL/max-hold
exits, per-signal dedupe (one trade per whale event id) so a repeat-poll of
the same buy never re-enters. Never writes on-chain.
"""

import sqlite3
import time
from pathlib import Path

import paper
import pricefeed
from config import COINS

DB_PATH = Path(__file__).parent / "multihedge.db"
paper.DB_PATH = DB_PATH

TRADER = paper.TRADER_WHALE_TRADER       # 'whale_trader'
SETUP_TAG = "whale_trader"
PROVIDER_TAG = "whale_tracker"

# 5 VERIFIED fomo.family wallets (mirrors track_whales.py)
WHALES = {
    "Unipcs":    "2heJbC32Tpfcb3nbUb5ER61K11FGZVfVGtVnDm6LDogF",
    "change":    "J9WiAZKf8JnCkHFL8fLCCXdEgdoLjLRqU2EGsDjdqYga",
    "frank":     "498g1rVnFcnjBjpfw1xyqA1WvgQXUU8RWuELjxkjAayQ",
    "Ethermonk": "2xUbYAVq1oJGj45d6JjnaYHAke3NQecUcqWvvVbwmYw8",
    "Avast":     "8xL8S7P4QLdTGRquHas8NP5EVjp2qUGbmSgrkh97mvmq",
}

DEFAULT_PARAMS = {
    "POSITION_FRACTION": 0.40,
    "TAKE_PROFIT": 0.05,
    "STOP_LOSS": 0.02,
    "MAX_HOLD_SECS": 6 * 3600,
    "TRAIL_ARM": 0.02,
    "TRAIL_DIST": 0.01,
    "CONFIDENCE_MIN": 0.80,
}

# paper.py gate stats: which mh_trades.setup tags belong to THIS trader
GATE_SETUPS = {"whale_trader"}


def _connect():
    con = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("""CREATE TABLE IF NOT EXISTS mh_whale_positions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL, side TEXT NOT NULL, qty REAL NOT NULL,
        entry REAL NOT NULL, ts REAL NOT NULL, entry_signal REAL, peak REAL)""")
    con.commit()
    return con


def _bias(symbol):
    """Freshest actionable whale_tracker bias for a symbol, or None."""
    con = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    con.row_factory = sqlite3.Row
    row = con.execute(
        "SELECT * FROM mh_news_bias WHERE symbol=? AND provider=? "
        "ORDER BY id DESC LIMIT 1", (symbol, PROVIDER_TAG)).fetchone()
    con.close()
    if not row:
        return None
    if time.time() - row["ts"] > 3600:
        return None  # whale signals are actionable ~1h
    return dict(row)


def tick():
    paper.ensure_account(TRADER, paper.DEFAULT_EQUITY)
    paused, _ = paper.kill_switch_check(TRADER)
    for symbol in COINS.keys():
        try:
            px = pricefeed.live_price(COINS[symbol]["mint"], symbol)
        except Exception:
            continue
        if not px:
            continue

        con = _connect()
        pos = con.execute(
            "SELECT * FROM mh_whale_positions WHERE symbol=? ORDER BY id DESC LIMIT 1",
            (symbol,)).fetchone()
        con.close()

        if pos:
            side = pos["side"]
            pct = (px - pos["entry"]) / pos["entry"] if side == "LONG" else \
                  (pos["entry"] - px) / pos["entry"]
            if pct >= DEFAULT_PARAMS["TAKE_PROFIT"]:
                _close(symbol, dict(pos), px, "take_profit")
            elif pct <= -DEFAULT_PARAMS["STOP_LOSS"]:
                _close(symbol, dict(pos), px, "stop_loss")
            elif time.time() - pos["ts"] >= DEFAULT_PARAMS["MAX_HOLD_SECS"]:
                _close(symbol, dict(pos), px, "max_hold")
            continue

        if paused:
            continue  # kill switch: no NEW entries

        bias = _bias(symbol)
        if bias and bias["direction"] == "UP" and \
                bias["confidence"] >= DEFAULT_PARAMS["CONFIDENCE_MIN"]:
            _open(symbol, "LONG", px, bias)


def _open(symbol, side, px, bias):
    # dedupe: one entry per bias row (id); skip if already traded this signal
    sig_id = bias.get("id")
    con = _connect()
    traded = con.execute(
        "SELECT 1 FROM mh_trades WHERE setup=? AND coin=? AND exit_reason LIKE 'sig:%' "
        "AND CAST(substr(exit_reason,5) AS INTEGER)=?",
        (SETUP_TAG, symbol, sig_id)).fetchone()
    con.close()
    if traded:
        return
    con = _connect()
    sig_id = bias.get("id")
    consumed = con.execute("SELECT 1 FROM mh_consumed_signals WHERE trader=? AND signal_id=?",
                           (TRADER, sig_id)).fetchone()
    con.close()
    if consumed:
        return

    free = paper.available(TRADER)
    budget = free * DEFAULT_PARAMS["POSITION_FRACTION"]
    qty = budget / px if px > 0 else 0.0
    if qty <= 0 or budget < 1.0:
        print(f"[whale_trader] skip {symbol}: budget ${budget:.2f} < $1 min", flush=True)
        return
    con = _connect()
    con.execute(
        "INSERT INTO mh_whale_positions(symbol,side,qty,entry,ts,entry_signal,peak) "
        "VALUES(?,?,?,?,?,?,?)",
        (symbol, side, qty, px, time.time(), sig_id, px))
    con.commit()
    con.close()
    print(f"[whale_trader] OPEN LONG {symbol} qty={qty:.4f} @ {px:.6f} "
          f"(signal #{sig_id})", flush=True)


def _close(symbol, pos, px, reason):
    side = pos["side"]
    pct = (px - pos["entry"]) / pos["entry"] if side == "LONG" else \
          (pos["entry"] - px) / pos["entry"]
    realized = pos["qty"] * pos["entry"] * pct

    con = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("BEGIN IMMEDIATE")
    if not con.execute("SELECT 1 FROM mh_whale_positions WHERE id=?",(pos["id"],)).fetchone():
        con.close()
        return
    con.execute("CREATE TABLE IF NOT EXISTS mh_consumed_signals (trader TEXT,signal_id INTEGER,PRIMARY KEY(trader,signal_id))")
    con.execute("INSERT OR IGNORE INTO mh_consumed_signals VALUES(?,?)",(TRADER,pos["entry_signal"]))
    con.execute("UPDATE mh_accounts SET equity_usd=equity_usd+? WHERE trader=?",
                (realized, TRADER))
    sig_id = pos["entry_signal"]
    tag = reason
    con.execute(
        "INSERT INTO mh_trades(coin,symbol,setup,side,open_ts,close_ts,"
        "entry_px,exit_px,qty,realized_pct,realized_usd,exit_reason) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (symbol, symbol, SETUP_TAG, side, pos["ts"], time.time(),
         pos["entry"], px, pos["qty"], pct, realized, tag))
    con.execute("DELETE FROM mh_whale_positions WHERE id=?", (pos["id"],))
    con.commit()
    con.close()
    print(f"[whale_trader] CLOSE LONG {symbol} {tag} pct={pct:+.3%} "
          f"usd={realized:+.4f}", flush=True)


if __name__ == "__main__":
    print("[whale_trader] starting", flush=True)
    while True:
        try:
            tick()
        except Exception as e:
            print(f"[whale_trader] error: {e}", flush=True)
        time.sleep(30)