"""MultiHedge report collector (runs INSIDE the multihedge container).

Emits shell-eval-able KEY=VALUE lines consumed by mh_report.sh.
Read-only against the DB (WAL-split safe).
"""
import os
import re
import shlex
import sqlite3
import time

DB = "file:/app/multihedge.db?mode=ro"
now = time.time()
out = []


def emit(k, v):
    # shlex.quote so values with spaces/parens stay eval-safe in bash
    out.append("%s=%s" % (k, shlex.quote(str(v))))


def q(conn, sql, args=()):
    try:
        return conn.execute(sql, args).fetchall()
    except Exception as e:  # noqa: BLE001
        return [("ERR", str(e))]


conn = sqlite3.connect(DB, uri=True)

# --- daemon liveness: scan /proc (container has no ps) -------------------
procs = {}
for pid in os.listdir("/proc"):
    if not pid.isdigit():
        continue
    try:
        with open("/proc/%s/cmdline" % pid, "rb") as fh:
            cmd = fh.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
    except Exception:  # noqa: BLE001
        continue
    if not cmd or "python" not in cmd:
        continue
    cpu = 0
    try:
        with open("/proc/%s/stat" % pid) as fh:
            cpu = int(fh.read().split()[13])
    except Exception:  # noqa: BLE001
        pass
    for key, pat in (
        ("loom", r"multihedge\.py\s+loom"),
        ("grid", r"grid_trader\.py"),
        ("news", r"mh_news_loop\.py"),
        ("reasoner", r"mh_reasoner_loop\.py"),
        ("dash", r"uvicorn\s+mh_dash"),
        ("whale_track", r"track_whales\.py"),
        ("whale_trader", r"mh_whale_trader\.py"),
        ("meme", r"mh_memecoin_trader\.py"),
        ("pump", r"pump_monitor\.py"),
    ):
        if re.search(pat, cmd) and key not in procs:
            procs[key] = (pid, cpu)

for key in ("loom", "grid", "news", "reasoner", "dash", "whale_track", "whale_trader", "meme", "pump"):
    entry = procs.get(key)
    emit("PID_%s" % key, entry[0] if entry else 0)
    emit("CPU_%s" % key, entry[1] if entry else 0)


# --- log freshness (container /tmp, one glob per daemon) ----------------
def log_age(pattern):
    import glob
    hits = glob.glob(pattern)
    if not hits:
        return -1
    newest = max(os.path.getmtime(h) for h in hits)
    return int(now - newest)


emit("LOGAGE_loom", log_age("/tmp/loom-stdout*.log"))
emit("LOGAGE_grid", log_age("/tmp/grid-stdout*.log"))
emit("LOGAGE_news", log_age("/tmp/news-stdout*.log"))
emit("LOGAGE_reasoner", log_age("/tmp/reasoner-stdout*.log"))
emit("LOGAGE_whale", log_age("/tmp/whale-stdout*.log"))
emit("LOGAGE_meme", log_age("/tmp/memecoin_trader-stdout*.log"))
emit("LOGAGE_dash", log_age("/tmp/dash-stdout*.log"))

# --- equity / kill switches ---------------------------------------------
accts = {r[0]: r[1] for r in q(conn, "SELECT trader,equity_usd FROM mh_accounts")}
for name in ("scalper", "reasoner", "whale_trader", "memecoin_trader", "dynamic_scalper"):
    emit("EQ_%s" % name, "%.2f" % float(accts.get(name, 0.0)))

risk = {r[0]: r[2] for r in q(conn, "SELECT * FROM mh_risk_state")}
for name in ("scalper", "reasoner", "whale_trader"):
    emit("PAUSED_%s" % name, risk.get(name, "n/a"))

gw = q(conn, "SELECT cash_usd,sol_qty,peak_equity,paused FROM grid_wallet")
if gw and gw[0][0] != "ERR":
    cash, sol, peak, paused = gw[0]
    last_px = q(conn, "SELECT last_px FROM grid_state")
    lp = float(last_px[0][0]) if last_px and last_px[0][0] not in ("ERR", None) else 0.0
    eq = float(cash) + float(sol) * lp
    emit("GRID_CASH", "%.2f" % float(cash))
    emit("GRID_SOL", "%.6f" % float(sol))
    emit("GRID_EQ", "%.2f" % eq)
    emit("GRID_PEAK", "%.2f" % float(peak))
    emit("GRID_DD", "%.2f" % (100.0 * (float(peak) - eq) / float(peak)) if float(peak) else 0.0)
    emit("GRID_PAUSED", paused)
else:
    for k in ("GRID_CASH", "GRID_SOL", "GRID_EQ", "GRID_PEAK", "GRID_DD"):
        emit(k, "0.00")
    emit("GRID_PAUSED", "n/a")

gs = q(conn, "SELECT range_low,range_high,last_px,resets,last_reset_ts FROM grid_state")
if gs and gs[0][0] != "ERR":
    emit("GRID_LOW", "%.2f" % float(gs[0][0]))
    emit("GRID_HIGH", "%.2f" % float(gs[0][1]))
    emit("GRID_LASTPX", "%.2f" % float(gs[0][2]))
    emit("GRID_RESETS", gs[0][3])
    emit("GRID_RESET_AGE", int(now - float(gs[0][4])))

# --- trade counts (2h) ---------------------------------------------------
H2 = 7200
emit("T2H_scalper", q(conn, "SELECT COUNT(*) FROM mh_trades WHERE setup NOT IN ('reasoner','dynamic_scalper') AND close_ts>?", (now - H2,))[0][0])
emit("T2H_reasoner", q(conn, "SELECT COUNT(*) FROM mh_trades WHERE setup='reasoner' AND close_ts>?", (now - H2,))[0][0])
emit("T2H_dyn", q(conn, "SELECT COUNT(*) FROM mh_trades WHERE setup='dynamic_scalper' AND close_ts>?", (now - H2,))[0][0])
emit("T2H_grid", q(conn, "SELECT COUNT(*) FROM grid_trades WHERE ts>?", (now - H2,))[0][0])
emit("T24H_grid", q(conn, "SELECT COUNT(*) FROM grid_trades WHERE ts>?", (now - 86400,))[0][0])
emit("T24H_whale", q(conn, "SELECT COUNT(*) FROM mh_whale_events WHERE ts>?", (now - 86400,))[0][0])
emit("T2H_whale", q(conn, "SELECT COUNT(*) FROM mh_whale_events WHERE ts>?", (now - H2,))[0][0])
gl = q(conn, "SELECT MAX(ts) FROM grid_trades")
emit("GRID_LASTTRADE_AGE", int(now - float(gl[0][0])) if gl and gl[0][0] not in ("ERR", None) else -1)
emit("GRID_CYCLES", q(conn, "SELECT COUNT(DISTINCT cycle_id) FROM grid_trades")[0][0])


# --- win rates over last 10 closed trades --------------------------------
def winrate(where):
    rows = q(conn, "SELECT realized_pct FROM mh_trades WHERE close_ts IS NOT NULL AND %s ORDER BY close_ts DESC LIMIT 10" % where)
    rows = [r for r in rows if r[0] not in ("ERR", None)]
    if not rows:
        return "N/A", 0
    wins = len([r for r in rows if float(r[0]) > 0])
    return "%.1f%% (%d/%d)" % (100.0 * wins / len(rows), wins, len(rows)), len(rows)


wr_s, n_s = winrate("setup NOT IN ('reasoner','dynamic_scalper')")
wr_r, n_r = winrate("setup='reasoner'")
wr_d, n_d = winrate("setup='dynamic_scalper'")
wr_a, n_a = winrate("1=1")
emit("WR_scalper", wr_s)
emit("WR_reasoner", wr_r)
emit("WR_dyn", wr_d)
emit("WR_all", wr_a)

grows = [r[0] for r in q(conn, "SELECT realized_usd FROM grid_trades WHERE realized_usd IS NOT NULL ORDER BY ts DESC LIMIT 10")]
if grows and grows[0] != "ERR":
    gwins = len([r for r in grows if float(r) > 0])
    emit("WR_grid", "%.1f%% (%d/%d)" % (100.0 * gwins / len(grows), gwins, len(grows)))
else:
    emit("WR_grid", "N/A")

# --- open positions ------------------------------------------------------
emit("OPEN_scalper", q(conn, "SELECT COUNT(*) FROM mh_positions")[0][0])
emit("OPEN_reasoner", q(conn, "SELECT COUNT(*) FROM mh_reasoner_positions")[0][0])
emit("OPEN_dyn", q(conn, "SELECT COUNT(*) FROM mh_dynamic_scalp_positions")[0][0])
emit("OPEN_whale", q(conn, "SELECT COUNT(*) FROM mh_whale_positions")[0][0])
emit("OPEN_meme", q(conn, "SELECT COUNT(*) FROM mh_memecoin_positions")[0][0])
emit("LIVE_INV", q(conn, "SELECT COUNT(*) FROM mh_live_inventory")[0][0])

# dynamic scalper drawdown vs start
ds = accts.get("dynamic_scalper")
ds_start = q(conn, "SELECT started_usd FROM mh_accounts WHERE trader='dynamic_scalper'")
if ds is not None and ds_start and ds_start[0][0] not in ("ERR", None):
    emit("DYN_DD", "%.1f" % (100.0 * (float(ds) - float(ds_start[0][0])) / float(ds_start[0][0])))

# --- feeds ---------------------------------------------------------------
px = q(conn, "SELECT COUNT(*),MAX(ts) FROM mh_pxhist WHERE coin!='USDC'")
emit("PXHIST_TOTAL", px[0][0])
emit("PXHIST_AGE", int(now - float(px[0][1])) if px[0][1] not in ("ERR", None) else -1)
nb = q(conn, "SELECT MAX(ts) FROM mh_news_bias")
emit("NEWS_AGE", int(now - float(nb[0][0])) if nb and nb[0][0] not in ("ERR", None) else -1)
solpx = q(conn, "SELECT px FROM mh_pxhist WHERE coin='SOL' ORDER BY ts DESC LIMIT 1")
emit("SOL_PX", "%.2f" % float(solpx[0][0]) if solpx and solpx[0][0] not in ("ERR", None) else "0")

# --- config integrity ----------------------------------------------------
try:
    import yaml
    cfg = yaml.safe_load(open("/app/config.yaml"))
    ok = isinstance(cfg, dict)
except Exception as e:  # noqa: BLE001
    cfg, ok = None, False
    emit("CFG_ERROR", str(e).replace("\n", " "))

if ok:
    need = {
        "paper": ["starting_equity_usd", "position_fraction", "signal_dev_pct", "max_open_per_coin", "fill_mode"],
        "grid": ["starting_cash_nzd", "usd_per_nzd", "grid_levels", "range_pct", "max_drawdown_kill", "flash_crash_drop"],
        "live": ["min_win_rate", "min_closed_trades", "min_aggregate_win_rate", "min_aggregate_trades"],
    }
    missing = []
    for block, keys in need.items():
        blk = cfg.get(block) or {}
        for k in keys:
            if k not in blk:
                missing.append("%s.%s" % (block, k))
    emit("CFG_MISSING", ",".join(missing) if missing else "none")
    emit("CFG_DEV", (cfg.get("paper") or {}).get("signal_dev_pct", "n/a"))
    emit("CFG_NET", cfg.get("network", "n/a"))
    emit("CFG_GATE", (cfg.get("live") or {}).get("gate_enabled", "n/a"))
    emit("CFG_MINWR", (cfg.get("live") or {}).get("min_win_rate", "n/a"))
    emit("CFG_MINT", (cfg.get("live") or {}).get("min_closed_trades", "n/a"))
    emit("CFG_AUTON", (cfg.get("live") or {}).get("autonomous", {}).get("enabled", "n/a"))

# --- whale log error share ------------------------------------------------
try:
    import glob
    wf = glob.glob("/tmp/whale-stdout*.log")
    if wf:
        with open(max(wf, key=os.path.getmtime), "rb") as fh:
            fh.seek(max(0, os.path.getsize(max(wf, key=os.path.getmtime)) - 20000))
            tail = fh.read().decode("utf-8", "replace")
        lines = [l for l in tail.splitlines() if l.strip()]
        errs = len([l for l in lines if "[ERROR]" in l])
        emit("WHALE_ERR", "%d/%d" % (errs, len(lines)))
except Exception as e:  # noqa: BLE001
    emit("WHALE_ERR", "n/a (%s)" % type(e).__name__)

print("\n".join(out))
