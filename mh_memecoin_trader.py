"""
mh_memecoin_trader - paper-trader for Solana memecoins.

Guardrails (hard-coded, paper only):
  * Long-only (no SHORTs)
  * Max allocation per trade: $5 (20% of $24 seed)
  * TP: +50%
  * SL: -20%
  * Max hold: 15 minutes
  * Trailing stop: arms at +30%, exits if price drops 10% from peak
  * Only acts on mh_news_bias rows with provider='memecoin_tracker'
  * Per-signal dedupe via open-position existence (one entry per token)
"""
import os
import time
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "multihedge.db"
TRADER = "memecoin_trader"

TP_PCT = 0.50
SL_PCT = -0.20
MAX_HOLD_S = 15 * 60
TRAIL_ARM_PCT = 0.30
TRAIL_DIST_PCT = 0.10
MAX_ALLOCATION_USD = 5.0
POSITION_FRACTION = 0.20

SCHEMA_POS = (
    "CREATE TABLE IF NOT EXISTS mh_memecoin_positions ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "symbol TEXT NOT NULL, side TEXT NOT NULL, "
    "qty REAL NOT NULL, entry REAL NOT NULL, ts REAL NOT NULL, "
    "entry_signal INTEGER, peak REAL)"
)


def _log(msg, lvl="INFO"):
    print(f"[{time.strftime('%H:%M:%S')}] [{lvl}] {msg}", flush=True)


def _connect():
    return sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)


def _ensure_tables():
    con = _connect()
    con.execute(SCHEMA_POS)
    con.commit()
    con.close()


def _ensure_wallet():
    import paper
    paper.ensure_account(TRADER, paper.DEFAULT_EQUITY)


def _available():
    import paper
    return paper.available(TRADER)


def _size_qty(entry_px):
    free = _available()
    budget = min(free * POSITION_FRACTION, MAX_ALLOCATION_USD)
    if budget < 1.0 or entry_px <= 0:
        return 0.0
    return budget / entry_px


def _fetch_signals():
    con = _connect()
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT * FROM mh_news_bias WHERE provider='memecoin_tracker' "
        "ORDER BY id DESC LIMIT 20"
    ).fetchall()
    con.close()
    return rows


def _open_position(symbol, entry_px, sig_id):
    qty = _size_qty(entry_px)
    if qty <= 0:
        _log(f"skip {symbol}: insufficient allocation", "WARN")
        return
    con = _connect()
    exist = con.execute(
        "SELECT 1 FROM mh_memecoin_positions WHERE symbol=?", (symbol,)
    ).fetchone()
    if exist:
        con.close()
        return
    con.execute(
        "INSERT INTO mh_memecoin_positions(symbol,side,qty,entry,ts,entry_signal,peak) "
        "VALUES(?,?,?,?,?,?,?)",
        (symbol, "LONG", qty, entry_px, time.time(), sig_id, entry_px),
    )
    con.commit()
    con.close()
    _log(f"OPEN {symbol} qty={qty:.6f} @ ${entry_px:.6f} (sig #{sig_id})")


def _close_position(pos, exit_px, reason):
    pct = (exit_px - pos["entry"]) / pos["entry"]
    realized = pos["qty"] * pos["entry"] * pct

    con = _connect()
    con.execute("BEGIN IMMEDIATE")
    if not con.execute("SELECT 1 FROM mh_memecoin_positions WHERE id=?",(pos["id"],)).fetchone():
        con.close()
        return
    con.execute("CREATE TABLE IF NOT EXISTS mh_consumed_signals (trader TEXT,signal_id INTEGER,PRIMARY KEY(trader,signal_id))")
    con.execute("INSERT OR IGNORE INTO mh_consumed_signals VALUES(?,?)",(TRADER,pos["entry_signal"]))
    con.execute(
        "UPDATE mh_accounts SET equity_usd=equity_usd+? WHERE trader=?",
        (realized, TRADER),
    )
    sig_id = pos["entry_signal"]
    tag = reason
    con.execute(
        "INSERT INTO mh_trades(coin,symbol,setup,side,open_ts,close_ts,"
        "entry_px,exit_px,qty,realized_pct,realized_usd,exit_reason) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            pos["symbol"], pos["symbol"], "memecoin_trader", "LONG",
            pos["ts"], time.time(),
            pos["entry"], exit_px, pos["qty"], pct, realized, tag,
        ),
    )
    con.execute("DELETE FROM mh_memecoin_positions WHERE id=?", (pos["id"],))
    con.commit()
    con.close()
    _log(f"CLOSE {pos['symbol']} {tag} pct={pct:+.2%} usd={realized:+.4f}")


def _eval_exit(pos, px):
    pct = (px - pos["entry"]) / pos["entry"]
    if pct >= TP_PCT:
        return "take_profit"
    if pct <= SL_PCT:
        return "stop_loss"
    if time.time() - pos["ts"] >= MAX_HOLD_S:
        return "max_hold"
    peak = max(pos["peak"] or pos["entry"], px)
    if (peak / pos["entry"] - 1) >= TRAIL_ARM_PCT and (peak - px) / peak >= TRAIL_DIST_PCT:
        return "trail_stop"
    return None


def _update_peak(pos, px):
    if px > (pos["peak"] or pos["entry"]):
        con = _connect()
        con.execute(
            "UPDATE mh_memecoin_positions SET peak=? WHERE id=?",
            (px, pos["id"]),
        )
        con.commit()
        con.close()


def _price_for(sym):
    """Try to fetch a live price via pricefeed's mint-generic path.
    Returns None if no price source can resolve the symbol."""
    try:
        import pricefeed
        return pricefeed.live_price(None, sym)
    except Exception:
        return None


def _run():
    _ensure_tables()
    _ensure_wallet()
    _log("memecoin trader up")
    while True:
        try:
            con = _connect()
            con.row_factory = sqlite3.Row
            positions = con.execute(
                "SELECT * FROM mh_memecoin_positions"
            ).fetchall()
            con.close()

            for p in positions:
                px = _price_for(p["symbol"])
                if not px:
                    continue
                reason = _eval_exit(p, px)
                if reason:
                    _close_position(p, px, reason)
                else:
                    _update_peak(p, px)

            for sig in _fetch_signals():
                if sig["direction"] != "UP":
                    continue
                sym = sig["symbol"]
                px = _price_for(sym)
                if not px:
                    continue
                _open_position(sym, px, sig["id"])
        except Exception as e:
            _log(f"run error: {e}", "ERROR")
        time.sleep(int(os.getenv("MEME_POLL_INTERVAL", "30")))


if __name__ == "__main__":
    _run()
