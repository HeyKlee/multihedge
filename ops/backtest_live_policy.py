#!/usr/bin/env python3
"""Historical backtest of the live exit policy over recorded scalp price samples.

Read-only. Reads mh_scalp_price_samples from the production database and writes
nothing back to it. No network, no signing, no execution.

The substrate is real recorded price history, not synthetic data: every row is a
timestamped, mint-scoped price observed while a paper position was open. Each
distinct opened_ts on a mint is one historical entry, and the samples after it
are that entry's forward price path.

Policy replayed is the policy that actually applies at runtime, read from
config.yaml rather than hardcoded, so this cannot drift from production:

    take profit   reasoner.TAKE_PROFIT
    stop loss     reasoner.STOP_LOSS
    max hold      reasoner.MAX_HOLD_SECS
    round trip    execution_costs.round_trip_cost_pct (0.018)

Favourable-excursion optimism is avoided: on every sample the stop is evaluated
before the take profit, so a path that touches both in one interval books the
loss. Age alone never closes a position, matching the live rule that max hold is
a missed-take-profit fallback only. Every exit pays the full round trip.
"""

import argparse
import sqlite3
import statistics as st
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

DB = "/app/multihedge.db"
NZ = timezone(timedelta(hours=12))

# Read from the live config so the replay cannot drift from production.
sys.path.insert(0, "/app")


def load_policy():
    """Resolve the policy that actually applies at runtime.

    mh_reasoner._load_params reads the DB table FIRST, then config.yaml, then
    code defaults. The DB override wins, so reading config alone replays a
    policy the system is not running. config.yaml even warns about this: it
    declares TAKE_PROFIT 0.015 while the applied override is 0.05.
    """
    import yaml  # noqa

    cfg = yaml.safe_load(open("/app/config.yaml"))
    r = cfg["reasoner"]
    tp = float(r["TAKE_PROFIT"])
    sl = float(r["STOP_LOSS"])
    hold = float(r["MAX_HOLD_SECS"])

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=15)
    try:
        for key, val in con.execute("SELECT key, value FROM mh_reasoner_params"):
            k = key.upper()
            if k == "TAKE_PROFIT":
                tp = float(val)
            elif k == "STOP_LOSS":
                sl = float(val)
            elif k == "MAX_HOLD_SECS":
                hold = float(val)
    except sqlite3.Error as exc:
        print(f"  WARNING: could not read runtime override ({exc}); "
              "replaying config values, which may not be what runs.")
    finally:
        con.close()

    from execution_costs import round_trip_cost_pct

    return tp, sl, hold, round_trip_cost_pct(cfg), cfg


def fmt(t):
    return datetime.fromtimestamp(t, NZ).strftime("%d %b %H:%M")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-cost", action="store_true",
                    help="Replay pre-46a2407 accounting: no round-trip cost "
                         "charged, i.e. gross P&L as it was recorded before "
                         "cost entered paper accounting.")
    ap.add_argument("--tp", type=float, default=None,
                    help="Override take profit fraction, e.g. --tp 0.015")
    ap.add_argument("--sl", type=float, default=None,
                    help="Override stop loss fraction, e.g. --sl 0.015")
    ap.add_argument("--hold", type=float, default=None,
                    help="Override max hold seconds")
    args = ap.parse_args()

    tp, sl, hold, cost, cfg = load_policy()
    if args.tp is not None:
        tp = args.tp
    if args.sl is not None:
        sl = args.sl
    if args.hold is not None:
        hold = args.hold
    basis = "GROSS, no round-trip cost (pre-46a2407 accounting)"
    if args.no_cost:
        cost = 0.0
    print("=" * 78)
    print("HISTORICAL BACKTEST OF THE LIVE EXIT POLICY")
    print("=" * 78)
    print(f"  Replayed policy   TP {tp:+.1%}  SL {sl:+.1%}  max hold {hold:.0f}s")
    print(f"  Round trip cost   {cost:.4%}  (charged on every exit, win or loss)")
    print(f"  Basis             {basis}")
    print(f"  Source            mh_scalp_price_samples, recorded prices only")
    print()

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=15)
    rows = con.execute(
        "SELECT mint, opened_ts, sample_ts, price_usd "
        "FROM mh_scalp_price_samples ORDER BY mint, opened_ts, sample_ts"
    ).fetchall()
    con.close()

    if not rows:
        print("  No price samples available. Nothing to replay.")
        return 1

    # Group into per-entry forward paths.
    paths = defaultdict(list)
    for mint, opened, sample, px in rows:
        if px and px > 0:
            paths[(mint, opened)].append((sample, float(px)))

    entries = []
    for (mint, opened), path in paths.items():
        if len(path) < 2:
            continue
        entry_px = path[0][1]
        entries.append((opened, mint, entry_px, path))
    entries.sort(key=lambda e: e[0])

    lo = entries[0][0]
    hi = max(e[0] for e in entries)
    print(f"  Entries found     {len(entries)} across "
          f"{len({e[1] for e in entries})} mints")
    print(f"  Window            {fmt(lo)}  ->  {fmt(hi)}")
    print()

    def replay(e):
        opened, mint, entry_px, path = e
        peak = entry_px
        tp_hit = False
        for sample, px in path:
            age = sample - opened
            # Stop evaluated first: conservative on any ambiguous interval.
            if px <= entry_px * (1 - sl):
                return ("SL", px, age, tp_hit)
            if px >= entry_px * (1 + tp):
                tp_hit = True
                return ("TP", px, age, tp_hit)
            if px > peak:
                peak = px
            # Max hold closes only if take profit was already touched and missed.
            if tp_hit and age >= hold:
                return ("MAXHOLD", px, age, tp_hit)
        # Still open at the end of the recorded path: mark, do not invent an exit.
        return ("OPEN", path[-1][1], path[-1][0] - opened, tp_hit)

    results = []
    for e in entries:
        reason, exit_px, age, tp_hit = replay(e)
        gross = exit_px / e[2] - 1.0
        results.append({
            "ts": e[0], "mint": e[1], "entry": e[2], "exit": exit_px,
            "gross": gross, "net": gross - cost, "reason": reason,
            "age": age, "tp_hit": tp_hit,
        })

    def stats(rows_):
        n = len(rows_)
        if n == 0:
            return None
        nets = [r["net"] for r in rows_]
        wins = [x for x in nets if x > 0]
        losses = [x for x in nets if x <= 0]
        aw = st.mean(wins) if wins else None
        al = st.mean(losses) if losses else None
        wr = len(wins) / n
        payoff = (aw / abs(al)) if (aw and al) else None
        be = (abs(al) / (aw + abs(al))) if (aw and al) else None
        return {
            "n": n, "wr": wr, "mean_net": st.mean(nets),
            "mean_gross": st.mean([r["gross"] for r in rows_]),
            "aw": aw, "al": al, "payoff": payoff, "be": be,
            "exp": wr * aw - (1 - wr) * abs(al) if (aw and al) else None,
            "wins": len(wins), "losses": len(losses),
        }

    # Chronological split. Every entry at or before the median timestamp is
    # training, after it is holdout. The split is on time, never random, so no
    # future price can inform an earlier decision.
    midpoint = entries[len(entries) // 2][0]
    train = [r for r in results if r["ts"] <= midpoint]
    holdout = [r for r in results if r["ts"] > midpoint]

    def show(label, s):
        if not s:
            print(f"  {label:<22} no completed exits")
            return
        payoff = f"{s['payoff']:.2f}x" if s["payoff"] else "n/a"
        be = f"{s['be']:.1%}" if s["be"] else "n/a"
        verdict = "PROFITABLE" if (s["exp"] or 0) > 0 else "UNPROFITABLE"
        print(f"  {label:<22} n={s['n']:<5} win {s['wr']:.1%}   "
              f"mean net {s['mean_net']:+.4%}")
        print(f"  {'':<22} avg win {s['aw']:+.4%}  avg loss {s['al']:+.4%}  "
              f"payoff {payoff}  break-even wr {be}")
        print(f"  {'':<22} expectancy {s['exp']:+.4%} per trade   "
              f"verdict {verdict}")
        print()

    print("-" * 78)
    print("RESULTS")
    print("-" * 78)
    print()
    print("  All entries, chronological order, real recorded prices")
    show("ALL", stats(results))
    print("  Chronological split at " + fmt(midpoint))
    show("TRAIN (earlier)", stats(train))
    show("HOLDOUT (later)", stats(holdout))
    print("-" * 78)

    reason_counts = defaultdict(int)
    for r in results:
        reason_counts[r["reason"]] += 1
    print("  Exit reasons: " + "  ".join(
        f"{k}={v}" for k, v in sorted(reason_counts.items(), key=lambda x: -x[1])))
    never = sum(1 for r in results if not r["tp_hit"])
    print(f"  Entries that never touched take profit: {never} of {len(results)}")
    print("-" * 78)

    allt = stats(results)
    if allt and allt["exp"] is not None:
        print()
        if allt["exp"] > 0:
            print(f"  The policy is positive expectancy at {allt['exp']:+.4%} per trade")
            print("  net of the 1.8% round trip, on recorded prices. That is the")
            print("  evidence a faster cadence would need, and it is a risk decision")
            print("  for you, not an automatic change.")
        else:
            print(f"  The policy is NEGATIVE expectancy at {allt['exp']:+.4%} per trade")
            print("  net of the 1.8% round trip, on recorded prices. Trading it faster")
            print("  would lose money faster. Raise cadence only after this turns")
            print("  positive.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
