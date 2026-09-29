"""READ-ONLY: prove the dashboard livegate hardcodes a threshold that config.yaml declares."""
import pathlib, re, subprocess

R = pathlib.Path("/home/kelly/multihedge")

print("=" * 8, "config.yaml declared policy")
cfg = (R / "config.yaml").read_text()
m = re.search(r"min_trades:\s*(\d+)", cfg)
w = re.search(r"min_win_rate:\s*([0-9.]+)", cfg)
print(f"  min_win_rate in config : {w.group(1) if w else 'NOT FOUND'}")
print(f"  min_trades  in config : {m.group(1) if m else 'NOT FOUND'}")

print("\n" + "=" * 8, "what the DASHBOARD actually enforces")
src = (R / "mh_dash.py").read_text()
for i, l in enumerate(src.splitlines(), 1):
    if "eligible" in l and "n >=" in l:
        print(f"  mh_dash.py:{i}: {l.strip()}")

print("\n" + "=" * 8, "consequence: is any trade count affected?")
import sqlite3
# ATLAS Rule A: was R / "deploy/data/multihedge.db", a stale path that resolves to an
# 8KB stub with no ledger tables. Reading it would report an empty history as fact.
import sys as _sys; _sys.path.insert(0, str(R))
import runtime_paths
db = runtime_paths.production_db()
if db.exists():
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    q = con.execute("""
        SELECT setup, COUNT(*) n, SUM(CASE WHEN pnl_pct>0 THEN 1 ELSE 0 END) w
        FROM mh_trades GROUP BY setup""").fetchall()
    print(f"  {'setup':<26}{'n':>6}{'wr':>9}   config(0.6667)  dashboard(0.75)")
    for setup, n, w_ in q:
        if not n: continue
        wr = w_ / n
        print(f"  {str(setup):<26}{n:>6}{wr:>9.3f}   "
              f"{'PASS' if wr>=0.6667 else 'fail':<16}"
              f"{'PASS' if wr>=0.75 else 'fail'}")
    con.close()
else:
    print("  db not readable from host (expected: WAL-split). Use container.")
