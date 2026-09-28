#!/usr/bin/env python3
"""How far do these tokens actually move, and in what time?

This exists to answer one question with data instead of assumption:

    Kelly's goal (2026-09-28): "20% every 30-60 min, working towards that by taking
    1.5% or more every 5 minutes."

For each observation it computes the maximum absolute excursion reachable AFTER that
sample within a given horizon, then reports the distribution. Read-only: opens the ledger
with mode=ro and never writes.

Interpretation notes, stated up front so the numbers are not over-read:
  - an excursion is measured from a *sample*, not from a trade entry, so it is an
    opportunity envelope, not a strategy backtest
  - windows overlap heavily, so the per-window probabilities are NOT independent
  - "reached 1.5% within 5 minutes" means the price touched it at some sample in that
    window, not that a system would have captured it (slippage, latency, exit cadence)

Usage:
    python3 ops/move_distribution.py [--hours N] [--json]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import runtime_paths  # noqa: E402

HORIZONS_MIN = (5, 15, 30, 60)
TARGETS_PCT = (1.5, 5.0, 20.0)


def collect(hours: int | None):
    db = runtime_paths.production_db()
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
    sql = ("SELECT mint, observed_ts, latest_usd FROM mh_shadow_entry_observations "
           "ORDER BY mint, observed_ts")
    if hours:
        sql = ("SELECT mint, observed_ts, latest_usd FROM mh_shadow_entry_observations "
               f"WHERE observed_ts > (SELECT MAX(observed_ts) - {hours * 3600} "
               "FROM mh_shadow_entry_observations) ORDER BY mint, observed_ts")
    rows = con.execute(sql).fetchall()
    con.close()
    per: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for mint, ts, px in rows:
        if px and px > 0:
            per[mint].append((ts, px))
    return per, len(rows)


def excursions(per, horizons=HORIZONS_MIN) -> dict[int, list[float]]:
    out: dict[int, list[float]] = {h: [] for h in horizons}
    for series in per.values():
        n = len(series)
        for i, (t0, p0) in enumerate(series):
            for h in horizons:
                limit = h * 60
                best = 0.0
                for j in range(i + 1, n):
                    tj, pj = series[j]
                    if tj - t0 > limit:
                        break
                    mv = abs(pj / p0 - 1.0)
                    if mv > best:
                        best = mv
                if best > 0:
                    out[h].append(best)
    return out


def summarise(exc: dict[int, list[float]]) -> list[dict]:
    table = []
    for h in HORIZONS_MIN:
        v = sorted(exc.get(h) or [])
        if not v:
            continue
        n = len(v)
        q = lambda f: round(v[min(int(n * f), n - 1)] * 100, 3)
        row = {
            "horizon_min": h,
            "samples": n,
            "median_pct": q(0.50),
            "p75_pct": q(0.75),
            "p90_pct": q(0.90),
            "p99_pct": q(0.99),
        }
        for t in TARGETS_PCT:
            row[f"reach_{t:g}pct"] = round(sum(1 for x in v if x >= t / 100) / n * 100, 2)
        # expected number of touches of 1.5% inside the horizon
        row["expected_1.5pct_touches"] = round(
            n and sum(1 for x in v if x >= 0.015) / n * (h / 5), 2)
        table.append(row)
    return table


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    per, total = collect(args.hours)
    if not per:
        print("no observations available")
        return 1
    table = summarise(excursions(per))

    if args.json:
        print(json.dumps({"mints": len(per), "points": total, "table": table}, indent=2))
        return 0

    print("MOVE DISTRIBUTION (max excursion after each sample)")
    print(f"  mints={len(per)}  price points={total}  ledger={runtime_paths.production_db()}")
    hdr = f"{'horizon':>7} {'samples':>8} {'median':>8} {'p75':>8} {'p90':>8} {'p99':>8}"
    for t in TARGETS_PCT:
        hdr += f" {f'>={t:g}%':>8}"
    hdr += f" {'E[1.5%x]':>9}"
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for r in table:
        line = (f"{r['horizon_min']:>6}m {r['samples']:>8} {r['median_pct']:>7.2f}% "
                f"{r['p75_pct']:>7.2f}% {r['p90_pct']:>7.2f}% {r['p99_pct']:>7.2f}%")
        for t in TARGETS_PCT:
            line += f" {r[f'reach_{t:g}pct']:>7.1f}%"
        line += f" {r['expected_1.5pct_touches']:>9.2f}"
        print(line)
    print()
    print("  E[1.5%x] = expected number of 1.5% touches inside the horizon, assuming the")
    print("             per-window rate repeats. Windows overlap, so this is an upper")
    print("             bound on a compounding '1.5% every 5 min' plan, not a forecast.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
