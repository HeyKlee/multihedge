#!/usr/bin/env python3
"""Chronological hold-time replay for XORA-SURVIVAL dynamic_scalper.

Read-only. Opens the ledger with mode=ro and writes nothing to it.

WHY THIS EXISTS
---------------
The closed-trade cohort shows mean GROSS edge of +0.765% for trades held
under 30 minutes and -1.759% for trades held past an hour. That split is
computed on realised trades, so it is already subject to whatever the exit
logic decided. This harness re-evaluates the SAME entries under different
exit rules using only the price path that followed each entry, so the
comparison isolates the exit rule from the entry.

TEMPORAL INTEGRITY (non-negotiable)
-----------------------------------
  * Price samples are only ever read at or after sample_ts >= open_ts.
  * The holdout is chronological: train on the earliest trades, test on the
    later ones, never shuffled and never reversed.
  * No sample from a different mint is ever used to fill a gap.
  * Costs are applied at the CONSERVATIVE rate, not the measured one. The
    council review found the stored route observations carry a synthetic sell
    leg, so they cannot price a round trip. See docs/atlas/FINDINGS.
"""

import argparse
import json
import sqlite3
import statistics as st
import sys
import time
from bisect import bisect_left
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Conservative round trip. execution_costs.round_trip_cost_pct() resolves to
# 2 * (quote_bps + slippage_bps) = 2 * (40 + 50) bps = 1.800%.
COST_FRACTION = 0.0180

DAY = 86400.0


def load_prices(con):
    """Return {mint: ([timestamps], [prices])} sorted ascending."""
    per = defaultdict(lambda: ([], []))
    for mint, ts, px in con.execute(
        "SELECT mint, sample_ts, price_usd FROM mh_scalp_price_samples "
        "WHERE price_usd > 0 ORDER BY mint, sample_ts"
    ):
        ts_l, px_l = per[mint]
        ts_l.append(float(ts))
        px_l.append(float(px))
    return per


def load_trades(con, after_ts=None, before_ts=None):
    # mh_trades names the mint column `coin`; mh_scalp_price_samples names it
    # `mint`. Both hold the canonical Solana mint address, which is the only
    # valid join key. Never join on symbol or ticker.
    q = ("SELECT id, coin, side, open_ts, close_ts, entry_px, exit_px, exit_reason "
         "FROM mh_trades WHERE setup='dynamic_scalper' "
         "AND entry_px > 0 AND exit_px > 0 AND close_ts > open_ts")
    args = []
    if after_ts is not None:
        q += " AND open_ts >= ?"
        args.append(after_ts)
    if before_ts is not None:
        q += " AND open_ts < ?"
        args.append(before_ts)
    q += " ORDER BY open_ts"
    return con.execute(q, args).fetchall()


def path_after(prices, mint, open_ts, horizon_s):
    """Samples for `mint` with open_ts <= ts <= open_ts + horizon_s.

    Never crosses mints and never looks backwards past the entry.
    """
    entry = prices.get(mint)
    if not entry:
        return []
    ts_l, px_l = entry
    i = bisect_left(ts_l, open_ts)
    if i >= len(ts_l):
        return []
    out = []
    limit = open_ts + horizon_s
    j = i
    while j < len(ts_l) and ts_l[j] <= limit:
        out.append((ts_l[j], px_l[j]))
        j += 1
    return out


def simulate(path, entry_px, side, tp, sl, max_hold_s, cost=COST_FRACTION,
             trail_arm=None, trail_dist=None):
    """Apply an exit rule to a realised price path.

    tp / sl are POSITIVE fractions (e.g. tp=0.015, sl=0.01). sl is stored
    negative in policy.py; the caller is responsible for the sign. This keeps
    one convention at the boundary and avoids the negation that previously
    inverted the stop band in mh_reasoner.

    trail_arm / trail_dist reproduce the live trailing stop, which
    mh_reasoner.closing_reason applies as: once the running peak has gained
    trail_arm, exit when price gives back trail_dist FROM THE PEAK (not from
    entry). Passing None disables the trail entirely.

    The trail is checked AFTER take_profit and stop_loss, matching live order,
    and the peak is updated before the trail test so the running maximum is
    never a stale value.
    """
    if not path or entry_px <= 0:
        return None
    peak = entry_px
    # Consume the WHOLE available path. If the price samples run out before
    # any exit triggers, the position is marked to the last observed price.
    # Returning None here silently DROPPED the trade, which removed drift
    # trades that never hit a band from the sample and inflated the mean --
    # and because longer max_hold windows exhaust the path more often, it
    # made long holds look better for a purely mechanical reason.
    for ts, px in path:
        held = ts - path[0][0]
        if side == "SHORT":
            move = (entry_px - px) / entry_px
        else:
            move = (px - entry_px) / entry_px
        if move >= tp:
            return ("take_profit", move - cost, held)
        if move <= -sl:
            return ("stop_loss", move - cost, held)
        # Track the running peak before testing the trail, so the giveback is
        # measured from the true maximum and not a stale one.
        if side == "LONG":
            if px > peak:
                peak = px
        else:
            if px < peak:
                peak = px
        if trail_arm is not None and trail_dist is not None:
            peak_gain = ((peak - entry_px) / entry_px) if side == "LONG" \
                else ((entry_px - peak) / entry_px)
            if peak_gain >= trail_arm:
                giveback = ((peak - px) / peak) if side == "LONG" \
                    else ((px - peak) / peak)
                if giveback >= trail_dist:
                    return ("trail_stop", move - cost, held)
        if held >= max_hold_s:
            return ("max_hold", move - cost, held)
    # Path exhausted with no exit triggering. Mark to the last observed price
    # rather than dropping the trade, so the sample is identical for every
    # rule and no rule wins by having the most silent omissions.
    ts, px = path[-1]
    held = ts - path[0][0]
    if side == "SHORT":
        move = (entry_px - px) / entry_px
    else:
        move = (px - entry_px) / entry_px
    return ("path_exhausted", move - cost, held)


def evaluate(trades, prices, tp, sl, max_hold_s, horizon_mult=3.0,
             trail_arm=None, trail_dist=None):
    """Run one exit rule over a trade set. Returns aggregate + per-trade."""
    horizon = max_hold_s * horizon_mult
    rows = []
    for _id, mint, side, open_ts, close_ts, ep, xp, reason in trades:
        path = path_after(prices, mint, open_ts, horizon)
        if not path:
            continue
        # Anchor the path at the recorded entry so the first sample is entry_px.
        path = [(open_ts, ep)] + path
        r = simulate(path, ep, side, tp, sl, max_hold_s,
                     trail_arm=trail_arm, trail_dist=trail_dist)
        if r is None:
            continue
        why, net, held = r
        rows.append({
            "id": _id, "mint": mint, "gross": net + COST_FRACTION, "net": net,
            "held_s": held, "reason": why, "live_reason": reason,
        })
    if not rows:
        return None
    nets = [r["net"] for r in rows]
    gross = [r["gross"] for r in rows]
    wins = len([n for n in nets if n > 0])
    aw = [n for n in nets if n > 0]
    al = [n for n in nets if n <= 0]
    payoff = (st.mean(aw) / abs(st.mean(al))) if al and aw else None
    return {
        "n": len(rows),
        "win_rate": wins / len(rows),
        "mean_gross": st.mean(gross),
        "mean_net": st.mean(nets),
        "median_net": st.median(nets),
        "payoff": payoff,
        "rows": rows,
    }


def split_chronological(trades, frac=0.6):
    """Earliest `frac` trains, remainder tests. Order is never shuffled."""
    n = len(trades)
    cut = int(n * frac)
    boundary = trades[cut][3] if cut < n else None
    return trades[:cut], trades[cut:], boundary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None)
    ap.add_argument("--tp", type=float, default=0.015)
    ap.add_argument("--sl", type=float, default=0.01)
    ap.add_argument("--max-hold", type=int, default=1800)
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--no-trail", action="store_true",
                    help="disable the trailing stop (for A/B against the live rule)")
    ap.add_argument("--trail-sweep", action="store_true",
                    help="sweep trail_arm x trail_distance instead of tp/sl/hold")
    ap.add_argument("--out", default=str(ROOT / "ops" / "hold_time_replay_result.json"))
    ap.add_argument("--trail-arm", type=float, default=0.005)
    ap.add_argument("--trail-dist", type=float, default=0.003)
    a = ap.parse_args()

    db = a.db
    if db is None:
        import runtime_paths
        db = str(runtime_paths.evidence_db())
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
    prices = load_prices(con)
    trades = load_trades(con)
    if not trades:
        print("no dynamic_scalper trades with prices", file=sys.stderr)
        return 2

    print(f"ledger     : {db}")
    print(f"mints      : {len(prices)}   samples: {sum(len(v[0]) for v in prices.values())}")
    print(f"trades     : {len(trades)}  {time.strftime('%Y-%m-%d', time.localtime(trades[0][3]))} .. "
          f"{time.strftime('%Y-%m-%d', time.localtime(trades[-1][3]))}")
    print(f"cost       : {COST_FRACTION*100:.3f}% round trip (conservative)")
    print()

    train, test, cut = split_chronological(trades, 0.6)
    print("=" * 78)
    print("CHRONOLOGICAL SPLIT (train = earliest 60%, test = latest 40%)")
    print("=" * 78)
    if cut:
        print(f"  train: {len(train)} trades, up to {time.strftime('%Y-%m-%d %H:%M', time.localtime(cut))}")
        print(f"  test : {len(test)} trades, from {time.strftime('%Y-%m-%d %H:%M', time.localtime(cut))}")
    print()

    arm = None if a.no_trail else a.trail_arm
    dist = None if a.no_trail else a.trail_dist
    print(f"trail      : {'DISABLED' if a.no_trail else f'arm={arm:.3%} dist={dist:.3%}'}")
    print()

    results = {"trail_arm": arm, "trail_dist": dist}
    if a.sweep:
        print("=" * 78)
        print("EXIT-RULE SWEEP  (tp, sl, max_hold)   gross / net at 1.800% cost")
        print("=" * 78)
        print(f"  {'tp':>7} {'sl':>7} {'hold':>7} | {'TRAIN n':>8} {'gross':>9} {'net':>9} | "
              f"{'TEST n':>7} {'gross':>9} {'net':>9}")
        print("  " + "-" * 92)
        grid = []
        for tp in (0.005, 0.010, 0.015, 0.020, 0.030):
            for sl in (0.005, 0.010, 0.015, 0.025):
                for hold in (300, 600, 900, 1200, 1800, 2700, 3600):
                    tr = evaluate(train, prices, tp, sl, hold,
                                  trail_arm=arm, trail_dist=dist)
                    te = evaluate(test, prices, tp, sl, hold,
                                  trail_arm=arm, trail_dist=dist)
                    if not tr or not te:
                        continue
                    grid.append((tp, sl, hold, tr, te))
                    print(f"  {tp*100:6.1f}% {sl*100:6.1f}% {hold:6d}s | "
                          f"{tr['n']:8d} {tr['mean_gross']*100:+8.3f}% {tr['mean_net']*100:+8.3f}% | "
                          f"{te['n']:7d} {te['mean_gross']*100:+8.3f}% {te['mean_net']*100:+8.3f}%")
        results["sweep"] = [
            {"tp": g[0], "sl": g[1], "hold": g[2],
             "train_n": g[3]["n"], "train_net": g[3]["mean_net"],
             "test_n": g[4]["n"], "test_net": g[4]["mean_net"],
             "test_gross": g[4]["mean_gross"]}
            for g in grid
        ]
        print()
        ranked = sorted(results["sweep"], key=lambda r: -r["test_net"])
        print("  TOP 10 BY OUT-OF-SAMPLE NET:")
        for r in ranked[:10]:
            print(f"    tp={r['tp']*100:4.1f}% sl={r['sl']*100:4.1f}% hold={r['hold']:5d}s  "
                  f"test_net {r['test_net']*100:+.3f}%  test_gross {r['test_gross']*100:+.3f}%  n={r['test_n']}")
        print()
        print("  NOTE: ranked on TEST to show out-of-sample reality. A rule that only")
        print("  wins in TRAIN is overfit and must not be promoted.")

    cur = evaluate(trades, prices, a.tp, a.sl, a.max_hold,
                   trail_arm=arm, trail_dist=dist)
    if cur:
        print("=" * 78)
        print(f"CURRENT RULE REPLAY  tp={a.tp*100:.1f}% sl={a.sl*100:.1f}% hold={a.max_hold}s")
        print("=" * 78)
        print(f"  n={cur['n']}  win {cur['win_rate']*100:.1f}%  "
              f"gross {cur['mean_gross']*100:+.3f}%  net {cur['mean_net']*100:+.3f}%  "
              f"payoff {cur['payoff']:.2f}x" if cur["payoff"] else "")
        results["current"] = {k: v for k, v in cur.items() if k != "rows"}

    Path(a.out).write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(f"\nwrote {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
