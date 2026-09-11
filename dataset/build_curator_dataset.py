#!/usr/bin/env python3
"""Build a labeled fine-tune dataset for a MultiHedge entry/TP/SL/exit curator.

Source: the deployed ledger, /home/kelly/multihedge/deploy/data/multihedge.db
Signal used: 491 realized trades + per-coin price history.
Agent decision log is degenerate (mostly 'hold', 0.5 conf) and not used as signal.

Each output record is one raw feature/label row (the label = retrospective
curator decision derived from the realized price path). Separate stage emits the
instruction/completion pairs for SFT.
"""
import sqlite3, json, math, statistics, os

DB = "/home/kelly/multihedge/deploy/data/multihedge.db"
OUT_DIR = "/home/kelly/multihedge/dataset"
os.makedirs(OUT_DIR, exist_ok=True)

con = sqlite3.connect(DB)
cur = con.cursor()

# --- load realized trades ---
cur.execute("SELECT id, coin, symbol, setup, side, open_ts, close_ts, entry_px, "
            "exit_px, qty, realized_pct, realized_usd, exit_reason FROM mh_trades "
            "ORDER BY open_ts")
trades = cur.fetchall()

# --- load price history, index per coin as sorted (ts, px) list ---
cur.execute("SELECT coin, ts, px FROM mh_pxhist ORDER BY coin, ts")
pxhist = {}
for coin, ts, px in cur.fetchall():
    pxhist.setdefault(coin, []).append((ts, px))
con.close()

def idx_le(arr, ts):
    """largest index i such that arr[i][0] <= ts (arr sorted ascending)."""
    lo, hi = 0, len(arr) - 1
    ans = -1
    while lo <= hi:
        mid = (lo + hi) // 2
        if arr[mid][0] <= ts:
            ans = mid; lo = mid + 1
        else:
            hi = mid - 1
    return ans

def window(arr, a, b):
    """(ts, px) pairs in [a, b]."""
    i = idx_le(arr, a)
    if i < 0: i = 0
    out = []
    for k in range(i, len(arr)):
        if arr[k][0] > b: break
        out.append(arr[k])
    return out

def rsi(prices, period=14):
    if len(prices) < period + 1: return None
    gains, losses = [], []
    for i in range(len(prices) - period, len(prices)):
        d = prices[i] - prices[i-1]
        gains.append(max(d, 0.0)); losses.append(max(-d, 0.0))
    ag = sum(gains) / period; al = sum(losses) / period
    if al == 0: return 100.0 if ag > 0 else 50.0
    rs = ag / al
    return 100.0 - 100.0 / (1.0 + rs)

records = []
for (tid, coin, symbol, setup, side, open_ts, close_ts, entry_px, exit_px,
     qty, rpct, rusd, xreason) in trades:
    if coin not in pxhist or not pxhist[coin]:
        continue
    arr = pxhist[coin]
    i_open = idx_le(arr, open_ts)
    if i_open < 0:
        continue
    # ---- INPUT FEATURES: only pre-open / at-open info (no forward leak) ----
    lookback = window(arr, open_ts - 3600.0, open_ts)  # 1h prior window
    if len(lookback) < 20:
        # widen lookback if sparse
        lookback = window(arr, open_ts - 6 * 3600.0, open_ts)
    lb_prices = [p for _, p in lookback]
    if len(lb_prices) < 5:
        continue
    ref_px = lb_prices[-1]
    sma5 = sum(lb_prices[-5:]) / 5.0
    sma10 = sum(lb_prices[-10:]) / min(10, len(lb_prices))
    dev5 = (ref_px - sma5) / sma5 if sma5 else 0.0
    dev10 = (ref_px - sma10) / sma10 if sma10 else 0.0
    rets = [(lb_prices[i] - lb_prices[i-1]) / lb_prices[i-1] for i in range(1, len(lb_prices))]
    vol = statistics.pstdev(rets) if len(rets) > 1 else 0.0  # intra-window vol proxy
    r = rsi(lb_prices, 14)
    momentum1h = (ref_px - lb_prices[0]) / lb_prices[0] if lb_prices[0] else 0.0

    # ---- RETROSPECTIVE LABEL (forward info, for training target only) ----
    fwd = window(arr, open_ts, close_ts + 2.0)
    fwd_px = [p for _, p in fwd]
    if not fwd_px:
        continue
    if side == "LONG":
        mfe = (max(fwd_px) - entry_px) / entry_px   # best favorable excursion
        mae = (min(fwd_px) - entry_px) / entry_px   # worst adverse excursion
    else:
        mfe = (entry_px - min(fwd_px)) / entry_px
        mae = (entry_px - max(fwd_px)) / entry_px

    hold_secs = close_ts - open_ts

    # curator guidance derived from realized outcome
    if rpct > 0:
        action = "hold_to_exit" if xreason == "take_profit" else "trail_exit"
    else:
        action = "cut_loss"

    records.append({
        "trade_id": tid,
        "coin": coin,
        "setup": setup,
        "side": side,
        "entry_px": round(entry_px, 6),
        "exit_px": round(exit_px, 6),
        "realized_pct": round(rpct, 6),
        "realized_usd": round(rusd, 6),
        "exit_reason": xreason,
        "hold_secs": int(hold_secs),
        # features (input)
        "feat_dev5": round(dev5, 6),
        "feat_dev10": round(dev10, 6),
        "feat_vol": round(vol, 8),
        "feat_rsi": round(r, 4) if r is not None else None,
        "feat_mom1h": round(momentum1h, 6),
        "feat_ref_px": round(ref_px, 6),
        # retrospective labels (target)
        "label_mfe": round(mfe, 6),
        "label_mae": round(mae, 6),
        "label_action": action,
    })

records.sort(key=lambda x: x["trade_id"])
print(f"trades with usable px: {len(records)} / {len(trades)}")

out_raw = os.path.join(OUT_DIR, "curator_raw.jsonl")
with open(out_raw, "w") as f:
    for rec in records:
        f.write(json.dumps(rec) + "\n")
print(f"wrote {out_raw}")

# quick stats
wins = [r for r in records if r["realized_pct"] > 0]
losses = [r for r in records if r["realized_pct"] < 0]
print(f"wins={len(wins)} losses={len(losses)} flat={len(records)-len(wins)-len(losses)}")
print(f"median hold_secs={statistics.median(r['hold_secs'] for r in records)}")
print(f"median MFE={round(statistics.median(r['label_mfe'] for r in records)*100,3)}%  "
      f"median MAE={round(statistics.median(r['label_mae'] for r in records)*100,3)}%")