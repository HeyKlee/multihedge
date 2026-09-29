"""Read-only: would a wider trail help? Uses recorded peak/trough excursions.

mh_scalp_excursions stores the actual max favourable/adverse excursion for each
closed shadow trade, so we can ask what a different trail arm/distance WOULD
have captured -- a counterfactual on already-recorded path data, not a forecast.

This is diagnostic only. It does not prove a policy is adoptable; that still
requires the chronological holdout validation the constitution demands.
"""
import sqlite3
from collections import defaultdict

# ATLAS Rule A: resolve through the authority; the old literal is a stale stub.
import sys as _sys, pathlib as _pl
_sys.path.insert(0, '/home/kelly/multihedge')
import runtime_paths
DB = f'file:{runtime_paths.production_db()}?mode=ro'
COST = 0.018

con = sqlite3.connect(DB, uri=True)
con.row_factory = sqlite3.Row

try:
    ex = [dict(r) for r in con.execute(
        "SELECT entry_usd, peak_usd, trough_usd, hold_seconds, realized_pct, "
        "exit_reason, mode FROM mh_scalp_excursions WHERE entry_usd > 0")]
except sqlite3.OperationalError as e:
    print("no excursion table:", e)
    raise SystemExit

print(f"excursion rows: {len(ex)}")
if not ex:
    raise SystemExit

for r in ex:
    r['peak_pct'] = r['peak_usd'] / r['entry_usd'] - 1
    r['trough_pct'] = r['trough_usd'] / r['entry_usd'] - 1

# Does the favourable excursion clear a wider TP often enough to matter?
print("\n=== would a HIGHER take-profit have been hit? ===")
print(f"{'TP':>7}{'hit%':>8}{'avg_peak_when_hit':>20}{'net_if_hit':>12}")
for tp in (0.025, 0.05, 0.10, 0.15, 0.20):
    hit = [r for r in ex if r['peak_pct'] >= tp]
    pct = len(hit) / len(ex) * 100
    avg_peak = sum(r['peak_pct'] for r in hit) / len(hit) * 100 if hit else 0
    print(f"{tp*100:>6.0f}%{pct:>7.1f}%{avg_peak:>19.2f}%{(tp-COST)*100:>11.1f}%")

# Critical: how often is the peak real or fleeting?
# If peak was tiny and the trade died anyway, a higher TP is unreachable.
print("\n=== peak distribution (is a higher TP even reachable?) ===")
peaks = sorted(r['peak_pct'] for r in ex)
n = len(peaks)
for q, lbl in ((0.25,'p25'), (0.5,'median'), (0.75,'p75'), (0.9,'p90'), (0.99,'p99')):
    print(f"  {lbl:<7} {peaks[int(n*q)]*100:>7.2f}%")

# Would a tighter stop have saved money, or just cut winners early?
print("\n=== would a TIGHTER stop have helped? ===")
for sl in (0.010, 0.015, 0.025, 0.040):
    hit = [r for r in ex if r['trough_pct'] <= -sl]
    pct = len(hit) / len(ex) * 100
    # of those stopped, how many had already been meaningfully up?
    had_run = sum(1 for r in hit if r['peak_pct'] > COST)
    print(f"  SL -{sl*100:<4.0f}%  would stop {pct:>5.1f}%  "
          f"of which {had_run/max(len(hit),1)*100:>5.1f}% were already net-positive")

# THE KEY QUESTION: is there any edge at all after cost?
# Sum best-case: if every trade hit its peak (perfect foresight) vs actual.
print("\n=== upper bound: perfect-exit vs actual (foresight bound, NOT a forecast) ===")
perfect = sum(r['peak_pct'] - COST for r in ex)
actual = sum((r['realized_pct'] if r['realized_pct'] is not None else 0) for r in ex)
print(f"  actual realised (pct sum)     {actual*100:+.1f}%")
print(f"  perfect exit at peak (pct sum) {perfect*100:+.1f}%")
print(f"  => edge is bounded but tiny; cost is {COST*100:.1f}% per trade "
      f"x {len(ex)} trades = {COST*len(ex)*100:.0f}% of notional")

# net winners if we only took trades whose peak was decent
print("\n=== selectivity test: trades whose peak cleared +2.5% gross ===")
good = [r for r in ex if r['peak_pct'] >= 0.025]
print(f"  {len(good)} of {len(ex)} trades ({len(good)/len(ex)*100:.1f}%)")
if good:
    print(f"  their avg NET if exited at TP: "
          f"{sum(r['peak_pct']-COST for r in good)/len(good)*100:+.3f}%")
    print(f"  their avg ACTUAL net:          "
          f"{sum((r['realized_pct'] or 0)-COST for r in good)/len(good)*100:+.3f}%")

con.close()
