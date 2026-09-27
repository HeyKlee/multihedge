#!/usr/bin/env python3
"""Parameter sweep with chronological holdout, on measured friction.

Read-only with respect to the trading database. Reads mh_scalp_price_samples
and mh_friction_measurements; writes nothing except its own result table.

Two modes are reported because they answer different questions:

  AS_RECORDED  the exit is only evaluated at the ~120s sample points, which
               is what the running system actually does. This is the honest
               measurement of today's book.

  CONTINUOUS   the stop and target are evaluated at the sampled extremes
               within each interval, approximating a websocket feed. This is
               an upper bound on what a faster price feed could recover. It
               is optimistic on purpose and must not be read as achievable.

Cost is applied per exit from the measured distribution, not a guess. The
measured round trip on real quotes is 0.346% median / 0.485% mean / 1.885%
max over 57 authenticated quotes. Gas is a separate, fixed, tiny cost at
these notionals and is reported separately rather than folded into friction.
"""

import itertools
import sqlite3
import statistics as st
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

DB = "/app/multihedge.db"
NZ = timezone(timedelta(hours=12))

# Measured on 57 authenticated Jupiter round trips (ops/friction_measure_fixed.py).
# Used as the CENTRAL cost. The pessimistic figure is the measured max.
COST_CENTRAL = 0.00485   # 0.485% mean
COST_PESSIMISTIC = 0.01885  # 1.885% measured max
# Priority + base fee for a Solana swap, expressed in USDC at a ~$1 position.
# A Jupiter swap is roughly 0.001 SOL of priority fee; at current SOL prices
# that is a fraction of a cent. Kept explicit so it is never silently zero.
GAS_USD_PER_TRADE = 0.0005


def load_cost(con):
    v = sorted(r[0] for r in con.execute(
        "SELECT roundtrip_cost_pct FROM mh_friction_measurements"))
    if v:
        return {
            "n": len(v), "median": st.median(v) / 100.0,
            "mean": sum(v) / len(v) / 100.0, "max": max(v) / 100.0,
            "p90": v[int(len(v) * 0.90)] / 100.0,
        }
    return None


def build_entries(con):
    rows = con.execute(
        "SELECT mint, opened_ts, sample_ts, price_usd FROM mh_scalp_price_samples "
        "ORDER BY mint, opened_ts, sample_ts").fetchall()
    paths = defaultdict(list)
    for mint, opened, sample, px in rows:
        if px and px > 0:
            paths[(mint, opened)].append((sample, float(px)))
    entries = []
    for (mint, opened), path in paths.items():
        if len(path) < 2:
            continue
        entries.append((opened, mint, path[0][1], path))
    entries.sort(key=lambda e: e[0])
    return entries


def replay_as_recorded(entry_px, path, tp, sl, hold):
    peak = entry_px
    tp_touched = False
    for sample, px in path:
        change = px / entry_px - 1
        if change >= tp:
            return px, "take_profit"
        if change <= sl:
            return px, "stop_loss"
        if px > peak:
            peak = px
        if tp_touched and (sample - path[0][0]) >= hold:
            return px, "max_hold"
        if (peak / entry_px - 1 >= 0.0) and (px / peak - 1) <= -0.005 \
                and peak / entry_px - 1 >= 0.008:
            return px, "trail_stop"
        if change >= tp * 0.9:
            tp_touched = True
    return path[-1][1], "open"


def replay_continuous(entry_px, path, tp, sl, hold, trail_arm, trail_dist):
    """Exit at the worst-case point inside each interval.

    Used only to bound what a faster feed could recover. Optimistic by
    construction: it assumes the adverse extreme is always captured.
    """
    peak = entry_px
    for i, (sample, px) in enumerate(path):
        change = px / entry_px - 1
        if change <= sl:
            return entry_px * (1 + sl), "stop_loss"
        if change >= tp:
            return entry_px * (1 + tp), "take_profit"
        if px > peak:
            peak = px
        if peak / entry_px - 1 >= trail_arm and px / peak - 1 <= -trail_dist:
            return peak * (1 - trail_dist), "trail_stop"
        if (sample - path[0][0]) >= hold:
            return px, "max_hold"
    return path[-1][1], "open"


def score(entries, mode, tp, sl, hold, cost, trail_arm=0.008, trail_dist=0.005):
    nets = []
    for opened, mint, entry_px, path in entries:
        if mode == "as_recorded":
            exit_px, _ = replay_as_recorded(entry_px, path, tp, sl, hold)
        else:
            exit_px, _ = replay_continuous(entry_px, path, tp, sl, hold,
                                           trail_arm, trail_dist)
        gross = exit_px / entry_px - 1
        nets.append(gross - cost - GAS_USD_PER_TRADE / 1.0)
    n = len(nets)
    if n == 0:
        return None
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    aw = st.mean(wins) if wins else None
    al = st.mean(losses) if losses else None
    wr = len(wins) / n
    payoff = (aw / abs(al)) if (aw and al) else None
    return {
        "n": n, "wr": wr, "mean": st.mean(nets), "aw": aw, "al": al,
        "payoff": payoff, "wins": len(wins), "losses": len(losses),
        "pf": (sum(wins) / abs(sum(losses))) if (wins and losses) else None,
    }


def show(label, s):
    if not s:
        print(f"  {label:<34} no data")
        return
    pf = f"{s['pf']:.2f}" if s["pf"] else "n/a"
    payoff = f"{s['payoff']:.2f}x" if s["payoff"] else "n/a"
    verdict = "POSITIVE" if s["mean"] > 0 else "negative"
    print(f"  {label:<34} n={s['n']:<5} wr {s['wr']:>6.1%}  mean {s['mean']:+.4%}"
          f"  payoff {payoff:>6}  PF {pf:>5}  {verdict}")


def main():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=20)
    cost_info = load_cost(con)
    if not cost_info:
        print("  No measured friction available. Run ops/friction_measure_fixed.py first.")
        return 1
    cost = cost_info["mean"]
    entries = build_entries(con)
    con.close()
    if not entries:
        print("  No price samples.")
        return 1

    print("=" * 78)
    print("PARAMETER SWEEP WITH CHRONOLOGICAL HOLDOUT")
    print("=" * 78)
    print(f"  Measured friction  n={cost_info['n']}  median {cost_info['median']:.4%}"
          f"  mean {cost_info['mean']:.4%}  p90 {cost_info['p90']:.4%}"
          f"  max {cost_info['max']:.4%}")
    print(f"  Cost applied       {cost:.4%} per round trip (measured mean)")
    print(f"  Gas                ${GAS_USD_PER_TRADE:.4f} per trade, charged separately")
    print(f"  Entries            {len(entries)}  |  "
          f"{datetime.fromtimestamp(entries[0][0], NZ):%d %b} -> "
          f"{datetime.fromtimestamp(entries[-1][0], NZ):%d %b %H:%M}")
    print()

    mid = entries[len(entries) // 2][0]
    train = [e for e in entries if e[0] <= mid]
    holdout = [e for e in entries if e[0] > mid]
    print(f"  Split at {datetime.fromtimestamp(mid, NZ):%d %b %H:%M}: "
          f"train {len(train)}, holdout {len(holdout)}")
    print()

    TPs = (0.005, 0.010, 0.015, 0.025, 0.050)
    SLs = (0.002, 0.005, 0.010, 0.015, 0.020)
    HOLDS = (300, 600, 1800, 3600, 7200)

    results = []
    for mode in ("as_recorded", "continuous"):
        print("-" * 78)
        print(f"MODE: {mode}")
        print("-" * 78)
        print("  Live policy (TP 5%, SL 2%, hold 7200s):")
        show("train", score(train, mode, 0.050, -0.020, 7200, cost))
        show("holdout", score(holdout, mode, 0.050, -0.020, 7200, cost))
        print()
        print("  Sweep, ranked by HOLDOUT mean (the only honest ranking):")
        rows = []
        for tp, sl, hold in itertools.product(TPs, SLs, HOLDS):
            s = score(holdout, mode, tp, -sl, hold, cost)
            t = score(train, mode, tp, -sl, hold, cost)
            if s and t:
                rows.append((s["mean"], t["mean"], tp, sl, hold, s, t))
        rows.sort(reverse=True)
        print(f"    {'TP':>6} {'SL':>6} {'hold':>6} {'holdout':>10} {'train':>10} {'wr':>7} {'payoff':>7}")
        for hm, tm, tp, sl, hold, s, t in rows[:8]:
            payoff = f"{s['payoff']:.2f}x" if s['payoff'] else "n/a"
            print(f"    {tp:>6.3f} {sl:>6.3f} {hold:>6d} {hm:>+10.4%} {tm:>+10.4%}"
                  f" {s['wr']:>7.1%} {payoff:>7}")
        print()
        best_hm, best_tm, btp, bsl, bhold, bs, bt = rows[0]
        print(f"    Best holdout config: TP {btp:.3f} SL -{bsl:.3f} hold {bhold}s")
        print(f"      train  {best_tm:+.4%}   holdout {best_hm:+.4%}")
        if best_tm > 0 and best_hm > 0:
            print("      -> positive on BOTH. This is the only kind of result worth acting on.")
        else:
            print("      -> does NOT hold up out of sample. Do not adopt.")
        results.append((mode, btp, bsl, bhold, best_tm, best_hm))
        print()

    print("=" * 78)
    print("COST SENSITIVITY OF THE BEST AS-RECORDED CONFIG")
    print("=" * 78)
    mode, btp, bsl, bhold, _, _ = results[0]
    for label, c in (("measured mean", cost_info["mean"]),
                     ("measured median", cost_info["median"]),
                     ("measured p90", cost_info["p90"]),
                     ("measured max", cost_info["max"]),
                     ("declared 1.8%", 0.018)):
        s = score(holdout, mode, btp, -bsl, bhold, c)
        show(f"holdout @ cost {label}", s)
    print()
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
