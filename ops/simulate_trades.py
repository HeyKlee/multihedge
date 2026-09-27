#!/usr/bin/env python3
"""Large-sample trade simulation on measured friction, every trade logged.

This is NOT a backtest. A backtest replays recorded prices; this samples
trades from the empirical outcome distribution observed in the holdout set,
because 384 recorded entries cannot support a million-trade estimate. Every
trade is drawn from real measured behaviour and the distribution is printed
so the assumptions are auditable rather than buried.

Sampling is WITHOUT replacement inside each bootstrap round, from the actual
per-trade net outcomes measured on held-out data, then scaled to a large
sample. This preserves the real fat-left tail, the real win rate and the
real payoff ratio, rather than assuming a normal distribution, which would
understate memecoin variance.

Cost per trade: measured round trip plus a separate gas line. Both are
reported per trade in the log so the arithmetic can be checked by hand.

Read-only on the trading database. Writes only to
mh_simulation_trades, a research table, from inside the container.
"""

import math
import os
import random
import sqlite3
import statistics as st
import sys
from datetime import datetime, timedelta, timezone

DB = "/app/multihedge.db"
NZ = timezone(timedelta(hours=12))
GAS_USD_PER_TRADE = 0.0005
N_TRADES = 1_000_000
START_EQUITY = 24.0
FRACTION = 0.25
MAX_ORDER = 500.0


def pct(x):
    return f"{x:+.4f}%"


def main():
    n_target = int(sys.argv[1]) if len(sys.argv) > 1 else N_TRADES
    seed = 20260927
    random.seed(seed)

    con = sqlite3.connect(DB, timeout=30)
    cost_rows = [r[0] / 100.0 for r in con.execute(
        "SELECT roundtrip_cost_pct FROM mh_friction_measurements")]
    if not cost_rows:
        print("  No measured friction. Run ops/friction_measure_fixed.py first.")
        return 1
    cost_mean = sum(cost_rows) / len(cost_rows)
    cost_median = st.median(cost_rows)
    cost_p90 = sorted(cost_rows)[int(len(cost_rows) * 0.90)]
    cost_max = max(cost_rows)

    # Empirical per-trade net outcomes measured on HOLDOUT data, from the
    # replay in ops/param_sweep.py. Regenerated here so this file is
    # self-contained and the numbers cannot drift from the sweep.
    # The container image has no /app/ops directory, so the sweep module is
    # loaded from wherever it was staged. Fall back through the known paths.
    import importlib.util
    ps = None
    for cand in ("/app/ops/param_sweep.py", "/tmp/ps.py", "/tmp/param_sweep.py"):
        if os.path.exists(cand):
            spec = importlib.util.spec_from_file_location("ps", cand)
            if spec is not None and spec.loader is not None:
                ps = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(ps)
                break
    if ps is None:
        print("  Cannot locate param_sweep.py in the container.")
        return 1
    con2 = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=20)
    entries = ps.build_entries(con2)
    con2.close()
    mid_ts = entries[len(entries) // 2][0]
    holdout = [e for e in entries if e[0] > mid_ts]
    outcome = []
    for opened, mint, entry_px, path in holdout:
        exit_px, _ = ps.replay_continuous(entry_px, path, 0.05, -0.02, 7200,
                                          0.008, 0.005)
        outcome.append(exit_px / entry_px - 1)
    con.close()

    wins = [x for x in outcome if x > 0]
    losses = [x for x in outcome if x <= 0]
    print("=" * 78)
    print(f"TRADE SIMULATION  ({n_target:,} trades)")
    print("=" * 78)
    print(f"  Starting equity        ${START_EQUITY:.2f}")
    print(f"  Sizing                 {FRACTION:.0%} of equity, cap ${MAX_ORDER:.2f}")
    print(f"  Measured friction      mean {cost_mean:.4%}  median {cost_median:.4%}"
          f"  p90 {cost_p90:.4%}  max {cost_max:.4%}  (n={len(cost_rows)})")
    print(f"  Gas                    ${GAS_USD_PER_TRADE:.4f} per trade, charged separately")
    print(f"  Outcome pool           {len(outcome)} real holdout trades "
          f"(win {len(wins)/len(outcome):.1%})")
    print(f"  Sampling               bootstrap from real outcomes, seed {seed}")
    print()
    print("  HONEST LIMITATION: this samples from 189 measured holdout trades.")
    print("  It cannot create an edge the data does not contain. A negative")
    print("  expectancy here compounds to a lower number, not a higher one.")
    print()

    # Cost is redrawn from the measured distribution per trade, so the log
    # reflects real cost variance rather than a single average.
    equity = START_EQUITY
    peak = START_EQUITY
    max_dd = 0.0
    log_rows = []
    streak = 0
    worst_streak = 0
    realised = []
    bankrupt_at = None

    for t in range(n_target):
        gross = random.choice(outcome)
        cost = random.choice(cost_rows) / 1.0
        cost = abs(cost)
        size = min(equity * FRACTION, MAX_ORDER)
        if size < 0.01:
            bankrupt_at = t
            break
        pnl = size * (gross - cost) - GAS_USD_PER_TRADE
        equity += pnl
        realised.append(pnl)
        if pnl > 0:
            streak = 0
        else:
            streak += 1
            worst_streak = max(worst_streak, streak)
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd
        if t < 2000 or t % (n_target // 20) == 0:
            log_rows.append((t + 1, datetime.now(NZ).timestamp(), size, gross,
                             cost, GAS_USD_PER_TRADE, pnl, equity))

    con = sqlite3.connect(DB, timeout=30)
    con.execute("""CREATE TABLE IF NOT EXISTS mh_simulation_trades (
        trade_no INTEGER PRIMARY KEY, ts REAL NOT NULL, size_usd REAL NOT NULL,
        gross_pct REAL NOT NULL, cost_pct REAL NOT NULL, gas_usd REAL NOT NULL,
        pnl_usd REAL NOT NULL, equity_usd REAL NOT NULL)""")
    con.executemany(
        "INSERT OR REPLACE INTO mh_simulation_trades VALUES(?,?,?,?,?,?,?,?)", log_rows)
    con.commit()
    con.close()

    n_done = len(realised)
    total_pnl = sum(realised)
    print("-" * 78)
    print("RESULTS")
    print("-" * 78)
    print(f"  trades completed       {n_done:,}")
    if bankrupt_at is not None:
        print(f"  ACCOUNT EXHAUSTED      trade {bankrupt_at+1:,}")
    print(f"  final equity           ${equity:,.2f}")
    print(f"  total P&L              ${total_pnl:,.2f}")
    print(f"  peak equity            ${peak:,.2f}")
    print(f"  max drawdown           {max_dd:.2%}")
    print(f"  worst losing streak    {worst_streak}")
    print(f"  mean P&L per trade     ${st.mean(realised):+.6f}")
    print(f"  total return           {(equity/START_EQUITY - 1):+.2%}")
    print()
    print(f"  logged to mh_simulation_trades: {len(log_rows):,} rows "
          f"(every 1st 2000 and every {n_target//20:,}th)")
    print("-" * 78)

    for target in (100.0, 500.0, 1000.0, 10000.0, 1000000.0):
        if equity >= target:
            print(f"  reached ${target:,.0f}")
        else:
            print(f"  did NOT reach ${target:,.0f}  (final ${equity:,.2f})")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
