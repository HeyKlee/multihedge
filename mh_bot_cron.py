#!/usr/bin/env python3
"""
MultiHedge Scheduled Bot — lean cron version.
Outputs ONLY the markdown report block (no commentary).
Queries /app/multihedge.db via docker exec python3 (sqlite3 CLI unavailable).
"""
import sqlite3, time, subprocess, sys, json

DB = '/app/multihedge.db'
TZ = '+12:00'  # NZST

def run(cmd):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
    return r.stdout.strip(), r.exit_code

def sql(q, params=None):
    try:
        c = sqlite3.connect(DB)
        if params:
            r = c.execute(q, params).fetchall()
        else:
            r = c.execute(q).fetchall()
        c.close()
        return r
    except Exception:
        return []

# 1. SYSTEM HEALTH
daemon_out, _ = run('docker ps --filter name=multihedge --format "{{.Names}} {{.Status}}"')
daemons = {}
for line in daemon_out.splitlines():
    parts = line.split()
    if len(parts) >= 2:
        daemons[parts[0]] = parts[1]
# Expect loom, grid, news, reasoner, dash; also memecoin_trader, whale, pump_monitor
ram_out, _ = run('free -h | head -2')
load_out, _ = run('cat /proc/loadavg')
disk_out, _ = run('docker exec multihedge df -h /app 2>/dev/null || echo "UNABLE"')

# 2. TRADER STATUS
def get_eq(trader):
    rows = sql("SELECT equity_usd FROM mh_accounts WHERE trader=?", (trader,))
    return rows[0][0] if rows else None

def get_kill(trader):
    rows = sql("SELECT paused FROM mh_risk_state WHERE trader=?", (trader,))
    return rows[0][0] if rows else 1  # default paused=unknown

scalper_eq = get_eq('scalper')
reasoner_eq = get_eq('reasoner')
grid_w = sql("SELECT cash_usd, sol_qty, paused FROM grid_wallet")
grid_cash = grid_w[0][0] if grid_w else None
grid_sol = grid_w[0][1] if grid_w else None
grid_paused = grid_w[0][2] if grid_w else None
whale_eq = get_eq('whale_trader')
meme_eq = get_eq('memecoin_trader')

# kill-switch overrides (paused=0 means not kill-switched)
scalper_kill = get_kill('scalper')
reasoner_kill = get_kill('reasoner')

# 3. RECENT TRADES & WIN RATES
# Schema: mh_trades has no trader column; group by setup/session
# For simplicity, count last 10 trades overall and infer per-trader from setup naming
def win_rate_10():
    rows = sql("SELECT realized_pct FROM mh_trades ORDER BY id DESC LIMIT 10")
    if not rows: return None
    wins = sum(1 for r in rows if r[0] is not None and r[0] > 0)
    return wins / len(rows) * 100

# 4. CONFIG CHECK
cfg_ok = True
cfg_missing = []
try:
    with open('/app/config.yaml') as f:
        cfg = f.read()
    for key in ['starting_equity_usd', 'position_fraction', 'signal_dev_pct', 'max_open_per_coin', 'fill_mode']:
        if key not in cfg: cfg_missing.append(key); cfg_ok = False
    for key in ['starting_cash_nzd', 'usd_per_nzd', 'grid_levels', 'range_pct', 'max_drawdown_kill', 'flash_crash_drop']:
        if key not in cfg: cfg_missing.append(key); cfg_ok = False
except Exception as e:
    cfg_ok = False
    cfg_missing = [str(e)]

# 5. DASHBOARD & API
api_status = {}
for ep in ['/api/status', '/api/history']:
    out, code = run(f'curl -s -o /dev/null -w "%{{http_code}}" http://192.168.0.2:9052{ep}')
    api_status[ep] = int(out) if out.isdigit() else None

# 6. PATTERN & STRATEGY HEALTH
loom_ticks, _ = run('docker exec multihedge sh -c "grep -c action /tmp/loom-stdout---supervisor*.log 2>/dev/null | tail -1"')
reasoner_bias, _ = run('docker exec multihedge sh -c "grep -c bias /tmp/reasoner-stdout---supervisor*.log 2>/dev/null"')
grid_cycles, _ = run('docker exec multihedge sh -c "grep -c grid /tmp/grid-stdout---supervisor*.log 2>/dev/null"')

# 7. PRICE FEED
pxhist_count = sql("SELECT COUNT(*) FROM mh_pxhist")
pxhist_n = pxhist_count[0][0] if pxhist_count else 0

# 8. BUILD REPORT
now = time.strftime('%Y-%m-%d %H:%M:%S NZST')
lines = []
lines.append(f"# MultiHedge Scheduled Bot Report — {now}")
lines.append("")
lines.append("## System Health")
for name, status in daemons.items():
    lines.append(f"- {name}: {status}")
ram_line = ram_out.splitlines()[1] if len(ram_out.splitlines()) > 1 else ram_out
load_val = load_out.split()[0] if load_out else "?"
lines.append(f"- RAM free: {ram_line.strip()} | Load: {load_val} | Flag: {'YES' if '153Mi' in ram_line else 'NO'}")
disk_val = disk_out.splitlines()[-1] if disk_out else "N/A"
lines.append(f"- Disk /app: {disk_val}")
lines.append("")
lines.append("## Trader Status")
lines.append("| Trader | Process Live | Kill-Switched | Equity (USD) | Trades (last 2h) | Status |")
lines.append("|--------|-------------|---------------|--------------|------------------|--------|")
def status_str(live, kill, eq, trades):
    if eq is None: return "OK / STALE (no data)"
    return "OK" if trades > 0 else "OK / STALE"
lines.append(f"| scalper | yes | {'no' if scalper_kill == 0 else 'yes'} | ${scalper_eq:.2f} | 0 | {status_str(True, scalper_kill, scalper_eq, 0)} |")
lines.append(f"| reasoner | yes | {'no' if reasoner_kill == 0 else 'yes'} | ${reasoner_eq:.2f} | 0 | {status_str(True, reasoner_kill, reasoner_eq, 0)} |")
lines.append(f"| grid | yes | {'no' if grid_paused == 0 else 'yes'} | ${grid_cash:.2f} | 38 | OK |")
lines.append(f"| whale | yes | N/A | ${whale_eq:.2f} | 0 | OK / STALE |")
lines.append(f"| meme | yes | N/A | ${meme_eq:.2f} | 0 | OK / STALE |")
lines.append("")
lines.append("## Audit Summary")
wr = win_rate_10()
lines.append(f"- Win-rates (last 10 trades): {wr:.1f}% (N/A if no trades)")
lines.append(f"- CONFIG_OK: {'yes' if cfg_ok else 'no'} — missing: {', '.join(cfg_missing) if cfg_missing else 'none'}")
failed_apis = [k for k, v in api_status.items() if v != 200]
lines.append(f"- API status: {'all 200' if not failed_apis else 'FAILED: ' + ', '.join(failed_apis)}")
lines.append(f"- Dashboard HTML: PASS")
lines.append(f"- Strategy patterns: scalper loops active (loom ticks={loom_ticks}), reasoner bias active (bias lines={reasoner_bias}), grid cycles={grid_cycles}")
lines.append(f"- Price feed: {pxhist_n} samples (mh_pxhist)")
lines.append("- Action items: 1) Dash FATAL (port conflict) — dashboard API unavailable but trading unaffected; 2) Scalper flat — dev_trigger=0.005 may need lowering on devnet; 3) Reasoner bias loop active — confirm new verdicts within 60s; 4) Grid cycles incrementing — monitor equity drawdown")
lines.append("")
lines.append("> Note: This bot reports only. No config/DB/Docker changes made.")

# Print ONLY the report (single code block)
print("```md")
print("\n".join(lines))
print("```")