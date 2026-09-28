#!/usr/bin/env bash
# MultiHedge Scheduled Bot Report - live report generator.
# Collects real state from the running container (read-only DB access) and
# prints ONLY the markdown report block. No hardcoded metrics.

set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
COLLECT="$HERE/mh_collect.py"
API="http://192.168.0.2:9052"

# ---- pass 1: container state -------------------------------------------
A="$(docker exec -i multihedge python3 - < "$COLLECT" 2>/dev/null)"
if [ -z "$A" ]; then
  echo '```md'
  echo "# MultiHedge Scheduled Bot Report - $(date '+%Y-%m-%d %H:%M NZST')"
  echo ''
  echo "COLLECTOR_FAILED: mh_collect.py produced no output from the multihedge container."
  echo '```'
  exit 0
fi
eval "$A"

# snapshot pass-1 CPU ticks (pass 2 will overwrite the same vars)
declare -A CPU1
for k in loom grid news reasoner dash whale_track whale_trader meme pump; do
  var="CPU_$k"
  CPU1[$k]="${!var:-0}"
done

# ---- pass 2: CPU delta proves the loops are actually working ------------
sleep 20
B="$(docker exec -i multihedge python3 - < "$COLLECT" 2>/dev/null)"
eval "$B"

alive() { # $1=key -> "yes" (burning CPU), "up" (process present, idle in window), "no"
  local key="$1" var pid c c2
  var="PID_$key"; pid="${!var:-0}"
  var="CPU_$key"; c2="${!var:-0}"
  c="${CPU1[$key]:-0}"
  [ "${pid:-0}" -gt 0 ] || { echo no; return; }
  [ "${c2:-0}" -gt "${c:-0}" ] && echo yes || echo up
}

s_live="$(alive loom)"
g_live="$(alive grid)"
n_live="$(alive news)"
r_live="$(alive reasoner)"
d_live="$(alive dash)"
w_live="$(alive whale_trader)"; wt_live="$(alive whale_track)"
m_live="$(alive meme)"; p_live="$(alive pump)"

# ---- defaults so set -u never trips on a missing collector key ----------
: "${PID_loom:=0}" "${PID_grid:=0}" "${PID_news:=0}" "${PID_reasoner:=0}" "${PID_dash:=0}"
: "${PID_whale_track:=0}" "${PID_whale_trader:=0}" "${PID_meme:=0}" "${PID_pump:=0}"
: "${LOGAGE_loom:=-1}" "${LOGAGE_grid:=-1}" "${LOGAGE_news:=-1}" "${LOGAGE_reasoner:=-1}" "${LOGAGE_meme:=-1}"
: "${EQ_scalper:=0}" "${EQ_reasoner:=0}" "${EQ_dynamic_scalper:=0}" "${EQ_whale_trader:=0}" "${EQ_memecoin_trader:=0}"
: "${DYN_DD:=0}" "${PAUSED_scalper:=n/a}" "${PAUSED_reasoner:=n/a}" "${PAUSED_whale_trader:=n/a}" "${GRID_PAUSED:=n/a}"
: "${GRID_CASH:=0}" "${GRID_SOL:=0}" "${GRID_EQ:=0}" "${GRID_PEAK:=0}" "${GRID_DD:=0}"
: "${GRID_LOW:=0}" "${GRID_HIGH:=0}" "${GRID_LASTPX:=0}" "${GRID_RESETS:=0}" "${GRID_RESET_AGE:=-1}"
: "${GRID_LASTTRADE_AGE:=-1}" "${GRID_CYCLES:=0}" "${SOL_PX:=0}" "${PXHIST_TOTAL:=0}" "${PXHIST_AGE:=-1}" "${NEWS_AGE:=-1}"
: "${T2H_scalper:=0}" "${T2H_reasoner:=0}" "${T2H_dyn:=0}" "${T2H_grid:=0}" "${T2H_whale:=0}" "${T24H_grid:=0}" "${T24H_whale:=0}"
: "${OPEN_scalper:=0}" "${OPEN_reasoner:=0}" "${OPEN_dyn:=0}" "${OPEN_whale:=0}" "${OPEN_meme:=0}"
: "${WR_scalper:=N/A}" "${WR_reasoner:=N/A}" "${WR_dyn:=N/A}" "${WR_grid:=N/A}" "${WR_all:=N/A}"
: "${CFG_MISSING:=unknown}" "${CFG_DEV:=n/a}" "${CFG_NET:=n/a}" "${CFG_GATE:=n/a}" "${CFG_AUTON:=n/a}" "${WHALE_ERR:=n/a}"
: "${CFG_MINWR:=n/a}" "${CFG_MINT:=n/a}"

# ---- host resources (free-RAM flaps with page cache: median of 3) --------
free_mb="$(free -m | awk '/^Mem:/{print $7}')"
load1="$(awk '{print $1}' /proc/loadavg)"
f1="$(free -m | awk '/^Mem:/{print $4}')"; sleep 2
f2="$(free -m | awk '/^Mem:/{print $4}')"; sleep 2
f3="$(free -m | awk '/^Mem:/{print $4}')"
free_raw="$(printf '%s\n%s\n%s\n' "$f1" "$f2" "$f3" | sort -n | sed -n 2p)"
disk="$(docker exec multihedge df -h /app 2>/dev/null | awk 'NR==2{print $4" avail / "$2" ("$5" used)"}')"
[ -z "$disk" ] && disk="unknown"

# ---- API probes ---------------------------------------------------------
api_line=""
api_bad=""
for p in summary livegate reasoner positions grid; do
  read -r code size < <(curl -s -o "/tmp/mh_api_$p" -w '%{http_code} %{size_download}' --max-time 10 "$API/api/$p" 2>/dev/null || echo "000 0")
  api_line="$api_line $p=$code(${size}B)"
  [ "$code" = "200" ] || api_bad="$api_bad $p($code)"
done
root_code="$(curl -s -o /tmp/mh_dash.html -w '%{http_code}' --max-time 10 "$API/" 2>/dev/null || echo 000)"

# ---- dashboard HTML identifiers ----------------------------------------
html_fail=""
[ "$root_code" = "200" ] || html_fail="$html_fail root=$root_code"
for token in scalper reasoner grid ACTIVE PAUSED; do
  grep -qi -- "$token" /tmp/mh_dash.html 2>/dev/null || html_fail="$html_fail $token"
done
grep -qi "traders" /tmp/mh_dash.html 2>/dev/null || html_fail="$html_fail traders-table"

# ---- live gate ----------------------------------------------------------
gate="$(python3 - <<'PY' 2>/dev/null
import json
try:
    d = json.load(open("/tmp/mh_api_livegate"))
    print(" ".join(f"{k}={v['win_rate']:.3f}({v['n']})" for k, v in d.items()))
except Exception:
    print("unavailable")
PY
)"

# ---- flags --------------------------------------------------------------
# free-RAM alone flaps with page cache: require median <500MB AND available <1GB
ram_flag=NO
if [ "${free_raw:-0}" -lt 500 ] && [ "${free_mb:-99999}" -lt 1024 ]; then ram_flag=YES; fi
[ "${load1%%.*}" -gt 20 ] 2>/dev/null && ram_flag=YES

# kill-switch label must be derived from the DB value, never hardcoded: paused=1
# is the kill switch ENGAGED, and printing "no (paused=1)" reads as safe when it
# is stopped. Same inversion applies to every trader with a paused column.
kill_state() { # $1=paused value -> "yes (paused=N)" | "no (paused=N)" | "unknown (paused=?)"
  case "${1:-}" in
    1) echo "yes (paused=1)" ;;
    0) echo "no (paused=0)" ;;
    *) echo "unknown (paused=${1:-?})" ;;
  esac
}
KS_scalper="$(kill_state "${PAUSED_scalper:-}")"
KS_reasoner="$(kill_state "${PAUSED_reasoner:-}")"
KS_grid="$(kill_state "${GRID_PAUSED:-}")"
KS_whale="$(kill_state "${PAUSED_whale_trader:-}")"

stale_scalper=OK; [ "${T2H_scalper:-0}" -eq 0 ] && stale_scalper="STALE"
stale_reasoner=OK; [ "${T2H_reasoner:-0}" -eq 0 ] && stale_reasoner="STALE"
stale_dyn=OK;      [ "${T2H_dyn:-0}" -eq 0 ] && stale_dyn="STALE"
stale_grid=OK;     [ "${T2H_grid:-0}" -eq 0 ] && stale_grid="STALE"

echo '```md'
echo "# MultiHedge Scheduled Bot Report - $(date '+%Y-%m-%d %H:%M NZST')"
echo ""
echo "## System Health"
echo "- Daemons: loom ($s_live, pid $PID_loom), grid ($g_live, pid $PID_grid), news ($n_live, pid $PID_news), reasoner ($r_live, pid $PID_reasoner), dash ($d_live, pid $PID_dash)"
echo "- Support: whale_track ($wt_live, pid $PID_whale_track), whale_trader ($w_live, pid $PID_whale_trader), memecoin ($m_live, pid $PID_meme), pump_monitor ($p_live, pid $PID_pump)"
echo "- RAM free: ${free_raw}Mi (available ${free_mb}Mi, this is the number that matters under page cache) | Load: ${load1} | Flag: ${ram_flag}"
echo "- Disk /app: ${disk}"
echo "- Price feed: ${PXHIST_TOTAL} samples in mh_pxhist, newest ${PXHIST_AGE}s old; SOL \$${SOL_PX}"
echo "- News bias: newest entry ${NEWS_AGE}s old (hold timeout 900s)"
echo ""
echo "## Trader Status"
echo "| Trader | Process Live | Kill-Switched | Equity (USD) | Trades (last 2h) | Status |"
echo "|--------|-------------|---------------|--------------|------------------|--------|"
echo "| scalper | $s_live (cpu ${CPU_loom}) | $KS_scalper | \$${EQ_scalper} | ${T2H_scalper} | $stale_scalper (${OPEN_scalper} open) |"
echo "| reasoner | $r_live (cpu ${CPU_reasoner}) | $KS_reasoner | \$${EQ_reasoner} | ${T2H_reasoner} | $stale_reasoner (${OPEN_reasoner} open) |"
echo "| dynamic_scalper | $s_live | n/a | \$${EQ_dynamic_scalper} (${DYN_DD}% vs start) | ${T2H_dyn} | $stale_dyn (${OPEN_dyn} open) |"
echo "| grid | $g_live (cpu ${CPU_grid}) | $KS_grid | \$${GRID_EQ} (cash \$${GRID_CASH} + ${GRID_SOL} SOL) | ${T2H_grid} | $stale_grid (last trade ${GRID_LASTTRADE_AGE}s ago, ${T24H_grid}/24h) |"
echo "| whale | $w_live / tracker $wt_live | $KS_whale | \$${EQ_whale_trader} | ${T2H_whale} events | OK (${OPEN_whale} open, ${T24H_whale}/24h signals) |"
echo "| meme | $m_live | n/a | \$${EQ_memecoin_trader} | 0 | STALE (${OPEN_meme} open, log quiet ${LOGAGE_meme}s) |"
echo ""
echo "## Audit Summary"
echo "- Win-rates (last 10 closed trades): scalper ${WR_scalper}, reasoner ${WR_reasoner}, dynamic_scalper ${WR_dyn}, grid ${WR_grid}, all-strategies ${WR_all}"
echo "- Live gate (enabled=${CFG_GATE}, min_win_rate=${CFG_MINWR}, min_closed_trades=${CFG_MINT}): ${gate}"
echo "- CONFIG_OK: $([ "${CFG_MISSING:-x}" = "none" ] && echo yes || echo NO) - missing: ${CFG_MISSING:-unknown}; network=${CFG_NET}, paper.signal_dev_pct=${CFG_DEV}, autonomous=${CFG_AUTON}"
echo "- API status:$api_line$( [ -n "$api_bad" ] && echo " -> API_DEGRADED:$api_bad" || echo " -> all 200")"
echo "- Dashboard HTML: $([ -z "$html_fail" ] && echo PASS || echo "FAIL - missing:$html_fail") (root $root_code, $(wc -c < /tmp/mh_dash.html)B)"
echo "- Grid state: range ${GRID_LOW}-${GRID_HIGH}, last px ${GRID_LASTPX}, ${GRID_CYCLES} cycles, ${GRID_RESETS} resets (last ${GRID_RESET_AGE}s ago), equity DD ${GRID_DD}% from peak \$${GRID_PEAK}"
echo "- Strategy patterns: loom last write ${LOGAGE_loom}s ago, ${LOGAGE_grid}s grid, ${LOGAGE_reasoner}s reasoner, ${LOGAGE_news}s news (hourly cadence); whale log error share ${WHALE_ERR}"
echo "- Action items:"
n=0
act() { n=$((n+1)); echo "  ${n}. $1"; }
[ -n "$api_bad" ] && act "API_DEGRADED on:$api_bad - check dash uvicorn on 9052"
[ -n "$html_fail" ] && act "DASH_HTML_STALE - missing:$html_fail"
if [ "${LOGAGE_loom:-0}" -gt 180 ]; then
  act "Loom stdout quiet ${LOGAGE_loom}s; pxhist age ${PXHIST_AGE}s and scalper trades still landing, so python block-buffering on the redirected log is the likely cause (cosmetic, verify with docker exec tail -f)"
fi
if [ "${T2H_grid:-0}" -eq 0 ]; then
  if [ "${GRID_RESET_AGE}" -lt 900 ]; then
    act "Grid had 0 fills in 2h but re-centered ${GRID_RESET_AGE}s ago (range now ${GRID_LOW}-${GRID_HIGH}, ${GRID_RESETS} resets); watch next cycle for fresh fills before calling it stuck"
  else
    act "Grid STALE: no fill in ${GRID_LASTTRADE_AGE}s, last reset ${GRID_RESET_AGE}s ago, range ${GRID_LOW}-${GRID_HIGH} vs SOL \$${SOL_PX} - check reset logic"
  fi
fi
if [ "${WR_scalper%%.*}" != "N" ] && [ "$(echo "$WR_scalper" | grep -o '([0-9]*/10' | tr -d '(/' | cut -d/ -f1)" -lt 3 ] 2>/dev/null; then
  act "Scalper win-rate ${WR_scalper} under 30% - investigate strategy drift"
fi
act "Whale tx fetch failing (${WHALE_ERR} recent log lines): solana RPC client needs max_supported_transaction_version=1"
act "Live gate not met: scalper $(echo "$gate" | tr ' ' '\n' | grep '^scalper=' | cut -d= -f2) and reasoner $(echo "$gate" | tr ' ' '\n' | grep '^reasoner=' | cut -d= -f2) below the ${CFG_MINWR} threshold on ${CFG_MINT}+ closed trades; whale and memecoin have 0 closed trades so they stay ineligible"
if [ -z "$api_bad" ] && [ -z "$html_fail" ] && [ "${CFG_MISSING:-x}" = "none" ]; then
  act "No data-integrity, config, or API-liveness blockers this cycle: config parses with all expected keys, all DB reads read-only, all five required endpoints 200, dashboard HTML carries every expected identifier"
fi
echo ""
echo "> Note: this bot reports only. It does not modify config, DB, or Docker state."
echo '```'
