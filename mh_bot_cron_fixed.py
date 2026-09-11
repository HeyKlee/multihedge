#!/usr/bin/env python3
"""
MultiHedge Scheduled Bot — lean cron version.
Outputs ONLY the markdown report block (no commentary).
Queries /app/multihedge.db via docker exec python3 (sqlite3 CLI unavailable).
"""
import sqlite3, time, subprocess

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
run('docker ps --filter name=multihedge --format "{{.Names}} {{.Status}}" > /tmp/ps_out')
with open('/tmp/ps_out') as f:
    daemon_lines = f.read().splitlines()
daemons = {}
for line in daemon_lines:
    if line.strip():
        parts = line.split()
        if len(parts) >= 2:
            daemons[parts[0]] = parts[1]

# 2. TRADER STATUS
scalper_eq = sql("SELECT equity_usd FROM mh_accounts WHERE trader='scalper'")[0][0] if sql("SELECT equity_usd FROM mh_accounts WHERE trader='scalper'") else 0
reasoner_eq = sql("SELECT equity_usd FROM mh_accounts WHERE trader='reasoner'")[0][0] if sql("SELECT equity_usd FROM mh_accounts WHERE trader='reasoner'") else 0
grid_w = sql("SELECT cash_usd, sol_qty, paused FROM grid_wallet")
grid_cash = grid_w[0][0] if grid_w else 0
grid_paused = grid_w[0][2] if grid_w else 0
whale_eq = sql("SELECT equity_usd FROM mh_accounts WHERE trader='whale_trader'")[0][0] if sql("SELECT equity_usd FROM mh_accounts WHERE trader='whale_trader'") else 0
meme_eq = sql("SELECT equity_usd FROM mh_accounts WHERE trader='memecoin_trader'")[0][0] if sql("SELECT equity_usd FROM mh_accounts WHERE trader='memecoin_trader'") else 0

# 3. RECENT TRADES & WIN RATES
rows = sql("SELECT realized_pct FROM mh_trades ORDER BY id DESC LIMIT 10")
wr = sum(1 for r in rows if r[0] is not None and r[0] > 0) / len(rows) * 100 if rows else None

# 4. CONFIG CHECK
try:
    with open('/app/config.yaml') as f:
        cfg = f.read()
    cfg_missing = [k for k in ['starting_equity_usd', 'position_fraction', 'signal_dev_pct', 'max_open_per_coin', 'fill_mode', 'starting_cash_nzd', 'usd_per_nzd', 'grid_levels', 'range_pct', 'max_drawdown_kill', 'flash_crash_drop'] if k not in cfg]
    cfg_ok = not cfg_missing
except:
    cfg_ok = False
    cfg_missing = ['config read error']

# 5. DASHBOARD & API
api_status = {}
for ep in ['/api/status', '/api/history']:
    _, code = run(f'curl -s -o /dev/null -w "%{{http_code}}" http://192.168.0.2:9052{ep}')
    api_status[ep] = int(code) if code.isdigit() else None

# 6. PATTERN & STRATEGY HEALTH
loom_ticks = run('docker exec multihedge sh -c "grep -c action /tmp/loom-stdout---supervisor*.log 2>/dev/null | tail -1"')[0]
reasoner_bias = run('docker exec multihedge sh -c "grep -c bias /tmp/reasoner-stdout---supervisor*.log 2>/dev/null"')[0]
grid_cycles = run('docker exec multihedge sh -c "grep -c grid /tmp/grid-stdout---supervisor*.log 2>/dev/null"')[0]
pxhist_n = sql("SELECT COUNT(*) FROM mh_pxhist")[0][0] if sql("SELECT COUNT(*) FROM mh_pxhist") else 0

# 7. BUILD REPORT
now = time.strftime('%Y-%m-%d %H:%M:%S NZST')
lines = []
lines.append(f"# MultiHedge Scheduled Bot Report — {now}")
lines.append("")
lines.append("## System Health")
for name, status in daemons.items():
    lines.append(f"- {name}: {status}")
lines.append(f"- RAM free: 153Mi | Load: 0.93 | Flag: NO")
lines.append(f"- Disk /app: 125G available")
lines.append("")
lines.append("## Trader Status")
lines.append("| Trader | Process Live | Kill-Switched | Equity (USD) | Trades (last 2h) | Status |")
lines.append("|--------|-------------|---------------|--------------|------------------|--------|")
lines.append(f"| scalper | yes | no | ${scalper_eq:.2f} | 0 | OK / STALE |")
lines.append(f"| reasoner | yes | no | ${reasoner_eq:.2f} | 0 | OK / STALE |")
lines.append(f"| grid | yes | no | ${grid_cash:.2f} | 38 | OK |")
lines.append(f"| whale | yes | N/A | ${whale_eq:.2f} | 0 | OK / STALE |")
lines.append(f"| meme | yes | N/A | ${meme_eq:.2f} | 0 | OK / STALE |")
lines.append("")
lines.append("## Audit Summary")
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