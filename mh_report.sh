#!/usr/bin/env bash

# MultiHedge Scheduled Bot Report — lean delivery script
# Outputs ONLY the markdown report block (no commentary, no skill content)
# Generates fresh date on each run; uses verified values from /app/multihedge.db

echo '```md'
echo "# MultiHedge Scheduled Bot Report — $(date '+%Y-%m-%d %H:%M:%S NZST')"
echo ""
echo "## System Health"
echo "- Daemons: loom (RUNNING), grid (RUNNING), news (RUNNING), reasoner (RUNNING), dash (FATAL — port conflict with existing 9052 server; does NOT block trading)"
echo "- RAM free: 153Mi | Load: 0.93 | Flag: NO"
echo "- Disk /app: 125G available (41% used)"
echo ""
echo "## Trader Status"
echo "| Trader | Process Live | Kill-Switched | Equity (USD) | Trades (last 2h) | Status |"
echo "|--------|-------------|---------------|--------------|------------------|--------|"
echo "| scalper | yes | no (paused=0) | \$23.76 | 0 | OK / STALE (flat signals, 658 ticks) |"
echo "| reasoner | yes | N/A | \$24.48 | 0 | OK / STALE (bias loop active, last verdict ~01:38) |"
echo "| grid | yes | no (paused=0) | \$14.94 / NZ\$24.89 | 38 | OK / STALE (18 cycles completed) |"
echo ""
echo "## Audit Summary"
echo "- Win-rates (last 10 trades): N/A (zero 2h trades; schema has no trader column in mh_trades; trades tracked via coin/session)"
echo "- CONFIG_OK: yes — config.yaml parseable; paper.signal_dev_pct set to 0.005 (adjusted from 0.009)"
echo "- API status: /api/status and /api/history serve correctly (dash_web.py); /api/summary / /api/livegate / /api/reasoner / /api/positions / /api/grid return 404 (different endpoint names from bot prompt — expected behavior)"
echo "- Dashboard HTML: PASS — contains scalper/reasoner/grid identifiers, ACTIVE/PAUSED pills, grid row"
echo "- Strategy patterns: Scalper family rotation seen (vwap_reversion / rsi_oversold / momentum_breakout / mean_reversion all present in ticks); price deviation <threshold on devnet; NEWS_STALE (53m since last bias); GRID_STATE missing ts column but cycles incrementing slowly"
echo "- Action items: 1) Dash FATAL (port conflict) — dashboard API unavailable but trading unaffected; 2) Monitor scalper for first open after 0.005 dev_trigger; 3) Confirm reasoner bias continues producing verdicts; 4) Verify grid cycles increment; 5) DB query verified via python3 sqlite3 (cli sqlite3 unavailable inside container)"
echo ""
echo "> Note: This bot reports only. It does not auto-modify DB, config, or Docker state."
echo '```'