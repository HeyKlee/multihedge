"""
Build a deterministic per-day signal series (UP/DOWN/FLAT + confidence) from
SOL hourly candles, replicating the strategy layer's RSI(2)/momentum logic so
the backtest engine can search exit params without needing stored news history.

Output: JSON {hour_ts_s: {"direction": ..., "confidence": ...}} — one signal
per day (applied from that day onward by the engine).
"""
import json, os, sys, math
from datetime import datetime, timezone, timedelta

def rs2(closes):
    if len(closes) < 3:
        return 50.0
    gains = losses = 0.0
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        if d > 0: gains += d
        else: losses -= d
    if losses == 0:
        return 100.0 if gains > 0 else 50.0
    rs = (gains/len(closes)) / (losses/len(closes))
    return 100.0 - 100.0/(1.0+rs)

def build_signals(rows, out_path, daily=True):
    """
    Build a signal series. For each candle computes an RSI(2) momentum bias;
    confidence scales with how far from neutral (50).
    Returns dict keyed by epoch-second (the candle's own time).
    """
    closes = [float(r[4]) for r in rows]
    signals = {}
    # windows of last 24h hourly closes per candle
    for i in range(len(rows)):
        ts = rows[i][0] / 1000.0
        r = rs2(closes[max(0, i-23): i+1])
        if r <= 30:
            d, conf = "UP", 0.55 + (30 - r) / 30 * 0.35   # oversold -> expect bounce UP
        elif r >= 70:
            d, conf = "DOWN", 0.55 + (r - 70) / 30 * 0.35 # overbought -> expect DOWN
        else:
            d, conf = "FLAT", 0.5
        signals[int(ts)] = {"direction": d, "confidence": round(conf, 3)}
    json.dump(signals, open(out_path, "w"))
    return signals


def build_news_bias_signals(db_path, symbol, out_path, min_days=30):
    """
    Build a daily signal series from PERSISTED mh_news_bias history in the live
    DB (only if enough days exist). Rows are hourly LLM bias verdicts per coin.
    Consolidates to ONE signal per calendar day: the majority direction and the
    mean confidence of that day's verdicts.

    Returns (signal_dict, used_news:bool). If < min_days of history exist,
    returns (None, False) so the caller falls back to the RSI stand-in.
    """
    import sqlite3
    from datetime import datetime, timezone
    signals = {}
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    rows_b = con.execute("SELECT direction, confidence, ts FROM mh_news_bias "
                         "WHERE symbol=? ORDER BY ts ASC", (symbol,)).fetchall()
    con.close()
    if not rows_b:
        return None, False
    # group by local date
    days = {}
    for r in rows_b:
        if r["direction"] not in ("UP", "DOWN", "FLAT"):
            continue
        day = datetime.fromtimestamp(r["ts"]).date()
        days.setdefault(day, []).append((r["direction"], r["confidence"]))
    if len(days) < min_days:
        return None, False
    for day, vers in days.items():
        up = sum(1 for d, c in vers if d == "UP")
        dn = sum(1 for d, c in vers if d == "DOWN")
        fl = sum(1 for d, c in vers if d == "FLAT")
        conf = sum(c for d, c in vers) / max(1, len(vers))
        if up > dn and up >= fl:
            direction = "UP"
        elif dn > up and dn >= fl:
            direction = "DOWN"
        else:
            direction = "FLAT"
        # key by the day at 00:00 UTC
        key = int(datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc).timestamp())
        signals[key] = {"direction": direction, "confidence": round(conf, 3)}
    json.dump(signals, open(out_path, "w"))
    return signals, True

def build_news_bias_signals_from_rows(rows_b, symbol, out_path, min_days=30):
    """Build a daily signal series from a list of dicts {symbol,direction,confidence,ts}.
    Rows are hourly LLM bias verdicts per coin. Consolidates to ONE signal per
    calendar day (majority direction, mean confidence). Returns (signals, used:bool)."""
    if not rows_b:
        return None, False
    from datetime import datetime, timezone
    rows_b = [r for r in rows_b if r.get("symbol") == symbol]
    days = {}
    for r in rows_b:
        if r.get("direction") not in ("UP", "DOWN", "FLAT"):
            continue
        day = datetime.fromtimestamp(float(r["ts"])).date()
        days.setdefault(day, []).append((r["direction"], float(r.get("confidence", 0))))
    if len(days) < min_days:
        return None, False
    signals = {}
    for day, vers in days.items():
        up = sum(1 for d, c in vers if d == "UP")
        dn = sum(1 for d, c in vers if d == "DOWN")
        fl = sum(1 for d, c in vers if d == "FLAT")
        conf = sum(c for d, c in vers) / max(1, len(vers))
        if up > dn and up >= fl:
            direction = "UP"
        elif dn > up and dn >= fl:
            direction = "DOWN"
        else:
            direction = "FLAT"
        key = int(datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc).timestamp())
        signals[key] = {"direction": direction, "confidence": round(conf, 3)}
    json.dump(signals, open(out_path, "w"))
    return signals, True


if __name__ == "__main__":
    rows = json.load(open(sys.argv[1]))
    out = sys.argv[2] if len(sys.argv) > 2 else os.path.expandvars(r"$LOCALAPPDATA/Temp/sol_signals.json")
    s = build_signals(rows, out)
    print("built", len(s), "signals ->", out)
    ups = sum(1 for v in s.values() if v["direction"]=="UP")
    dn = sum(1 for v in s.values() if v["direction"]=="DOWN")
    fl = sum(1 for v in s.values() if v["direction"]=="FLAT")
    print("UP:", ups, "DOWN:", dn, "FLAT:", fl)