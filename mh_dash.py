"""
MultiHedge DASHBOARD — FastAPI, AutoHedge-style design recolored to
lavender-tulip / dark-navy / red, generalized across multiple coins.

Reads multihedge.db (paper ledger, strategy rotation, reasoner, news bias).
DB path: $MULTIHEDGE_DB or default /app/multihedge.db.

Run: uvicorn mh_dash:app --host 0.0.0.0 --port 9052
"""
import os
import sqlite3
import time
import json
from pathlib import Path
import urllib.parse
import urllib.request

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

import paper
import pricefeed as pricefeed_module

DB_PATH = Path(os.environ.get("MULTIHEDGE_DB", str(Path(__file__).parent / "multihedge.db")))
paper.DB_PATH = DB_PATH  # dashboard and engine share the same ledger

COINS = ["SOL", "JUP", "ETH"]
SETUPS = ["momentum_breakout", "mean_reversion", "rsi_oversold", "vwap_reversion"]

app = FastAPI(title="MultiHedge")


def _conn():
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def _accounts():
    c = _conn()
    try:
        rows = {r["coin"]: dict(r) for r in c.execute("SELECT * FROM mh_accounts").fetchall()}
    except Exception:
        rows = {}
    c.close()
    return rows


def _positions():
    c = _conn()
    try:
        rows = [dict(r) for r in c.execute("SELECT * FROM mh_positions ORDER BY open_ts DESC").fetchall()]
    except Exception:
        rows = []
    c.close()
    return rows


def _trades(limit=100):
    c = _conn()
    try:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM mh_trades ORDER BY (open_ts IS NULL), open_ts DESC LIMIT ?", (limit,)).fetchall()]
    except Exception:
        try:
            rows = [dict(r) for r in c.execute("SELECT * FROM mh_trades ORDER BY rowid DESC LIMIT ?", (limit,)).fetchall()]
        except Exception:
            rows = []
    c.close()
    return rows


def _pxhist(coin, limit=200):
    c = _conn()
    try:
        rows = [dict(r) for r in c.execute(
            "SELECT ts, px FROM mh_pxhist WHERE coin=? ORDER BY ts DESC LIMIT ?", (coin, limit)).fetchall()]
    except Exception:
        rows = []
    c.close()
    return list(reversed(rows))


def _strategies():
    c = _conn()
    beta = {}
    points = {}
    try:
        for r in c.execute("SELECT * FROM mh_strategy_beta").fetchall():
            beta[f"{r['coin']}:{r['setup']}"] = dict(r)
        for r in c.execute("SELECT * FROM mh_strat_points").fetchall():
            points[f"{r['coin']}:{r['setup']}"] = r["points"]
    except Exception:
        pass
    c.close()
    out = []
    for coin in COINS:
        for s in SETUPS:
            b = beta.get(f"{coin}:{s}") or {"wins": 0, "losses": 0}
            n = int(b.get("wins", 0)) + int(b.get("losses", 0))
            out.append({
                "coin": coin, "setup": s,
                "points": int(points.get(f"{coin}:{s}", 0)),
                "wins": int(b.get("wins", 0)), "losses": int(b.get("losses", 0)),
                "win_rate": round(int(b.get("wins", 0)) / n, 3) if n else 0.0,
                "n": n,
            })
    return out


def _reasoner():
    c = _conn()
    poss = []
    bias = {}
    try:
        poss = [dict(r) for r in c.execute("SELECT * FROM mh_reasoner_positions ORDER BY ts DESC").fetchall()]
    except Exception:
        pass
    for coin in COINS:
        try:
            r = c.execute("SELECT * FROM mh_news_bias WHERE symbol=? ORDER BY id DESC LIMIT 1", (coin,)).fetchone()
            bias[coin] = dict(r) if r else None
        except Exception:
            bias[coin] = None
    c.close()
    fx = pricefeed_module.nzd_per_usd() if pricefeed_module else 1.67
    accounts = [{"trader": t, "equity": paper.equity(t),
                 "committed": paper.committed(t), "available": paper.available(t),
                 "fx": fx}
                for t in (paper.TRADER_REASONER,)]
    return {
        "accounts": accounts,
        "positions": poss, "bias": bias,
    }


def _grid():
    """Grid trader (SOL) status from its own tables; safe before tables exist."""
    try:
        import grid_trader
        return grid_trader.status_json()
    except Exception:
        return None


def _survival_cycles():
    """Replay the Xora-Survival decision log (cycles.jsonl) newest-first."""
    path = Path(os.environ.get(
        "MULTIHEDGE_CYCLES_LOG",
        str(Path(__file__).parent / "deploy/data/agent_logs/cycles.jsonl")))
    rows = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        return []
    return list(reversed(rows))


@app.get("/api/survival")
def api_survival():
    """The Xora-Survival agent: real on-chain fills (live), open live inventory,
    separately-labelled paper incubator activity, decision log, and edge."""
    c = _conn()
    live_positions, paper_positions = [], []
    try:
        live_positions = [dict(r) for r in c.execute(
            "SELECT * FROM mh_live_inventory ORDER BY opened_ts DESC").fetchall()]
    except Exception:
        pass
    try:
        paper_positions = [dict(r) for r in c.execute(
            "SELECT * FROM mh_dynamic_scalp_positions ORDER BY opened_ts DESC").fetchall()]
    except Exception:
        pass
    c.close()

    # ---- Real on-chain fills (live). This dashboard's canonical DB grows a
    # mh_live_logs table the first time the signer settles a fill. ----
    live_trades = []
    try:
        cc = _conn()
        live_trades = [dict(r) for r in cc.execute(
            "SELECT * FROM mh_live_logs ORDER BY ts DESC").fetchall()]
        cc.close()
    except Exception:
        pass
    # Legacy one-off manual fills were logged to the host-root engine ledger.
    legacy = Path(os.environ.get("MULTIHEDGE_LEGACY_DB", str(Path(__file__).parent / "multihedge.db")))
    try:
        lc = sqlite3.connect(legacy)
        lc.row_factory = sqlite3.Row
        for r in lc.execute("SELECT * FROM mh_live_logs ORDER BY ts DESC").fetchall():
            live_trades.append(dict(r))
        lc.close()
    except Exception:
        pass
    # De-dupe by signature, keep newest.
    seen = set()
    uniq = []
    for t in live_trades:
        sig = t.get("signature")
        if sig in seen:
            continue
        seen.add(sig)
        uniq.append(t)
    live_trades = uniq

    # ---- Paper incubator activity (closed + open) stays clearly separated. ----
    paper_trades = [t for t in _trades(1000) if t.get("setup") == "dynamic_scalper"]
    paper_trades.sort(key=lambda x: x.get("close_ts") or x.get("open_ts") or 0, reverse=True)

    paper_wins = sum(1 for t in paper_trades if (t.get("realized_usd") or 0) > 0)
    paper_net = round(sum(t.get("realized_usd") or 0 for t in paper_trades), 6)
    reasons = {}
    for t in paper_trades:
        r = t.get("exit_reason") or "unknown"
        reasons[r] = reasons.get(r, 0) + 1
    paper_pos_rows = paper_positions
    live_pos_rows = live_positions
    paper_notional = round(sum((p.get("qty") or 0) * (p.get("entry_usd") or 0)
                               for p in paper_pos_rows), 4)
    live_notional = round(sum((p.get("amount_atomic") or 0) / (10 ** (p.get("decimals") or 0))
                              * (p.get("entry_usd") or 0) for p in live_pos_rows), 4)
    return {
        "live_positions": live_pos_rows,
        "paper_positions": paper_pos_rows,
        "live_trades": live_trades,
        "paper_trades": paper_trades,
        "cycles": _survival_cycles(),
        "edge": {
            "live_n": len(live_trades),
            "paper_n": len(paper_trades),
            "paper_wins": paper_wins, "paper_losses": len(paper_trades) - paper_wins,
            "paper_win_rate": round(paper_wins / len(paper_trades), 4) if paper_trades else 0.0,
            "paper_net_usd": paper_net,
            "live_fills": len([t for t in live_trades if t.get("side") == "BUY"]),
            "live_closes": len([t for t in live_trades if t.get("side") == "SELL"]),
        },
        "exit_reasons": reasons,
        "notional": {"paper_usd": paper_notional, "live_usd": live_notional},
        "risk_params": _survival_risk_status(
            Path(os.environ.get("MULTIHEDGE_DB",
                                str(Path(__file__).parent / "multihedge.db"))),
        ),
    }


def _survival_risk_status(db_path: Path) -> dict:
    """Report the currently active TP/SL/max-hold per class (default or tuned),
    plus whether the autonomous autotuner has written an override."""
    try:
        import yaml
        from live_inventory import _default_params, _risk_params_override
        cfg = yaml.safe_load((Path(__file__).parent / "config.yaml").read_text(encoding="utf-8"))
    except Exception:
        return {}
    out = {}
    for mode in ("MEME", "SERIOUS"):
        override = _risk_params_override(db_path, mode)
        active = override or _default_params(mode)
        out[mode] = {
            "take_profit_pct": active["take_profit_pct"],
            "stop_loss_pct": active["stop_loss_pct"],
            "max_hold_seconds": active["max_hold_seconds"],
            "source": "autotuned" if override is not None else "default",
        }
    return out


@app.get("/api/summary")
def api_summary():
    poss = _positions()
    trades = _trades(1000)
    sr = _strategies()
    reasoner = _reasoner()
    extras = []
    c = _conn()
    for table in ('mh_whale_positions','mh_memecoin_positions'):
        try: extras.extend(dict(r) for r in c.execute('SELECT * FROM '+table))
        except sqlite3.OperationalError: pass
    c.close()
    coins = []
    for coin in COINS:
        ct = [t for t in trades if t.get("coin") == coin]
        # scalper trades are those NOT tagged as reasoner or whale_trader
        sc = [t for t in ct if t.get("setup") not in ("reasoner", "whale_trader", "memecoin_trader")]
        rc = [t for t in ct if t.get("setup") == "reasoner"]
        wc = [t for t in ct if t.get("setup") == "whale_trader"]
        best = sorted([s for s in sr if s["coin"] == coin], key=lambda x: -x["points"])
        active = best[0]["setup"] if best else "-"
        open_sc = sum(1 for p in poss if p["coin"] == coin)
        open_rc = sum(1 for r in reasoner["positions"] if (r.get("symbol") or "").upper() == coin)
        coins.append({
            "symbol": coin,
            "scalper_trades": len(sc), "scalper_wins": sum(1 for t in sc if t.get("realized_pct", 0) > 0),
            "reasoner_trades": len(rc), "reasoner_wins": sum(1 for t in rc if t.get("realized_pct", 0) > 0),
            "whale_trades": len(wc), "whale_wins": sum(1 for t in wc if t.get("realized_pct", 0) > 0),
            "open_positions": open_sc + open_rc + sum(p.get("symbol")==coin for p in extras),
            "active_setup": active,
        })
    traders = []
    fx = pricefeed_module.nzd_per_usd() if pricefeed_module else 1.67
    for t in paper.TRADERS:
        eq = paper.equity(t)
        traders.append({
            "trader": t, "equity": eq, "started": paper.DEFAULT_EQUITY,
            "committed": paper.committed(t), "available": paper.available(t),
            "equity_nzd": eq * fx, "started_nzd": paper.DEFAULT_EQUITY * fx,
            "fx_nzd_per_usd": fx,
        })
    grid = _grid()
    if grid:
        grid = dict(grid)
        grid["fx_nzd_per_usd"] = fx
    return {
        "coins": coins,
        "traders": traders,
        "grid": grid,
        "totals": {
            "open_positions": len(poss) + len(reasoner["positions"]) + len(extras),
        },
        "ts": time.time(),
    }


@app.get("/api/trades")
def api_trades(limit: int = 20):
    return _trades(limit)


@app.get("/api/pxhist")
def api_pxhist(coin: str = "SOL", limit: int = 200):
    return _pxhist(coin, limit)


@app.get("/api/klines")
def api_klines(coin: str = "SOL", limit: int = 200):
    return _pxhist(coin, limit)


@app.get("/api/strategies")
def api_strategies():
    return _strategies()


@app.get("/api/reasoner")
def api_reasoner():
    return _reasoner()


@app.get("/api/whales")
def api_whales():
    """Whale copy-trade layer: tracked wallets, whale_trader wallet/positions,
    recent whale events (on-chain buys) and whale_trader trade history."""
    c = _conn()
    wallets, events, positions, wtrades = [], [], [], []
    try:
        wallets = [dict(r) for r in c.execute(
            "SELECT * FROM mh_whale_wallets ORDER BY rank ASC").fetchall()]
    except Exception:
        pass
    try:
        events = [dict(r) for r in c.execute(
            "SELECT * FROM mh_whale_events ORDER BY id DESC LIMIT 20").fetchall()]
    except Exception:
        pass
    try:
        positions = [dict(r) for r in c.execute(
            "SELECT * FROM mh_whale_positions ORDER BY id DESC").fetchall()]
    except Exception:
        pass
    c.close()
    trades = _trades(1000)
    wtrades = [t for t in trades if t.get("setup") == "whale_trader"][:20]
    acc = next((t for t in paper.TRADERS if t == paper.TRADER_WHALE_TRADER), None)
    fx = pricefeed_module.nzd_per_usd() if pricefeed_module else 1.67
    acct = {
        "equity": paper.equity(TRADER_WHALE_ACC) if acc else 24.0,
        "committed": paper.committed(TRADER_WHALE_ACC) if acc else 0.0,
        "available": paper.available(TRADER_WHALE_ACC) if acc else 24.0,
        "fx": fx,
    }
    return {"wallets": wallets, "events": events, "positions": positions,
            "trades": wtrades, "account": acct}


@app.get("/api/memecoin")
def api_memecoin():
    """Memecoin paper trader wallet, open positions, recent signals, and trade history."""
    c = _conn()
    positions, signals, wtrades = [], [], []
    try:
        positions = [dict(r) for r in c.execute(
            "SELECT * FROM mh_memecoin_positions ORDER BY id DESC").fetchall()]
    except Exception:
        pass
    try:
        signals = [dict(r) for r in c.execute(
            "SELECT * FROM mh_news_bias WHERE provider='memecoin_tracker' "
            "ORDER BY id DESC LIMIT 20").fetchall()]
    except Exception:
        pass
    c.close()
    trades = _trades(1000)
    wtrades = [t for t in trades if t.get("setup") == "memecoin_trader"][:20]
    fx = pricefeed_module.nzd_per_usd() if pricefeed_module else 1.67
    acct = {
        "equity": paper.equity(paper.TRADER_MEMECOIN),
        "committed": paper.committed(paper.TRADER_MEMECOIN),
        "available": paper.available(paper.TRADER_MEMECOIN),
        "fx": fx,
    }
    return {"positions": positions, "signals": signals,
            "trades": wtrades, "account": acct}


TRADER_WHALE_ACC = "whale_trader"


@app.get("/api/livegate")
def api_livegate():
    trades = _trades(1000)
    out = {}
    for t in paper.TRADERS:
        if t == paper.TRADER_SCALPER:
            ct = [x for x in trades if x.get("setup") not in ("reasoner", "whale_trader", "memecoin_trader")]
        elif t == paper.TRADER_WHALE_TRADER:
            ct = [x for x in trades if x.get("setup") == "whale_trader"]
        elif t == paper.TRADER_MEMECOIN:
            ct = [x for x in trades if x.get("setup") == "memecoin_trader"]
        else:
            ct = [x for x in trades if x.get("setup") == "reasoner"]
        wins = sum(1 for x in ct if x.get("realized_pct", 0) > 0)
        n = len(ct)
        wr = wins / n if n else 0.0
        out[t] = {"n": n, "wins": wins, "losses": n - wins,
                  "win_rate": round(wr, 3),
                  "eligible": n >= 20 and wr >= 0.75,
                  "reason": ("READY" if (n >= 20 and wr >= 0.75)
                             else f"{n}/20 trades, wr {wr:.3f}")}
    return out


@app.get("/api/equity_curve")
def api_equity_curve():
    trades = _trades(1000)
    series = [t for t in trades if t.get("open_ts") is not None]
    series.sort(key=lambda x: x["open_ts"])
    cum = 0.0
    out = []
    for t in series:
        cum += t.get("realized_pct", 0) or 0
        out.append({"ts": t["open_ts"], "cum": round(cum, 6)})
    return out


@app.get("/api/exit_reason_series")
def api_exit_reason_series(trader: str = "all"):
    """Cumulative exit-count per exit reason, built from the real mh_trades
    close_ts (equity_curve style). trader= all|scalper|reasoner|grid. Grid has
    no exit_reason column, so its 'exits' are counted grid sells (closed
    cycles). Returns flat events: {ts, reason, cum}."""
    c = _conn()
    rows = []
    try:
        if trader == "grid":
            rows = [dict(r) for r in c.execute(
                "SELECT ts FROM grid_trades "
                "WHERE side='SELL' AND realized_usd IS NOT NULL "
                "ORDER BY ts ASC").fetchall()]
            c.close()
            out, cnt = [], 0
            for r in rows:
                cnt += 1
                out.append({"ts": r["ts"], "reason": "cycle", "cum": cnt})
            return out
        if trader == "scalper":
            cond = "AND setup NOT IN ('reasoner','whale_trader')"
        elif trader == "reasoner":
            cond = "AND setup = 'reasoner'"
        elif trader == "whale_trader":
            cond = "AND setup = 'whale_trader'"
        else:
            cond = ""
        rows = [dict(r) for r in c.execute(
            "SELECT exit_reason, close_ts FROM mh_trades "
            "WHERE exit_reason IS NOT NULL AND close_ts IS NOT NULL " + cond +
            " ORDER BY close_ts ASC").fetchall()]
    except Exception:
        rows = []
    c.close()
    counts = {}
    out = []
    for r in rows:
        reason = r["exit_reason"]
        counts[reason] = counts.get(reason, 0) + 1
        out.append({"ts": r["close_ts"], "reason": reason, "cum": counts[reason]})
    return out


@app.get("/api/grid")
def api_grid():
    g = _grid()
    # enrich with the db row set (grid_trader.status_json already has recent_trades)
    if g is None:
        return {"enabled": False}
    g = dict(g)
    g["fx_nzd_per_usd"] = pricefeed_module.nzd_per_usd() if pricefeed_module else 1.67
    g["starting_equity_usd"] = 24.0  # 40 NZD * 0.60 usd_per_nzd
    return g


_BINANCE_SYMBOL = "SOLUSDT"   # the grid trades SOL; use Binance SOLUSDT kline backdrop
_GRID_HIST_CACHE = {}          # interval -> (fetched_ts, bars)

@app.get("/api/grid_hist")
def api_grid_hist(interval: str = "15m", limit: int = 120):
    """Binance SOLUSDT candlestick backdrop for the Grid Ladder. Mirrors the
    interval strings used by the TradingView-style timeframe buttons. Cached
    briefly so the 15s auto-refresh doesn't hammer Binance."""
    iv = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1h", "4h": "4h",
          "d": "1d", "D": "1d", "1d": "1d", "1D": "1d",
          "w": "1w", "W": "1w", "1w": "1w",
          "m": "1M", "M": "1M", "1M": "1M"}.get(interval, "15m")
    now = time.time()
    hit = _GRID_HIST_CACHE.get(iv)
    if hit and now - hit[0] < 30:      # 30s TTL
        return {"symbol": _BINANCE_SYMBOL, "interval": iv, "bars": hit[1]}
    try:
        qs = urllib.parse.urlencode({"symbol": _BINANCE_SYMBOL, "interval": iv, "limit": min(int(limit) or 120, 500)})
        url = "https://api.binance.com/api/v3/klines?" + qs
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            raw = json.loads(r.read().decode())
        bars = [{
            "ts": int(k[0]) / 1000.0,   # ms -> s
            "open": float(k[1]), "high": float(k[2]),
            "low": float(k[3]), "close": float(k[4]),
        } for k in raw]
        _GRID_HIST_CACHE[iv] = (time.time(), bars)
        return {"symbol": _BINANCE_SYMBOL, "interval": iv, "bars": bars}
    except Exception as e:
        # fail soft: no bars -> ladder falls back to its level + last-px draw
        return {"symbol": _BINANCE_SYMBOL, "interval": iv, "bars": [], "error": str(e)}


@app.get("/api/edge_curve")
def api_edge_curve():
    """Per-trader cumulative edge (%): scalper + reasoner from realized_pct,
    grid from realized_usd normalized to % of its $24 USD starting equity.
    All three share the same axis. Returns {scalper:[...], reasoner:[...], grid:[...]}."""
    trades = _trades(2000)
    out = {"scalper": [], "reasoner": [], "whale_trader": [], "memecoin_trader": [], "grid": []}
    for label, pred in (("scalper", lambda t: t.get("setup") not in ("reasoner", "whale_trader", "memecoin_trader")),
                        ("reasoner", lambda t: t.get("setup") == "reasoner"),
                        ("whale_trader", lambda t: t.get("setup") == "whale_trader"),
                        ("memecoin_trader", lambda t: t.get("setup") == "memecoin_trader")):
        ser = [t for t in trades if pred(t) and t.get("close_ts") is not None]
        ser.sort(key=lambda x: x["close_ts"])
        c = _conn()
        row = c.execute("SELECT started_usd FROM mh_accounts WHERE trader=?",(label,)).fetchone()
        c.close()
        start = row[0] if row and row[0] > 0 else paper.DEFAULT_EQUITY
        cum = 0.0
        for t in ser:
            cum += t.get("realized_usd", 0) or 0
            out[label].append({"ts": t["close_ts"], "cum": round(cum / start * 100, 9)})
    # grid edge: cumulative realized_usd from grid_trades, expressed as % of $24 USD start
    c = _conn()
    try:
        rows = [dict(r) for r in c.execute(
            "SELECT ts, realized_usd FROM grid_trades "
            "WHERE realized_usd IS NOT NULL ORDER BY ts ASC").fetchall()]
    except Exception:
        rows = []
    c.close()
    cum = 0.0
    for r in rows:
        cum += r["realized_usd"]
        out["grid"].append({"ts": r["ts"], "cum": round(cum / 24.0 * 100, 6)})
    return out


@app.get("/api/market")
def api_market():
    """Live market widget feed: per coin, best-effort live USD price + recent
    pxhist (ts,px) sparkline data. Falls back to latest pxhist entry."""
    out = []
    for coin in COINS:
        # Serve the full retained pxhist window so the Market chart can bin
        # candles over real timeframes (1m..4h), not just a 120-pt tail.
        hist = _pxhist(coin, 100000)
        px = None
        try:
            px = hist[-1]["px"] if hist else None
        except Exception:
            px = None
        if px is None and hist:
            px = hist[-1]["px"]
        out.append({
            "symbol": coin, "price": px, "hist": hist, "stale": not hist or time.time()-hist[-1]["ts"] > 90, "price_ts":hist[-1]["ts"] if hist else None,
            "hist_span": hist[-1]["ts"] - hist[0]["ts"] if len(hist) >= 2 else 0,
            "hist_start": hist[0]["ts"] if hist else None,
            "hist_end": hist[-1]["ts"] if hist else None,
        })
    return out


@app.get("/api/exit_reasons")
def api_exit_reasons():
    c = _conn()
    rows = []
    try:
        rows = [dict(r) for r in c.execute(
            "SELECT exit_reason, COUNT(*) as n FROM mh_trades WHERE exit_reason IS NOT NULL "
            "GROUP BY exit_reason ORDER BY n DESC").fetchall()]
    except Exception:
        pass
    c.close()
    return rows


@app.get("/api/positions")
def api_positions():
    """Read-only view of open scalper positions (dashboard layer only)."""
    return _positions()


@app.get("/", response_class=HTMLResponse)
def index():
    return _html()


def _html() -> str:
    return r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MultiHedge &middot; Command</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk&family=Inter&family=IBM+Plex+Mono&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#0a1020;--surface:#141c33;--surface-2:#19233f;
  --border:rgba(180,150,255,.12);--border-strong:rgba(180,150,255,.24);
  --text:#f2eefb;--text-dim:#b9add4;--text-faint:#7a709c;
  --lav:#c7b2f0;--lav2:#e0d6fa;--lav-dim:rgba(199,178,240,.14);
  --red:#ff4d5e;--red2:#ff8066;--red-dim:rgba(255,77,94,.14);
}
*{margin:0;padding:0;box-sizing:border-box;}
body{font-family:'Inter',sans-serif;color:var(--text);
  background:
    radial-gradient(ellipse 800px 460px at 12% -8%, rgba(199,178,240,.10), transparent 60%),
    radial-gradient(ellipse 700px 460px at 92% 6%, rgba(255,77,94,.08), transparent 60%),
    var(--bg);
  min-height:100vh;padding:20px 20px 70px;}
.wrap{max-width:1440px;margin:0 auto}
.topnav{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap;padding:12px 18px;background:var(--surface);border:1px solid var(--border);border-radius:16px;}
.brand{display:flex;align-items:center;gap:10px}
.brand-mark{width:30px;height:30px;border-radius:9px;background:linear-gradient(135deg,var(--lav),#8a6ee0);display:flex;align-items:center;justify-content:center;font-family:'Space Grotesk';font-weight:700;color:#0a1020;font-size:15px;text-align:center;}
.brand-name{font-family:'Space Grotesk';font-weight:700;font-size:17px;color:var(--text);}
.tabs{display:flex;gap:4px;background:var(--surface-2);border-radius:11px;padding:4px;}
.tabs button{font:inherit;cursor:pointer;border:none;background:transparent;font-size:12.5px;font-weight:500;color:var(--text-faint);padding:7px 13px;border-radius:8px;white-space:nowrap;}
.tabs button.active{background:linear-gradient(135deg,var(--lav),#9a7ee6);color:#0a1020;font-weight:600;}
.nav-right{display:flex;align-items:center;gap:10px;flex-wrap:wrap;}
.status-pill{font-size:11px;font-weight:600;color:var(--lav2);background:var(--lav-dim);border:1px solid var(--border-strong);padding:6px 12px;border-radius:20px;display:flex;align-items:center;gap:6px;}
.status-pill .dot{width:6px;height:6px;border-radius:50%;background:var(--lav);box-shadow:0 0 8px var(--lav);animation:pulse 1.6s ease-in-out infinite;}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}
.ts{font-family:'IBM Plex Mono';font-size:10.5px;color:var(--text-faint);}
.ticker{overflow:hidden;border-radius:12px;border:1px solid var(--border);background:var(--surface);margin-bottom:12px;white-space:nowrap;padding:8px 0;}
.ticker-track{display:flex;gap:30px;animation:scroll 40s linear infinite;width:max-content;}
@keyframes scroll{from{transform:translateX(0)}to{transform:translateX(-50%)}}
.ticker-item{font-family:'IBM Plex Mono';font-size:12px;color:var(--text-dim);display:inline-flex;gap:6px;padding:0 8px;white-space:nowrap;}
.ticker-item b{color:var(--text);}.ticker-item .up{color:var(--lav);}.ticker-item .down{color:var(--red);}
.hero{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px;margin-bottom:16px;}
.kpi{background:var(--surface);border:1px solid var(--border);padding:14px 16px;border-radius:12px;}
.kpi .lbl{font-size:11px;color:var(--text-dim);letter-spacing:.12em;text-transform:uppercase;}
.kpi .val{font-family:'IBM Plex Mono';font-size:25px;font-weight:600;color:var(--lav);}
.kpi .val.neg{color:var(--red);}
.card{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:16px;margin-bottom:14px;}
.card h3{font-family:'IBM Plex Mono';font-size:13px;font-weight:600;color:var(--lav);text-transform:uppercase;letter-spacing:.12em;margin-bottom:10px;}
.grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));}
canvas{width:100%;height:130px;background:var(--surface-2);border:1px solid var(--border);border-radius:8px;}
#mkcv{height:min(56vh,520px);min-height:300px;}
#gld-cv{height:min(56vh,520px);min-height:300px;}
.tfbar{display:flex;gap:6px;flex-wrap:wrap;margin:10px 0 4px;}
.tfbar button{font:600 11px 'IBM Plex Mono';background:var(--surface);color:var(--text-dim);border:1px solid var(--border);border-radius:6px;padding:4px 9px;cursor:pointer;}
.tfbar button:hover{border-color:var(--lav);color:var(--text);}
.tfbar button.on{background:var(--lav);color:#120b18;border-color:var(--lav);}
.tfbar button:disabled{opacity:.35;cursor:not-allowed;}
table{width:100%;border-collapse:collapse;font-size:12.5px;}
th,td{padding:7px 10px;text-align:left;border-bottom:1px solid var(--border);}
th{color:var(--text-dim);font-weight:600;font-size:11px;text-transform:uppercase;}
.pos{color:var(--lav);}.neg{color:var(--red);}
.pilltag{display:inline-block;padding:2px 9px;border-radius:12px;font-size:11px;font-weight:600;}
.pilltag.ok{background:var(--lav-dim);color:var(--lav);border:1px solid var(--border-strong);}
.pilltag.no{background:var(--red-dim);color:var(--red);border:1px solid rgba(255,77,94,.35);}
.ok-tag{display:inline-block;padding:2px 9px;border-radius:12px;font-size:11px;font-weight:600;background:var(--lav-dim);color:var(--lav);}
.no-tag{display:inline-block;padding:2px 9px;border-radius:12px;font-size:11px;font-weight:600;background:var(--red-dim);color:var(--red);}
.scroll-wrap{max-height:190px;overflow-y:auto;border-radius:8px;border:1px solid var(--border);}
.scroll-wrap table thead th{position:sticky;top:0;background:var(--surface-2);z-index:2;}
.chart-flex{display:flex;gap:14px;align-items:flex-start;}
.chart-legend{min-width:170px;font:12px 'IBM Plex Mono';display:flex;flex-direction:column;gap:6px;}
.whats{font-size:12.5px;color:var(--text-dim);line-height:1.55;}
.whats b{color:var(--lav);font-family:'IBM Plex Mono';font-size:11.5px;letter-spacing:.05em;text-transform:uppercase;}
/* Mobile optimisations */
@media (max-width: 900px){
  .wrap{padding:10px;}
  .topnav{flex-wrap:wrap;gap:8px;padding:8px;}
  .tabs{overflow-x:auto;scroll-snap-type:x mandatory;-webkit-overflow-scrolling:touch;display:flex;flex-wrap:nowrap;gap:4px;justify-content:flex-start;padding:4px 0;}
  .tabs button{flex-shrink:0;min-width:80px;scroll-snap-align:start;padding:8px 12px;font-size:13px;}
  .tabs button.active{background:linear-gradient(135deg,var(--lav),#9a7ee6);color:#0a1020;font-weight:600;}
  .nav-right{flex-wrap:wrap;gap:8px;}
  .hero{grid-template-columns:1fr;gap:10px;margin-bottom:12px;}
  .kpi .val{font-size:20px;}
  .card{margin-bottom:10px;padding:12px;}
  .card h3{font-size:12px;margin-bottom:8px;}
  .chart-flex{flex-direction:column;}
  canvas{height:180px !important;}
  .grid{grid-template-columns:1fr;gap:10px;}
  table{font-size:11px;}
  th,td{padding:6px 8px;}
  .scroll-wrap{max-height:160px;}
  .tfbar{flex-wrap:wrap;}
  .tfbar button{padding:6px 10px;font-size:11px;}
  .ticker{display:none;}
  .status-pill{display:none;}
}
</style></head><body><div class="wrap">
<nav class="topnav">
  <div class="brand"><div class="brand-mark">Mh</div><div class="brand-name">MULTIHEDGE</div></div>
  <div class="tabs" id="tabNav">
    <button data-tab="overview">Overview</button>
    <button data-tab="strategies">Scalper</button>
    <button data-tab="reasoner">Reasoner</button>
    <button data-tab="whales">Whales</button>
    <button data-tab="memecoin">Memecoin</button>
    <button data-tab="grid">Grid</button>
    <button data-tab="market">Market</button>
    <button data-tab="gate">Gate</button>
    <button data-tab="survival" class="active">Xora-Survival</button>
  </div>
  <div class="nav-right"><span class="status-pill"><span class="dot"></span>PAPER</span><div class="ts" id="ts">--</div></div>
</nav>
<div class="ticker" id="ticker"></div>
<div class="hero" id="kpis"></div>
<div id="tab-panels"></div>
<div class="grid" id="coin-wrap"></div>
</div>
<script>
const COINS=['SOL','JUP','ETH'];
const TRADERS=['scalper','reasoner'];
let TAB='survival';
const fmt=n=>n===null||n===undefined||isNaN(n)?'-':Number(n).toLocaleString(undefined,{maximumFractionDigits:Number(n)<1?6:2});
const fmtMoney=n=>n===null||n===undefined||isNaN(n)?'-':'$'+Number(n).toLocaleString(undefined,{maximumFractionDigits:2});
const fmtTime=t=>{const d=new Date(t*1000);return d.toLocaleString([],{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});};
async function load(o){try{const r=await fetch('/api/'+o);return await r.json();}catch(e){return null;}}
function lineChart(id,series,color,fill){
  const cv=document.getElementById(id);if(!cv)return;
  const ctx=cv.getContext('2d');const w=cv.width,h=cv.height;ctx.clearRect(0,0,w,h);
  if(!series||series.length<2)return;
  const vals=series.map(p=>p.px!==undefined?p.px:p.cum!==undefined?p.cum:p).filter(v=>v!==null&&!isNaN(v));
  if(vals.length<2)return;
  const mn=Math.min(...vals),mx=Math.max(...vals),rng=(mx-mn)||1;
  ctx.strokeStyle=color||'#c7b2f0';ctx.lineWidth=1.6;ctx.beginPath();
  for(let i=0;i<vals.length;i++){const x=i/(vals.length-1)*w,y=h-2-((vals[i]-mn)/rng)*(h-26);i?ctx.lineTo(x,y):ctx.moveTo(x,y);}
  ctx.stroke();
  if(fill){ctx.save();ctx.globalAlpha=.12;ctx.lineTo(w,h-2);ctx.lineTo(0,h-2);ctx.closePath();ctx.fillStyle=color||'#c7b2f0';ctx.fill();ctx.restore();}
}
// Candlestick chart (theme: up=lavender, down=red). pxhist only stores single
// quote points, so we bucket them into OHLC candles: open=first px, close=last,
// high=max, low=min. Y = price, X = time. `tf` is the candle timeframe in
// seconds (60=1m .. 604800=W); candles are binned into aligned time windows.
const CANDLE_UP='#c7b2f0',CANDLE_DOWN='#ff4d5e';
function candleChart(id,hist,tf){
  const cv=document.getElementById(id);if(!cv)return;
  const ctx=cv.getContext('2d');const w=cv.width,h=cv.height;ctx.clearRect(0,0,w,h);
  const ps=(hist||[]).filter(p=>p&&p.px!=null).sort((a,b)=>a.ts-b.ts);
  if(ps.length<2)return;
  let candles;
  if(tf){
    // bin by aligned timeframe bucket (epoch floor / tf)
    const buckets=new Map();
    ps.forEach(p=>{const k=Math.floor(p.ts/tf);if(!buckets.has(k))buckets.set(k,[]);buckets.get(k).push(p);});
    const keys=[...buckets.keys()].sort((a,b)=>a-b);
    candles=keys.map(k=>{
      const g=buckets.get(k);
      const o=g[0].px,c=g[g.length-1].px;
      let hi=o,lo=o;g.forEach(pp=>{if(pp.px>hi)hi=pp.px;if(pp.px<lo)lo=pp.px;});
      return {o,c,hi,lo,t0:g[0].ts,t1:g[g.length-1].ts};
    });
    // cap at ~120 candles for readability on the wide windows
    if(candles.length>120)candles=candles.slice(candles.length-120);
  }else{
    const n=40,bucket=Math.max(1,Math.floor(ps.length/n));
    candles=[];
    for(let i=0;i<ps.length;i+=bucket){
      const grp=ps.slice(i,i+bucket);if(!grp.length)break;
      const o=grp[0].px,c=grp[grp.length-1].px;
      let hi=o,lo=o;grp.forEach(g=>{if(g.px>hi)hi=g.px;if(g.px<lo)lo=g.px;});
      candles.push({o,c,hi,lo,t0:grp[0].ts,t1:grp[grp.length-1].ts});
    }
  }
  let mn=Infinity,mx=-Infinity;
  candles.forEach(k=>{if(k.lo<mn)mn=k.lo;if(k.hi>mx)mx=k.hi;});
  const rng=(mx-mn)||1;
  const gx=46,gw=w-gx-8,gy=8,gh=h-30;
  const y=v=>gy+(1-(v-mn)/rng)*gh;
  // gridlines + y price labels
  ctx.font='10px "IBM Plex Mono"';ctx.fillStyle='rgba(255,255,255,.45)';
  [0,1,2,3,4].forEach(k=>{
    const val=mn+rng*k/4,yv=y(val);
    ctx.fillText(fmt(val),2,yv+3);
    ctx.strokeStyle='rgba(255,255,255,.05)';ctx.beginPath();ctx.moveTo(gx,yv);ctx.lineTo(w-8,yv);ctx.stroke();
  });
  // x time labels
  const t0=candles[0].t0,t1=candles[candles.length-1].t1;
  [[t0,0],[(t0+t1)/2,.5],[t1,1]].forEach(([t,f])=>ctx.fillText(fmtTime(t),gx+(gw-50)*f,h-8));
  // candles
  const bw=Math.max(2,(gw/candles.length)*0.62);
  candles.forEach((k,i)=>{
    const cx=gx+(i+0.5)*(gw/candles.length);
    const yc=y(k.c),yo=y(k.o),yh=y(k.hi),yl=y(k.lo);
    const up=k.c>=k.o,color=up?CANDLE_UP:CANDLE_DOWN;
    ctx.strokeStyle=color;ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(cx,yh);ctx.lineTo(cx,yl);ctx.stroke();
    ctx.fillStyle=color;
    const top=Math.min(yo,yc),bot=Math.max(yo,yc),bh=Math.max(1,bot-top);
    ctx.fillRect(cx-bw/2,top,bw,bh);
  });
}
// Exit-reason trends as a multi-coloured line graph rebuilt from REAL trade
// history via /api/exit_reason_series (Cumulative-Edge style): each reason's
// line rises from 0 as trades close, and redraws so it tracks live. Colours
// are contrasting accents of the theme; y-axis = cumulative count, x = time.
const ER_COLORS=['#c7b2f0','#5ec8d5','#ff4d5e','#f0b46a','#4ade80','#e07af7'];
// Cumulative edge over time, one line PER TRADER (scalper, reasoner, grid),
// all in % on a shared axis. data = {name: [{ts,cum}]} from /api/edge_curve.
function edgeChart(id,data){
  const cv=document.getElementById(id);if(!cv)return;
  const ctx=cv.getContext('2d');const w=cv.width,h=cv.height;ctx.clearRect(0,0,w,h);
  const names=Object.keys(data).filter(n=>(data[n]||[]).length>=1);
  if(!names.length)return;
  let mn=0,mx=0;
  names.forEach(n=>(data[n]||[]).forEach(p=>{if(p.cum<mn)mn=p.cum;if(p.cum>mx)mx=p.cum;}));
  const pad=((mx-mn)||1)*0.12;mn-=pad;mx+=pad;
  const t0=Math.min(...names.flatMap(n=>(data[n]||[]).map(p=>p.ts))),t1=Math.max(...names.flatMap(n=>(data[n]||[]).map(p=>p.ts)));
  const gx=46,gw=w-gx-8,gy=8,gh=h-30;
  const X=ts=>gx+(t1>t0?(ts-t0)/(t1-t0):0.5)*gw;
  const Y=v=>gy+(1-(v-mn)/((mx-mn)||1))*gh;
  ctx.font='10px "IBM Plex Mono"';ctx.fillStyle='rgba(255,255,255,.45)';
  [0,1,2,3,4].forEach(k=>{
    const val=mn+(mx-mn)*k/4,yv=Y(val);
    ctx.fillText((val>=0?'+':'')+val.toFixed(1)+'%',2,yv+3);
    ctx.strokeStyle='rgba(255,255,255,.06)';ctx.beginPath();ctx.moveTo(gx,yv);ctx.lineTo(w-8,yv);ctx.stroke();
  });
  [[t0,0],[(t0+t1)/2,.5],[t1,1]].forEach(([t,f])=>ctx.fillText(fmtTime(t),gx+(gw-46)*f,h-8));
  if(mn<0&&mx>0){ctx.strokeStyle='rgba(255,255,255,.14)';ctx.beginPath();ctx.moveTo(gx,Y(0));ctx.lineTo(w-8,Y(0));ctx.stroke();}
  const colors=ER_COLORS;
  names.forEach((n,ni)=>{
    const ser=data[n],color=colors[ni%colors.length];
    ctx.strokeStyle=color;ctx.lineWidth=1.7;ctx.beginPath();
    ser.forEach((p,i)=>{const x=X(p.ts),y=Y(p.cum);i?ctx.lineTo(x,y):ctx.moveTo(x,y);});
    ctx.stroke();
    ctx.fillStyle=color;
    ser.forEach(p=>{ctx.beginPath();ctx.arc(X(p.ts),Y(p.cum),2.1,0,7);ctx.fill();});
  });
  const lg=document.getElementById(id+'-legend');if(!lg)return;
  lg.innerHTML=names.map((n,ni)=>{
    const s=data[n],cur=s[s.length-1].cum,color=colors[ni%colors.length];
    return `<div style="display:flex;gap:8px;align-items:center">
      <span style="width:10px;height:10px;background:${color};border-radius:2px;display:inline-block"></span>
      <span style="flex:1;font-weight:600;color:${color}">${n}</span>
      <span style="color:var(--text-dim)">${cur>=0?'+':''}${cur.toFixed(2)}%</span></div>`;
  }).join('');
}
// Known scalper strategies with a one-line "what it does" each (for the Scalper tab).
const STRAT_INFO={
  momentum_breakout:'Buys a coin that breaks above its recent high on volume, riding the continuation for a quick 2.5% scalp.',
  mean_reversion:'Fades sharp spikes: buys when price stretches far above/below its moving average, betting on a snap-back.',
  rsi_oversold:'Watches RSI and buys when it dips into oversold territory, capturing the bounce to the 2.5% target.',
  vwap_reversion:'Re-enters when price trades well away from VWAP, buying the reversion to the volume-weighted mean.'
};
async function renderExitGraph(){
  const evs=await load('exit_reason_series')||[];
  exitChart('ercv',evs,'erlabels','erlegend');
}
setInterval(renderExitGraph,15000);
// Generalized exit-reason line chart. Reasons are drawn and listed MOST-TO-LEAST
// by latest cumulative count, but each reason keeps ONE stable accent colour
// (erColorCache) so lines don't change colour when their rank flips on the 15s
// redraw. Reused by Overview and every trader tab (ids passed in per tab).
const erColorCache={};
function erColor(reason){
  if(!(reason in erColorCache)){erColorCache[reason]=ER_COLORS[Object.keys(erColorCache).length%ER_COLORS.length];}
  return erColorCache[reason];
}
function exitChart(cvId,evs,labId,lgId){
  const cv=document.getElementById(cvId);if(!cv)return;
  const ctx=cv.getContext('2d');const w=cv.width,h=cv.height;ctx.clearRect(0,0,w,h);
  if(!evs||!evs.length){const lg=document.getElementById(lgId);if(lg)lg.innerHTML='';const lb=document.getElementById(labId);if(lb)lb.innerHTML='';return;}
  const ser={},order=[];
  evs.forEach(e=>{if(!(e.reason in ser)){ser[e.reason]=[0];order.push(e.reason);}ser[e.reason].push(e.cum);});
  // most-to-least by latest cumulative count
  order.sort((a,b)=>(ser[b][ser[b].length-1])-(ser[a][ser[a].length-1]));
  let mx=1;order.forEach(r=>ser[r].forEach(v=>{if(v>mx)mx=v;}));
  const gx=38,gw=w-gx-8,gy=8,gh=h-30;
  ctx.font='10px "IBM Plex Mono"';ctx.fillStyle='rgba(255,255,255,.45)';
  [0,1,2,3,4].forEach(k=>{
    const val=Math.round(mx*k/4),yv=gy+(1-k/4)*gh;
    ctx.fillText(String(val),2,yv+3);
    ctx.strokeStyle='rgba(255,255,255,.06)';ctx.beginPath();ctx.moveTo(gx,yv);ctx.lineTo(w-8,yv);ctx.stroke();
  });
  const t0=evs[0].ts,t1=evs[evs.length-1].ts;
  [[t0,0],[(t0+t1)/2,.5],[t1,1]].forEach(([t,f])=>ctx.fillText(fmtTime(t),gx+(gw-40)*f,h-8));
  order.forEach(r=>{
    const color=erColor(r);const pts=ser[r];const den=Math.max(1,pts.length-1);
    if(pts.length>1){
      ctx.strokeStyle=color;ctx.lineWidth=1.6;ctx.beginPath();
      pts.forEach((v,i)=>{const x=gx+i/den*gw,y=gy+(1-v/mx)*gh;i?ctx.lineTo(x,y):ctx.moveTo(x,y);});
      ctx.stroke();
    }
    ctx.fillStyle=color;pts.forEach((v,i)=>{const x=gx+i/den*gw,y=gy+(1-v/mx)*gh;ctx.beginPath();ctx.arc(x,y,2.2,0,7);ctx.fill();});
  });
  const lg=document.getElementById(lgId);
  // Compact single-line labels under the chart (no cramped rotated text).
  const lab=document.getElementById(labId);
  if(lab){
    const tot=order.reduce((a,r)=>a+ser[r][ser[r].length-1],0)||1;
    lab.innerHTML=`<div style="display:flex;justify-content:center;gap:14px;margin-top:4px;font:11px 'IBM Plex Mono'">`+
      order.map(r=>`<span style="color:${erColor(r)}">${r} ${ser[r][ser[r].length-1]} (${Math.round(ser[r][ser[r].length-1]/tot*100)}%)</span>`).join('')+`</div>`;
    if(lg)lg.innerHTML='';
    return;
  }
  if(lg)lg.innerHTML=order.map(r=>{
    const cur=ser[r][ser[r].length-1],color=erColor(r);
    return `<div style="display:flex;gap:8px;align-items:center">
      <span style="width:10px;height:10px;background:${color};border-radius:2px;display:inline-block"></span>
      <span style="flex:1;font-weight:600;color:${color}">${r}</span>
      <span style="color:var(--text-dim)">${cur}</span>
    </div>`;
  }).join('');
}
// Single-trader cumulative edge line (% from an /api/edge_curve slice).
function edgeLine(id,series,color,name){
  const cv=document.getElementById(id);if(!cv)return;
  const ctx=cv.getContext('2d');const w=cv.width,h=cv.height;ctx.clearRect(0,0,w,h);
  const lg=document.getElementById(id+'-legend');if(lg)lg.innerHTML='';
  if(!series||series.length<2)return;
  let mn=0,mx=0;series.forEach(p=>{if(p.cum<mn)mn=p.cum;if(p.cum>mx)mx=p.cum;});
  const pad=((mx-mn)||1)*0.12;mn-=pad;mx+=pad;
  const t0=series[0].ts,t1=series[series.length-1].ts;
  const gx=46,gw=w-gx-8,gy=8,gh=h-30;
  const X=ts=>gx+(t1>t0?(ts-t0)/(t1-t0):0.5)*gw;
  const Y=v=>gy+(1-(v-mn)/((mx-mn)||1))*gh;
  ctx.font='10px "IBM Plex Mono"';ctx.fillStyle='rgba(255,255,255,.45)';
  [0,1,2,3,4].forEach(k=>{
    const val=mn+(mx-mn)*k/4,yv=Y(val);
    ctx.fillText((val>=0?'+':'')+val.toFixed(1)+'%',2,yv+3);
    ctx.strokeStyle='rgba(255,255,255,.06)';ctx.beginPath();ctx.moveTo(gx,yv);ctx.lineTo(w-8,yv);ctx.stroke();
  });
  [[t0,0],[(t0+t1)/2,.5],[t1,1]].forEach(([t,f])=>ctx.fillText(fmtTime(t),gx+(gw-46)*f,h-8));
  if(mn<0&&mx>0){ctx.strokeStyle='rgba(255,255,255,.14)';ctx.beginPath();ctx.moveTo(gx,Y(0));ctx.lineTo(w-8,Y(0));ctx.stroke();}
  ctx.strokeStyle=color||'#c7b2f0';ctx.lineWidth=1.7;ctx.beginPath();
  series.forEach((p,i)=>{const x=X(p.ts),y=Y(p.cum);i?ctx.lineTo(x,y):ctx.moveTo(x,y);});ctx.stroke();
  ctx.fillStyle=color||'#c7b2f0';series.forEach(p=>{ctx.beginPath();ctx.arc(X(p.ts),Y(p.cum),2.1,0,7);ctx.fill();});
  if(lg){const cur=series[series.length-1].cum;
    lg.innerHTML=`<div style="display:flex;gap:8px;align-items:center">
      <span style="width:10px;height:10px;background:${color||'#c7b2f0'};border-radius:2px;display:inline-block"></span>
      <span style="flex:1;font-weight:600;color:${color||'#c7b2f0'}">${name||''}</span>
      <span style="color:var(--text-dim)">${cur>=0?'+':''}${cur.toFixed(2)}%</span></div>`;}
}
// Per-trader exit + edge charts, redrawn every 15s while its tab is open.
const traderCharts={scalper:{key:'sc'},reasoner:{key:'rn'},grid:{key:'gd'}};
async function drawTraderCharts(trader){
  const key=traderCharts[trader].key;
  const evs=await load('exit_reason_series?trader='+trader)||[];
  if(evs&&evs.length)exitChart(key+'-exit',evs,key+'-exitlab',key+'-exitlg');
  const eq=await load('edge_curve')||{};
  const ser=eq[trader]||[];
  if(ser.length)edgeLine(key+'-edge',ser,ER_COLORS[{scalper:0,reasoner:1,grid:2}[trader]],trader);
}
setInterval(()=>{
  const map={strategies:'scalper',reasoner:'reasoner',grid:'grid'};
  if(map[TAB]){drawTraderCharts(map[TAB]);if(map[TAB]==='grid')renderGridLadder();}
},15000);
function updateClocks(){const n=new Date();const el=document.getElementById('ts');if(el)el.textContent=n.toLocaleTimeString();}
setInterval(updateClocks,1000);updateClocks();
document.getElementById('tabNav').addEventListener('click',e=>{
  const b=e.target.closest('button');if(!b)return;
  TAB=b.dataset.tab;
  document.querySelectorAll('#tabNav button').forEach(x=>x.classList.toggle('active',x===b));
  run();
});
async function renderOverview(sum){
  const strat=await load('strategies')||[];
  const eq=await load('edge_curve')||{};
  const cw=document.getElementById('coin-wrap');
  // Positions feed for the trader/coin matrix
  const pos=await load('positions')||[];
  const rpos=((await load('reasoner'))||{}).positions||[];
  const allOpen=[...pos.map(p=>({src:'scalper',coin:p.coin,entry:p.entry_px,qty:p.qty,setup:p.setup})),
                 ...rpos.map(p=>({src:'reasoner',coin:(p.symbol||'').toUpperCase(),entry:p.entry,qty:p.qty,setup:p.entry_signal}))];
  // grid trader: SOL only; open exposure = cash + sol valued at last price
  const gsum=sum.grid||{};
  const gridPos=(gsum.sol_qty||0)>0?[{coin:'SOL',entry:null,qty:gsum.sol_qty}]:[];
  // per-trader closed W/L for the TOTAL W/L column
  const lg2=await load('livegate')||{};
  const wlTot=t=>{const v=lg2[t]||{};return `WIN: ${v.wins||0} / LOSE: ${v.losses||0}`;};
  // per-trader TP/SL (mirrors engine config: scalper paper.py TP 2.5/SL 1.5, reasoner config 1.5/1.5)
  const TPSL={scalper:{tp:0.025,sl:-0.015},reasoner:{tp:0.015,sl:-0.015}};
  const posCell=(trader,coinSym,ops)=>{
    const rows=ops.map(p=>{
      const e=p.entry||0,q=p.qty||0,entryUsd=e*q;
      return `<div style="margin-bottom:6px">
        ${fmtMoney(e)} (${fmt(q)} ${coinSym}) &middot; <b>${fmtMoney(entryUsd)}</b> &middot; <span class="pilltag ok">OPEN</span></div>`;
    }).join('')||'<span style="color:var(--text-faint)">flat</span>';
    return rows;
  };
  const wlCell=(trader,coinSym)=>{
    const c=sum.coins.find(x=>(x.symbol||'').toUpperCase()===coinSym)||{};
    const t=c[trader+'_trades']||0,w=c[trader+'_wins']||0,l=t-w;
    return `W= ${w} / L= ${l}`;
  };
  // Grid trader has no per-trader coin W/L or TP/SL levels: show its range frame as SL bound,
  // the upper sell levels as TP, and realized cycles as its W/L.
  const grid=sum.grid||{};
  const gWal=grid.wallet||{}; const gG=grid.grid||{};
  const gridEntry=(gWal.sol_qty>0)
    ?`<div style="margin-bottom:6px">${fmtMoney(gG.center_px)} (<b>${fmt(gWal.sol_qty)} SOL</b>) &middot; <b>${fmtMoney((gG.center_px||0)*gWal.sol_qty)}</b> &middot; <span class="pilltag ${gWal.paused?'no':'ok'}">${gWal.paused?'PAUSED':'ACTIVE'}</span>
       <div style="font-size:11px;color:var(--text-dim)">grid ${fmt(gG.range_low)}-${fmt(gG.range_high)} &middot; ${grid.open_sells||0} open sells</div></div>`
    :'<span style="color:var(--text-faint)">no SOL held</span>';
  const gridWl=`cycles ${grid.cycles_completed||0} &middot; realized ${fmtMoney(grid.realized_usd_total||0)}`;
  const rowsHtml=[
    ...TRADERS.map(t=>COINS.map(c=>{
      const ops=allOpen.filter(p=>p.src===t&&p.coin===c);
      const coinData=sum.coins.find(x=>(x.symbol||'').toUpperCase()===c)||{};
      const st=coinData.open_positions?'OPEN':'CLOSE';
      // pick the first open position's TP/SL for the per-position columns
      const o=ops[0];
      const tpSl=TPSL[t]||{tp:0.015,sl:-0.015};
      const e=(o&&o.entry)||0, side=((o&&o.side)||'LONG').toUpperCase();
      const tpPx=e?(side==='LONG'?e*(1+tpSl.tp):e*(1-tpSl.tp)):0;
      const slPx=e?(side==='LONG'?e*(1+tpSl.sl):e*(1+tpSl.sl)):0;
      return `<tr><td>${t}</td><td>${c}</td>
        <td>${posCell(t,c,ops)}</td>
        <td>${ops.length?fmtMoney((ops[0].qty||0)*(ops[0].entry||0)):'-'}</td>
        <td>${e?fmtMoney(tpPx):'-'}</td>
        <td>${e?fmtMoney(slPx):'-'}</td>
        <td>${ops.length?'<span class="pilltag ok">OPEN</span>':'<span class="pilltag no">CLOSE</span>'}</td>
        <td>${wlCell(t,c)}</td>
        <td>${wlTot(t)}</td></tr>`;
    }).join('')).join(''),
    `<tr><td style="color:var(--accent)"><b>grid</b></td><td>SOL</td>
      <td>${gridEntry}</td>
      <td>${fmtMoney((gG.center_px||0)*gWal.sol_qty)}</td>
      <td>${gG.range_high?fmtMoney(gG.range_high):'-'}</td>
      <td>${gG.range_low?fmtMoney(gG.range_low):'-'}</td>
      <td><span class="pilltag ${gWal.paused?'no':'ok'}">${gWal.paused?'PAUSED':'ACTIVE'}</span></td>
      <td>${gridWl}</td>
      <td>${grid.realized_usd_total>0?'WIN: 0 / LOSE: 0':'WIN: 0 / LOSE: 0'}</td></tr>`
  ].join('');
  cw.insertAdjacentHTML('beforeend',`<div class="card" style="grid-column:1/-1"><h3>Traders &times; Coins &middot; live positions &amp; W/L</h3>
    <table><tr><th>Trader</th><th>Coin</th><th>Entry position (coin amount)</th><th>Entry amount</th><th>TP</th><th>SL</th><th>Open/Close</th><th>W/L</th><th>Total W/L</th></tr>
    ${rowsHtml}
    </table>
    <div style="font-size:11px;color:var(--text-faint);margin-top:6px">TP/SL prices are derived from entry and the per-trader take-profit/stop-loss config (scalper 2.5%/1.5%, reasoner 1.5%/1.5%). Grid trader shows its range frame (SL=lower bound, TP=upper sell levels) and realized cycles as its W/L.</div>
    </div>`);
  const panels=document.getElementById('tab-panels');
  // Per-trader W/L ratios from livegate; exit reasons with vertical labels
  const lg=await load('livegate')||{};
  const wlRows=TRADERS.map(t=>{
    const v=lg[t]||{wins:0,losses:0,n:0,win_rate:0};
    return `<tr><td>${t}</td><td>${v.wins||0} : ${v.losses||0}</td><td class="${(v.win_rate||0)>=0.5?'pos':'neg'}">${((v.win_rate||0)*100).toFixed(1)}%</td><td>${v.n||0}</td></tr>`;
  }).join('');
  // grid has no livegate ratio: show cycles as closed count and realized P&L in place of W/L
  const ggg=sum.grid||{};
  const gridWlRow=`<tr><td>grid</td><td>-</td><td>-</td><td>${ggg.cycles_completed||0} cycles</td></tr>`;
  panels.innerHTML=`
  <div class="grid">
    <div class="card"><h3>Cumulative Edge &middot; per trader</h3>
      <div class="chart-flex"><canvas id="eqcv" width="560" height="220"></canvas>
      <div id="eqcv-legend" class="chart-legend"></div></div></div>
    <div class="card"><h3>Win / Loss Mix &middot; ratio per trader</h3>
      <table><tr><th>Trader</th><th>W : L</th><th>Win%</th><th>Closed</th></tr>${wlRows}${gridWlRow}</table></div>
    <div class="card"><h3>Exit reasons</h3>
      <div style="display:flex;gap:14px;align-items:flex-start">
        <div style="position:relative"><canvas id="ercv" width="560" height="220"></canvas>
        <div id="erlabels" style="position:absolute;left:0;top:0;width:100%;height:100%;pointer-events:none"></div></div>
        <div id="erlegend" style="min-width:180px;font:12px 'IBM Plex Mono';display:flex;flex-direction:column;gap:6px"></div>
      </div></div>
  </div>`;
  setTimeout(()=>edgeChart('eqcv',eq),80);
  setTimeout(()=>renderExitGraph(),100);
}
function tradeRows(trades){
  return trades.map(t=>{
    const pl=t.realized_usd||0,cls=(pl>=0?'pos':'neg'),pct=t.realized_pct!=null?t.realized_pct:null;
    return `<tr><td>${t.coin||t.symbol||'-'}</td><td>${(t.side||'').toUpperCase()}</td>
      <td>${t.entry_px!=null?fmt(t.entry_px):'-'}</td><td>${t.exit_px!=null?fmt(t.exit_px):'-'}</td>
      <td class="${cls}">${pct!=null?(pct>0?'+':'')+(pct*100).toFixed(2)+'%':'-'}</td>
      <td class="${cls}">${fmtMoney(pl)}</td>
      <td>${t.exit_reason||'-'}</td><td>${t.close_ts?fmtTime(t.close_ts):'-'}</td></tr>`;
  }).join('')||'<tr><td colspan="8" style="color:var(--text-faint)">no trades yet</td></tr>';
}
async function renderScalper(){
  const s=await load('strategies')||[];
  if(TAB!=='scalper')return;
  const p=document.getElementById('tab-panels');
  const trades=(await load('trades?limit=30'))||[];
  const st=trades.filter(t=>t.setup!=='reasoner'&&t.setup!=='whale_trader');
  const strats=[...new Set(s.map(x=>x.setup))];
  const what=strats.map(ss=>`<div style="margin-bottom:8px"><b>${ss}</b><br>${STRAT_INFO[ss]||'Short-horizon setup with a quick 2.5% take-profit / 1.5% stop-loss scalp target.'}</div>`).join('')||'<div>Rotates between several short-horizon setups with a 2.5% take-profit / 1.5% stop-loss.</div>';
  p.innerHTML=`
  <div class="card"><h3>Scalper &middot; fast momentum layer &middot; own wallet</h3>
    <div style="font-size:12px;color:var(--text-dim)">own wallet &middot; scalps on 2.5% take-profit / 1.5% stop-loss, ~1h max hold</div></div>
  <div class="card"><h3>What it does</h3>
    <div class="whats">The fast, high-frequency layer of the bot. It rotates between several short-horizon strategies and enters whenever the active setup fires, always aiming for a quick 2.5% scalp with a 1.5% stop-loss cap. Breakdown of the strategies it switches between:<br><br>${what}</div></div>
  <div class="card"><h3>Scalper wallet &middot; strategy rotations</h3><table><tr><th>Coin</th><th>Setup</th><th>Pts</th><th>W/L</th><th>W%</th></tr>
  ${s.map(x=>`<tr><td>${x.coin}</td><td>${x.setup}</td><td>${x.points}</td><td>${x.wins}/${x.losses}</td><td class="${x.win_rate>=0.5?'pos':'neg'}">${(x.win_rate*100).toFixed(1)}%</td></tr>`).join('')}</table></div>
  <div class="grid">
    <div class="card"><h3>Exit reasons &middot; scalper</h3>
      <div style="position:relative"><canvas id="sc-exit" width="560" height="220"></canvas><div id="sc-exitlab" style="position:absolute;left:0;top:0;width:100%;height:100%;pointer-events:none"></div></div>
      <div id="sc-exitlg" style="margin-top:8px;font:12px 'IBM Plex Mono';display:flex;flex-direction:column;gap:6px"></div></div>
    <div class="card"><h3>Cumulative edge &middot; scalper</h3>
      <div class="chart-flex"><canvas id="sc-edge" width="560" height="220"></canvas><div id="sc-edge-legend" class="chart-legend"></div></div></div>
  </div>
  <div class="card"><h3>Recent trades</h3><div class="scroll-wrap"><table><tr><th>Coin</th><th>Side</th><th>Entry</th><th>Exit</th><th>P/L%</th><th>P/L$</th><th>Reason</th><th>Close</th></tr>${tradeRows(st)}</table></div></div>`;
  setTimeout(()=>drawTraderCharts('scalper'),80);
}
async function renderReasoner(){
  const r=await load('reasoner')||{accounts:[],positions:[],bias:{}};
  if(TAB!=='reasoner')return;
  const p=document.getElementById('tab-panels');
  const acc=(r.accounts||[])[0]||{};
  const rows=COINS.map(coin=>{
    const b=(r.bias||{})[coin];const po=(r.positions||[]).filter(x=>(x.symbol||'').toUpperCase()===coin)[0];
    const posTxt=po?(po.side+' @ '+fmt(po.entry)):'-';
    const dirCls=b&&b.direction==='UP'?'pos':b&&b.direction==='DOWN'?'neg':'';
    return `<tr><td>${coin}</td><td class="${dirCls}">${b?b.direction:'-'}</td><td>${b?((b.confidence||0)*100).toFixed(0)+'%':'-'}</td><td>${posTxt}</td></tr>`;
  }).join('');
  const trades=(await load('trades?limit=15'))||[];
  const rt=trades.filter(t=>t.setup==='reasoner');
  p.innerHTML=`
  <div class="card"><h3>Reasoner &middot; slow news-swing &middot; own wallet</h3></div>
  <div class="card"><h3>What it does</h3>
    <div style="font-size:12.5px;color:var(--text-dim);line-height:1.55">A slow, news-driven swing trader with its own wallet. Reads per-coin news bias from the news daemon, takes directional positions with a 1.5% take-profit / 1.5% stop-loss, holds up to ~2h, and trails its stop once price moves in its favour. Only enters when its confidence clears the threshold, so it trades far less often than the scalper.</div></div>
  <div class="card"><h3>Reasoner wallet</h3>
    <div style="font-size:12px;color:var(--text-dim);margin-bottom:8px">Reasoner wallet: NZ${fmtMoney((acc.equity||0)*(acc.fx||1.67))} (avail NZ${fmtMoney((acc.available||0)*(acc.fx||1.67))}, committed NZ${fmtMoney((acc.committed||0)*(acc.fx||1.67))})</div>
    <table><tr><th>Coin</th><th>Bias</th><th>Conf</th><th>Pos</th></tr>${rows}</table></div>
  <div class="grid">
    <div class="card"><h3>Exit reasons &middot; reasoner</h3>
      <div style="position:relative"><canvas id="rn-exit" width="560" height="220"></canvas><div id="rn-exitlab" style="position:absolute;left:0;top:0;width:100%;height:100%;pointer-events:none"></div></div>
      <div id="rn-exitlg" style="margin-top:8px;font:12px 'IBM Plex Mono';display:flex;flex-direction:column;gap:6px"></div></div>
    <div class="card"><h3>Cumulative edge &middot; reasoner</h3>
      <div class="chart-flex"><canvas id="rn-edge" width="560" height="220"></canvas><div id="rn-edge-legend" class="chart-legend"></div></div></div>
  </div>
  <div class="card"><h3>Recent trades</h3><div class="scroll-wrap"><table><tr><th>Coin</th><th>Side</th><th>Entry</th><th>Exit</th><th>P/L%</th><th>P/L$</th><th>Reason</th><th>Close</th></tr>${tradeRows(rt)}</table></div></div>`;
  setTimeout(()=>drawTraderCharts('reasoner'),80);
}
async function renderWhales(){
  const w=await load('whales')||{};
  if(TAB!=='whales')return;
  const p=document.getElementById('tab-panels');
  const fx=(w.account&&w.account.fx)||1.67;
  const acc=w.account||{};
  const eqN=(acc.equity||0)*fx, comN=(acc.committed||0)*fx, avN=(acc.available||0)*fx;
  const wallets=(w.wallets||[]);
  const events=(w.events||[]);
  const positions=(w.positions||[]);
  const trades=(w.trades||[]);
  // wallet liveness: green if seen within last hour
  const now=Date.now()/1000;
  const walletRows=wallets.map(v=>{
    const age=v.last_sig_ts?(now-v.last_sig_ts):null;
    const live=age!=null&&age<3600;
    const last=age!=null?(age<3600?(age/60).toFixed(0)+'m':(age/3600).toFixed(1)+'h'):'-';
    const addrShort=v.address?v.address.slice(0,6)+'\u2026'+v.address.slice(-4):'-';
    return `<tr><td><b>${v.name}</b> <span style="color:var(--text-faint)">#${v.rank}</span></td>
      <td>@${v.handle||'-'}</td>
      <td style="font:11px 'IBM Plex Mono';color:var(--text-dim)" title="${v.address||''}">${addrShort}</td>
      <td><span class="pilltag ${live?'ok':'no'}">${live?'ACTIVE':'IDLE'}</span></td>
      <td>${last} ago</td><td>${v.tx_7d!=null?v.tx_7d:'-'}</td></tr>`;
  }).join('')||'<tr><td colspan="6" style="color:var(--text-faint)">wallet table warming up (30s sweeps)</td></tr>';
  const eventRows=events.map(e=>`<tr><td>${e.name}</td><td>${e.symbol}</td>
<td class="pos">${e.direction}</td><td>${fmtTime(e.ts)}</td>
    <td style="font:10.5px 'IBM Plex Mono';color:var(--text-faint)">${(e.sig||'').slice(0,16)}\u2026</td></tr>`
  ).join('')||'<tr><td colspan="5" style="color:var(--text-faint)">no whale buys seen yet</td></tr>';
  const posRows=positions.map(p2=>{
    const pct0=p2.entry?(((((p2.entry*p2.qty)))/(p2.entry*p2.qty)-1)*0):0;
    return `<tr><td>${p2.symbol}</td><td>${p2.side} @ ${fmt(p2.entry)}</td>
      <td>${fmt(p2.qty)}</td><td><span class="pilltag ok">OPEN</span></td></tr>`;
  }).join('')||'<tr><td colspan="4" style="color:var(--text-faint)">no open whale positions</td></tr>';
  const tradeRowsW=trades.map(t=>{
    const pl=t.realized_usd||0,cls=(pl>=0?'pos':'neg'),pct=t.realized_pct;
    return `<tr><td>${t.symbol||t.coin}</td><td>${(t.side||'').toUpperCase()}</td>
      <td>${fmt(t.entry_px)}</td><td>${fmt(t.exit_px)}</td>
      <td class="${cls}">${pct!=null?(pct>0?'+':'')+(pct*100).toFixed(2)+'%':'-'}</td>
      <td class="${cls}">${fmtMoney(pl)}</td><td>${(t.exit_reason||'').replace('sig:','signal#')}</td>
      <td>${t.close_ts?fmtTime(t.close_ts):'-'}</td></tr>`;
  }).join('')||'<tr><td colspan="8" style="color:var(--text-faint)">no whale trades yet</td></tr>';
  p.innerHTML=`
  <div class="card"><h3>Whales &middot; fomo.family copy-trade &middot; own wallet</h3>
    <div style="font-size:12px;color:var(--text-dim)">The bot mirrors the recent on-chain buys (sampled, last-15) of 5 curated wallets. When a tracked wallet buys SOL/JUP/ETH on-chain, the whale trader opens a LONG on that coin from its own wallet (5% TP / 2% SL / 6h max hold, kill switch at 20% drawdown).</div></div>
  <div class="card"><h3>Tracked wallets &middot; ${wallets.length}/5 curated (activity = last-15 sample)</h3>
    <table><tr><th>Name</th><th>Handle</th><th>Solana wallet</th><th>Status</th><th>Last tx</th><th>Tx 7d</th></tr>${walletRows}</table></div>
  <div class="grid">
    <div class="card"><h3>Whale trader wallet</h3>
      <div style="display:flex;gap:22px;flex-wrap:wrap">
        <div><div style="font-size:11px;color:var(--text-dim)">Equity</div><div style="font-size:20px;font-weight:700" class="${(acc.equity||0)>=24?'pos':'neg'}">NZ${fmtMoney(eqN)}</div></div>
        <div><div style="font-size:11px;color:var(--text-dim)">Committed</div><div style="font-size:18px;font-weight:600">NZ${fmtMoney(comN)}</div></div>
        <div><div style="font-size:11px;color:var(--text-dim)">Available</div><div style="font-size:18px;font-weight:600">NZ${fmtMoney(avN)}</div></div>
      </div>
      <div style="font-size:10px;color:var(--text-faint);margin-top:6px">= US$${fmtMoney(acc.equity||0)} held as USDC (fx ${fx.toFixed(3)})</div></div>
    <div class="card"><h3>Open whale positions</h3>
      <table><tr><th>Coin</th><th>Position</th><th>Qty</th><th>Status</th></tr>${posRows}</table></div>
  </div>
  <div class="card"><h3>Recent whale buys &middot; on-chain</h3>
    <div class="scroll-wrap"><table><tr><th>Whale</th><th>Coin</th><th>Dir</th><th>When</th><th>Signature</th></tr>${eventRows}</table></div></div>
  <div class="card"><h3>Whale trader &middot; recent trades</h3>
    <div class="scroll-wrap"><table><tr><th>Coin</th><th>Side</th><th>Entry</th><th>Exit</th><th>P/L%</th><th>P/L$</th><th>Reason</th><th>Close</th></tr>${tradeRowsW}</table></div></div>`;
}
async function renderMemecoin(){
  const m=await load('memecoin')||{};
  if(TAB!=='memecoin')return;
  const p=document.getElementById('tab-panels');
  const fx=(m.account&&m.account.fx)||1.67;
  const acc=m.account||{};
  const eqN=(acc.equity||0)*fx, comN=(acc.committed||0)*fx, avN=(acc.available||0)*fx;
  const positions=(m.positions||[]);
  const signals=(m.signals||[]);
  const trades=(m.trades||[]);
  const posRows=positions.map(p2=>`<tr><td>${p2.symbol}</td><td>${p2.side} @ ${fmt(p2.entry)}</td>
    <td>${fmt(p2.qty)}</td><td><span class="pilltag ok">OPEN</span></td></tr>`
  ).join('')||'<tr><td colspan="4" style="color:var(--text-faint)">no open memecoin positions</td></tr>';
  const sigRows=signals.map(s=>`<tr><td>${s.symbol}</td><td>${s.direction}</td>
    <td>${s.confidence!=null?(s.confidence*100).toFixed(0)+'%':'-'}</td>
    <td>${(s.rationale||'').replace('whale memecoin buy event','sig#')}</td>
    <td>${fmtTime(s.ts)}</td></tr>`
  ).join('')||'<tr><td colspan="5" style="color:var(--text-faint)">no memecoin signals yet</td></tr>';
  const tradeRows=trades.map(t=>{
    const pl=t.realized_usd||0,cls=(pl>=0?'pos':'neg'),pct=t.realized_pct;
    return `<tr><td>${t.symbol||t.coin}</td><td>${(t.side||'').toUpperCase()}</td>
      <td>${fmt(t.entry_px)}</td><td>${fmt(t.exit_px)}</td>
      <td class="${cls}">${pct!=null?(pct>0?'+':'')+(pct*100).toFixed(2)+'%':'-'}</td>
      <td class="${cls}">${fmtMoney(pl)}</td><td>${(t.exit_reason||'').replace('sig:','signal#')}</td>
      <td>${t.close_ts?fmtTime(t.close_ts):'-'}</td></tr>`;
  }).join('')||'<tr><td colspan="8" style="color:var(--text-faint)">no memecoin trades yet</td></tr>';
  p.innerHTML=`
  <div class="card"><h3>Memecoin &middot; long-only paper sniper &middot; own wallet</h3>
    <div style="font-size:12px;color:var(--text-dim)">Snipes fresh memecoin signals from the pump monitor. Strict guardrails: $5 max per trade, +50% TP, -20% SL, 15-minute max hold, trailing stop at +30%/-10%. Long-only.</div></div>
  <div class="grid">
    <div class="card"><h3>Memecoin wallet</h3>
      <div style="display:flex;gap:22px;flex-wrap:wrap">
        <div><div style="font-size:11px;color:var(--text-dim)">Equity</div><div style="font-size:20px;font-weight:700" class="${(acc.equity||0)>=24?'pos':'neg'}">NZ${fmtMoney(eqN)}</div></div>
        <div><div style="font-size:11px;color:var(--text-dim)">Committed</div><div style="font-size:18px;font-weight:600">NZ${fmtMoney(comN)}</div></div>
        <div><div style="font-size:11px;color:var(--text-dim)">Available</div><div style="font-size:18px;font-weight:600">NZ${fmtMoney(avN)}</div></div>
      </div>
      <div style="font-size:10px;color:var(--text-faint);margin-top:6px">= US$${fmtMoney(acc.equity||0)} held as USDC (fx ${fx.toFixed(3)})</div></div>
    <div class="card"><h3>Open memecoin positions</h3>
      <table><tr><th>Coin</th><th>Position</th><th>Qty</th><th>Status</th></tr>${posRows}</table></div>
  </div>
  <div class="card"><h3>Recent memecoin signals &middot; pump monitor</h3>
    <div class="scroll-wrap"><table><tr><th>Token</th><th>Dir</th><th>Conf</th><th>Source</th><th>When</th></tr>${sigRows}</table></div></div>
  <div class="card"><h3>Memecoin trader &middot; recent trades</h3>
    <div class="scroll-wrap"><table><tr><th>Coin</th><th>Side</th><th>Entry</th><th>Exit</th><th>P/L%</th><th>P/L$</th><th>Reason</th><th>Close</th></tr>${tradeRows}</table></div></div>`;
}
async function renderGate(){
  const g=await load('livegate')||{};
  if(TAB!=='gate')return;
  const gi=await load('grid')||{};
  const p=document.getElementById('tab-panels');
  const gridRow=(gi.enabled===false)?'':`<tr><td>grid</td><td>${gi.cycles_completed||0} cycles</td><td class="pos">-</td><td><span class="pilltag no">PAPER</span></td></tr>`;
  p.innerHTML=`<div class="card"><h3>Live Gate &middot; real wallet untouched until a trader passes &ge;75% win-rate over &ge;20 closed trades</h3><table><tr><th>Trader</th><th>Closed trades</th><th>Win%</th><th>Eligibility</th></tr>
  ${Object.entries(g).map(([t,v])=>`<tr><td>${t}</td><td>${v.n} / 20</td><td class="${v.win_rate>=0.75?'pos':'neg'}">${(v.win_rate*100).toFixed(1)}%</td><td title="${v.reason||''}"><span class="pilltag ${v.eligible?'ok':'no'}">${v.eligible?'READY FOR LIVE':'NOT ELIGIBLE'}</span><div style="font-size:10.5px;color:var(--text-faint);margin-top:2px">${v.reason||''}</div></td></tr>`).join('')}
  ${gridRow}
  </table>
  <div style="font-size:11px;color:var(--text-faint);margin-top:6px">Grid is spot long-only on its own paper wallet and never touches the real wallet, so it is not part of the go-live gate.</div></div>`;
}
async function renderGrid(){
  const g=await load('grid')||{};
  if(TAB!=='grid')return;
  const p=document.getElementById('tab-panels');
  if(g.enabled===false){p.innerHTML=`<div class="card"><h3>Grid</h3><div style="font-size:13px;color:var(--text-dim)">Grid trader is disabled.</div></div>`;return;}
  const st=g.grid||{};const w=g.wallet||{};
  const eq=g.equity_usd||0;
  const fx=g.fx_nzd_per_usd||1.67;
  const eqN=eq*fx,cashN=(w.cash_usd||0)*fx,realN=(g.realized_usd_total||0)*fx;
  const paused=!!w.paused;
  const trades=(g.recent_trades||[]).map(t=>`<tr><td>${t.side}</td><td>${fmt(t.level_px)}</td><td>${fmt(t.qty)} SOL</td><td>NZ${fmtMoney((t.usd||0)*fx)}</td><td>${t.cycle_id||'-'}</td><td class="${(t.realized_usd||0)>=0?'pos':'neg'}">${t.realized_usd!=null&&t.realized_usd!==0?'NZ'+fmtMoney(t.realized_usd*fx):'-'}</td></tr>`).join('')||'<tr><td colspan="6" style="color:var(--text-faint)">no trades yet</td></tr>';
  p.innerHTML=`
  <div class="card"><h3>Grid &middot; SOL &middot; spot long-only geometric grid</h3>
    <div style="font-size:12px;color:var(--text-dim)">own wallet &middot; <span class="pilltag ${paused?'no':'ok'}">${paused?'PAUSED (kill switch)':'ACTIVE'}</span></div></div>
  <div class="card" id="gld-wrap" style="grid-column:1/-1"></div>
  <div class="card"><h3>What it does</h3>
    <div style="font-size:12.5px;color:var(--text-dim);line-height:1.55">A spot long-only grid trader for SOL with its own wallet. Places buy orders on a 9-level geometric ladder around a centre price and sells back into strength, closing one full cycle per buy-sell pair. When price leaves the range it resets the grid (with hysteresis and a cooldown). Protected by a 20% max-drawdown kill switch and a flash-crash guard.</div></div>
  <div class="card"><h3>Grid wallet</h3>
    <div style="display:flex;gap:22px;flex-wrap:wrap;margin-bottom:12px">
      <div><div style="font-size:11px;color:var(--text-dim)">Equity (marked)</div><div style="font-size:20px;font-weight:700" class="${eq>=24?'pos':'neg'}">NZ${fmtMoney(eqN)}</div></div>
      <div><div style="font-size:11px;color:var(--text-dim)">Cash</div><div style="font-size:18px;font-weight:600">NZ${fmtMoney(cashN)}</div></div>
      <div><div style="font-size:11px;color:var(--text-dim)">SOL held</div><div style="font-size:18px;font-weight:600">${fmt(w.sol_qty)}</div></div>
      <div><div style="font-size:11px;color:var(--text-dim)">Center / Range</div><div style="font-size:15px;font-weight:600">${fmt(st.center_px)} &middot; ${fmt(st.range_low)}-${fmt(st.range_high)}</div></div>
      <div><div style="font-size:11px;color:var(--text-dim)">Open sells</div><div style="font-size:18px;font-weight:600">${g.open_sells||0}</div></div>
      <div><div style="font-size:11px;color:var(--text-dim)">Cycles</div><div style="font-size:18px;font-weight:600">${g.cycles_completed||0}</div></div>
      <div><div style="font-size:11px;color:var(--text-dim)">Realized P&amp;L</div><div style="font-size:18px;font-weight:600" class="${(g.realized_usd_total||0)>=0?'pos':'neg'}">NZ${fmtMoney(realN)}</div></div>
    </div></div>
  <div class="grid">
    <div class="card"><h3>Closed cycles &middot; grid (exits)</h3>
      <div style="position:relative"><canvas id="gd-exit" width="560" height="220"></canvas><div id="gd-exitlab" style="position:absolute;left:0;top:0;width:100%;height:100%;pointer-events:none"></div></div>
      <div id="gd-exitlg" style="margin-top:8px;font:12px 'IBM Plex Mono';display:flex;flex-direction:column;gap:6px"></div>
      <div style="font-size:11px;color:var(--text-faint);margin-top:6px">Grid has no exit-reason column; each sold level with realized P&amp;L counts as one closed cycle.</div></div>
    <div class="card"><h3>Cumulative edge &middot; grid</h3>
      <div class="chart-flex"><canvas id="gd-edge" width="560" height="220"></canvas><div id="gd-edge-legend" class="chart-legend"></div></div></div>
  </div>
  <div class="card"><h3>Recent trades</h3><div class="scroll-wrap"><table><tr><th>Side</th><th>Level</th><th>Qty</th><th>USD</th><th>Cycle</th><th>Realized</th></tr>${trades}</table></div></div>`;
  setTimeout(()=>drawTraderCharts('grid'),80);
  setTimeout(()=>renderGridLadder(),90);
}
async function renderMarket(){
  const m=await load('market')||[];
  if(TAB!=='market')return;
  const p=document.getElementById('tab-panels');
  if(!m||!m.length){p.innerHTML=`<div class="card"><h3>Live Market</h3><div style="font-size:13px;color:var(--text-dim)">no market data</div></div>`;return;}
  let selected=(document.getElementById('mkt-coin')||{value:m[0].symbol}).value;
  const coin=m.find(x=>x.symbol===selected)||m[0];
  selected=coin.symbol;
  const hist=coin.hist||[];
  const px=coin.price;
  const pxs=hist.filter(p=>p&&p.px!=null).map(p=>p.px);
  const hi=pxs.length?Math.max(...pxs):px,lo=pxs.length?Math.min(...pxs):px;
  const chg=pxs.length>=2&&pxs[0]?((px-pxs[0])/pxs[0]*100):null;
  // TradingView-style timeframes (seconds). Which are usable depends on the
  // span of retained history actually served by /api/market.
  const TFS=[['1m',60],['5m',300],['15m',900],['1h',3600],['4h',14400],['D',86400],['W',604800],['M',2592000]];
  if(typeof MKT_TF==='undefined')MKT_TF=900; // default 15m
  const span=(coin.hist_span)||(hist.length>=2?hist[hist.length-1].ts-hist[0].ts:0);
  const mkTfButtons=TFS.map(([l,s])=>{
    const ok=span>=2*s;
    const on=(MKT_TF===s);
    return `<button data-tf="${s}" ${on?'class="on"':''} ${ok?'':'disabled title="needs ~'+fmt(2*s/3600)+'h of history"'} style="${ok?'':'cursor:not-allowed;opacity:.35'}">${l}</button>`;
  }).join('');
  p.innerHTML=`<div class="card"><h3>Live Market</h3>
    <div style="margin-bottom:10px;display:flex;align-items:center;gap:14px;flex-wrap:wrap">
      <label style="font-size:12px;color:var(--text-dim)">Coin</label>
      <select id="mkt-coin" style="background:var(--panel);color:var(--text);border:1px solid var(--border-strong);border-radius:8px;padding:6px 10px;font:600 13px 'IBM Plex Mono'">${m.map(c=>`<option value="${c.symbol}" ${c.symbol===selected?'selected':''}>${c.symbol}</option>`).join('')}</select>
      <span style="font-size:26px;font-weight:700;color:var(--lav)">$${fmt(px)}</span>
      ${chg!=null?`<span class="${chg>=0?'pos':'neg'}" style="font-weight:600">${chg>=0?'+':''}${chg.toFixed(2)}%</span>`:''}
      <span style="font-size:11px;color:var(--text-dim)">H $${fmt(hi)} &middot; L $${fmt(lo)}</span>
    </div>
    <div class="tfbar">${mkTfButtons}</div>
    <canvas id="mkcv" width="920" height="420"></canvas>
    <div style="font-size:10.5px;color:var(--text-faint);margin-top:6px">Timeframe ${span>0?'over '+fmt(span/3600)+'h retained history':'n/a'} &middot; greyed-out timeframes need more history than is retained on this box.</div></div>`;
  document.getElementById('mkt-coin').addEventListener('change',renderMarket);
  document.querySelectorAll('#mkt-coin ~ .tfbar button').forEach(b=>{
    b.addEventListener('click',()=>{if(b.disabled)return;MKT_TF=+b.dataset.tf;renderMarket();});
  });
  setTimeout(()=>candleChart('mkcv',hist,MKT_TF),60);
}
// ---- Mini live market widget (Overview) + live grid ladder ----
// Namespaced ids (mkt-*, gld-*) so they never collide with the trader charts.
async function renderMiniMarket(){
  const m=await load('market')||[]; if(!m||!m.length)return;
  const cw=document.getElementById('coin-wrap'); if(!cw)return;
  let wrap=document.getElementById('mkt-wrap');
  if(!wrap){cw.insertAdjacentHTML('beforeend','<div class="card" id="mkt-wrap" style="grid-column:1/-1"></div>');wrap=document.getElementById('mkt-wrap');}
  const cards=m.slice(0,COINS.length).map(c=>{
    const pxs=(c.hist||[]).map(p=>p&&p.px).filter(v=>v!=null);
    const chg=pxs.length>=2&&pxs[0]?((c.price-pxs[0])/pxs[0]*100):null;
    return `<div style="display:flex;flex-direction:column;gap:6px"><div style="display:flex;justify-content:space-between;align-items:center">
        <b style="font:600 12px 'IBM Plex Mono'">${c.symbol}</b>
        ${chg!=null?`<span class="${chg>=0?'pos':'neg'}" style="font-weight:600">${chg>=0?'+':''}${chg.toFixed(2)}%</span>`:''}</div>
      <div style="font-size:19px;font-weight:700;color:var(--lav)">$${fmt(c.price)}</div>
      <canvas id="mkt-${c.symbol}" width="150" height="40"></canvas></div>`;
  }).join('');
  wrap.innerHTML=`<h3>Live Market &middot; tick</h3><div style="display:grid;grid-template-columns:repeat(3,1fr);gap:16px">${cards}</div>`;
  m.slice(0,COINS.length).forEach(c=>{
    const cv=document.getElementById('mkt-'+c.symbol); if(!cv)return;
    const pxs=(c.hist||[]).map(p=>p&&p.px).filter(v=>v!=null);
    const ctx=cv.getContext('2d'),w=cv.width,h=cv.height;ctx.clearRect(0,0,w,h);
    if(pxs.length<2)return;
    const mn=Math.min(...pxs),mx=Math.max(...pxs),rng=(mx-mn)||1;
    ctx.strokeStyle=pxs[pxs.length-1]>=pxs[0]?'#4ade80':'#ffb4a2';
    ctx.lineWidth=1.4;ctx.beginPath();
    pxs.forEach((v,i)=>{const x=i/(pxs.length-1)*w,y=h-3-((v-mn)/rng)*(h-6);i?ctx.lineTo(x,y):ctx.moveTo(x,y);});
    ctx.stroke();
  });
}
// Geometric-grid ladder: levels from range_low..range_high, center marked, live price as a line.
// New: Binance SOLUSDT candles (via /api/grid_hist) drawn over the ladder at the selected
// timeframe, with a union Y-range that keeps both candles and grid bands visible.
function ladderLevels(lo,hi,n){
  const out=[];for(let i=0;i<n;i++)out.push(lo*Math.pow(hi/lo,i/(n-1)));return out;
}
const GLD_TFS=[['1m','1m',60],['5m','5m',300],['15m','15m',900],['1h','1h',3600],['4h','4h',14400],['D','1d',86400],['W','1w',604800],['M','1M',2592000]];
async function renderGridLadder(){
  const g=await load('grid')||{}; if(!g||g.enabled===false)return;
  const st=g.grid||{}; if(!st.center_px)return;
  const cw=document.getElementById('coin-wrap'); if(!cw)return;
  let wrap=document.getElementById('gld-wrap');
  if(typeof GLD_TF==='undefined')GLD_TF='15m';
  const tfIdx=GLD_TFS.findIndex(x=>x[0]===GLD_TF); const tf=GLD_TFS[Math.max(0,tfIdx)];
  const tfbar=GLD_TFS.map(([l])=>`<button data-gtf="${l}" ${l===GLD_TF?'class="on"':''}>${l}</button>`).join('');
  const html=`<div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px">
      <h3 style="margin:0">Grid Ladder &middot; live price vs levels</h3>
      ${st.last_px!=null?`<div style="font-size:12px;color:var(--text-dim)">last <b>$${fmt(st.last_px)}</b> &middot; center <b>$${fmt(st.center_px)}</b> &middot; ${g.open_sells||0} open sells &middot; candles: Binance ${tf[0]} ${tf[1]}</div>`:''}
    </div>
    <div class="tfbar">${tfbar}</div>
    <canvas id="gld-cv" width="920" height="420"></canvas>`;
  if(!wrap){cw.insertAdjacentHTML('beforeend','<div class="card" id="gld-wrap" style="grid-column:1/-1">'+html+'</div>');wrap=document.getElementById('gld-wrap');}
  else wrap.innerHTML=html;
  document.querySelectorAll('#gld-wrap .tfbar button').forEach(b=>{
    b.onclick=()=>{GLD_TF=b.dataset.gtf;renderGridLadder();};
  });
  // fetch Binance backdrop for the selected timeframe
  let bars=[];
  try{ const h=await load('grid_hist?interval='+tf[1]); bars=h&&h.bars?h.bars:[]; }catch(e){ bars=[]; }
  const cv=document.getElementById('gld-cv'); if(!cv)return;
  const ctx=cv.getContext('2d'),w=cv.width,h=cv.height;ctx.clearRect(0,0,w,h);
  const lo=st.range_low||0,hi=st.range_high||0,cx=st.center_px||0,last=st.last_px!=null?st.last_px:cx;
  if(!(hi>lo))return;
  // union Y-range: candles + grid band + last
  let mn=Math.min(lo,cx),mx=Math.max(hi,cx);
  bars.forEach(b=>{if(b.low<mn)mn=b.low;if(b.high>mx)mx=b.high;});
  mn=Math.min(mn,last);mx=Math.max(mx,last);
  const padY=(mx-mn)*0.06||1;mn-=padY;mx+=padY;
  const pad=32;const q=(nv)=>(pad+((mx-nv)/(mx-mn))*(h-2*pad));
  const gx=70,gw=w-gx-10,gy=8,gh=h-40;
  // grid y gridlines + price labels
  ctx.font='9px "IBM Plex Mono"';ctx.fillStyle='rgba(255,255,255,.4)';
  [0,1,2,3].forEach(k=>{
    const val=mn+(mx-mn)*k/3,yv=pad+(h-2*pad)*k/3;
    ctx.strokeStyle='rgba(255,255,255,.05)';ctx.beginPath();ctx.moveTo(0,yv);ctx.lineTo(w,yv);ctx.stroke();
    ctx.fillText(fmt(val),2,yv-2);
  });
  // candle chart area
  if(bars.length){
    const bw=Math.max(2,(gw/bars.length)*0.62);
    const Y=v=>gy+(1-(v-mn)/((mx-mn)||1))*gh, X=i=>gx+(i+0.5)*(gw/bars.length);
    bars.forEach((b,i)=>{
      const up=b.close>=b.open,color=up?'#c7b2f0':'#ff4d5e';
      ctx.strokeStyle=color;ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(X(i),Y(b.high));ctx.lineTo(X(i),Y(b.low));ctx.stroke();
      ctx.fillStyle=color;
      const top=Math.min(Y(b.open),Y(b.close)),bot=Math.max(Y(b.open),Y(b.close)),bh=Math.max(1,bot-top);
      ctx.fillRect(X(i)-bw/2,top,bw,bh);
    });
    // x time labels
    ctx.font='9px "IBM Plex Mono"';ctx.fillStyle='rgba(255,255,255,.45)';
    const b0=bars[0],bN=bars[bars.length-1],mid=bars[Math.floor(bars.length/2)];
    ctx.fillText(fmtTime(b0.ts),pad,h-14);
    ctx.fillText(fmtTime(mid.ts),pad+(gw-60)/2,h-14);
    ctx.textAlign='right';ctx.fillText(fmtTime(bN.ts),w,h-14);ctx.textAlign='left';
  } else {
    ctx.font='11px "IBM Plex Mono"';ctx.fillStyle='rgba(255,255,255,.35)';
    ctx.fillText('Binance history unavailable · showing levels + live price',pad+8,pad+30);
  }
  // grid level lines (drawn over candles)
  const lv=ladderLevels(lo,hi,(g.grid_levels&&g.grid_levels>=4?g.grid_levels+1:9));
  lv.forEach((lv0,i)=>{
    const s=lv0>=cx, y=q(lv0);
    ctx.strokeStyle=s?'rgba(74,222,128,.6)':'rgba(255,93,162,.55)';
    ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(0,y);ctx.lineTo(w-6,y);ctx.stroke();
    ctx.fillStyle='rgba(255,255,255,.5)';ctx.font='bold 9px "IBM Plex Mono"';
    ctx.fillText(s?'SELL':'buy',4,y-2);
    ctx.textAlign='right';ctx.fillText('$'+fmt(lv0),w-4,y-2);ctx.textAlign='left';
  });
  // center
  ctx.strokeStyle='#c7b2f0';ctx.setLineDash([4,4]);ctx.lineWidth=1.4;
  ctx.beginPath();ctx.moveTo(0,q(cx));ctx.lineTo(w-6,q(cx));ctx.stroke();ctx.setLineDash([]);
  ctx.fillStyle='#c7b2f0';ctx.font='600 10px "IBM Plex Mono"';ctx.fillText('center $'+fmt(cx),4,q(cx)-8);
  // current price marker
  const yy=q(last);
  ctx.fillStyle=last>=cx?'#4ade80':'#ff5d9b';ctx.font='600 10px "IBM Plex Mono"';
  ctx.fillText('▲ $'+fmt(last),w-70,yy-6);
  ctx.fillRect(w-6,yy-1.5,4,3);
}
// auto-refresh market + ladder while Overview is open
setInterval(()=>{if(TAB==='overview'){renderMiniMarket();}},8000);
function renderKpis(sum){
  const k=document.getElementById('kpis');
  const traders=(sum.traders||[]);
  const sc=traders.find(t=>t.trader==='scalper')||{};
  const rn=traders.find(t=>t.trader==='reasoner')||{};
  const fx=sc.fx_nzd_per_usd||1.67;
  const gv=sum.grid?fmtMoney((sum.grid.equity_usd||0)*fx):'-';
  // Wallet widget detail lines: each wallet card carries its own full read-out.
  const pct=t=>t.started>0?((t.equity-t.started)/t.started*100):0;
  const traderDetail=t=>`<div style="font-size:11px;color:var(--text-dim);margin-top:6px;line-height:1.5">
    <div>started NZ${fmtMoney(t.started_nzd)} &middot; <span class="${pct(t)>=0?'pos':'neg'}">${pct(t)>=0?'+':''}${pct(t).toFixed(2)}%</span> vs start</div>
    <div>Equity <b>NZ${fmtMoney(t.equity_nzd)}</b> &middot; committed NZ${fmtMoney((t.committed||0)*fx)} &middot; avail NZ${fmtMoney((t.available||0)*fx)}</div>
    <div style="font-size:10px;color:var(--text-faint)">= US$${fmtMoney(t.equity)} held as USDC (fx ${fx.toFixed(3)})</div></div>`;
  const gridDetail=()=>{const g=sum.grid||{};const w=g.wallet||{};const gr=g.grid||{};const gfx=g.fx_nzd_per_usd||1.67;const paused=!!w.paused;
    return `<div style="font-size:11px;color:var(--text-dim);margin-top:6px;line-height:1.5">
      <div>own wallet &middot; spot long-only geometric grid &middot; <span class="pilltag ${paused?'no':'ok'}">${paused?'PAUSED':'ACTIVE'}</span></div>
      <div>Equity <b>NZ${fmtMoney((g.equity_usd||0)*gfx)}</b> &middot; cash NZ${fmtMoney((w.cash_usd||0)*gfx)} &middot; SOL ${fmt(w.sol_qty)}</div>
      <div style="font-size:10px;color:var(--text-faint)">center ${fmt(gr.center_px)} &middot; range ${fmt(gr.range_low)}-${fmt(gr.range_high)} &middot; open sells ${g.open_sells||0} &middot; cycles ${g.cycles_completed||0} &middot; realized NZ${fmtMoney((g.realized_usd_total||0)*gfx)}</div></div>`;
  };
  const kpi=(lbl,val,detail)=>`<div class="kpi"><div class="lbl">${lbl}</div><div class="val">${val}</div>${detail||''}</div>`;
  const op=kpi('Open Positions',sum.totals.open_positions);
  let h;
  if(TAB==='strategies')h=kpi('Scalper Wallet','NZ'+fmtMoney(sc.equity_nzd),traderDetail(sc))+op;
  else if(TAB==='reasoner')h=kpi('Reasoner Wallet','NZ'+fmtMoney(rn.equity_nzd),traderDetail(rn))+op;
  else if(TAB==='whales'){const wh=traders.find(t=>t.trader==='whale_trader')||{};
    h=kpi('Whale Trader Wallet','NZ'+fmtMoney(wh.equity_nzd),traderDetail(wh))+op;}
  else if(TAB==='memecoin'){const mc=traders.find(t=>t.trader==='memecoin_trader')||{};
    h=kpi('Memecoin Wallet','NZ'+fmtMoney(mc.equity_nzd),traderDetail(mc))+op;}
  else if(TAB==='grid')h=kpi('Grid Wallet',gv,gridDetail())+op;
  else h=kpi('Scalper Wallet','NZ'+fmtMoney(sc.equity_nzd),traderDetail(sc))+
           kpi('Reasoner Wallet','NZ'+fmtMoney(rn.equity_nzd),traderDetail(rn))+
           (()=>{const wh=traders.find(t=>t.trader==='whale_trader')||{};
             return kpi('Whale Trader Wallet','NZ'+fmtMoney(wh.equity_nzd),traderDetail(wh));})()+
           (()=>{const mc=traders.find(t=>t.trader==='memecoin_trader')||{};
             return kpi('Memecoin Wallet','NZ'+fmtMoney(mc.equity_nzd),traderDetail(mc));})()+
           kpi('Grid Wallet',gv,gridDetail())+op;
  k.innerHTML=h;
}
function renderTicker(sum){
  const tk=document.getElementById('ticker');
  const traders=(sum.traders||[]);
  const items=COINS.map(c=>`<span class="ticker-item"><b>${c}</b> ${(sum.coins.find(x=>x.symbol===c)||{}).open_positions||0} open</span>`).join('');
  const titems=traders.map(t=>`<span class="ticker-item"><b>${t.trader}</b> NZ${fmtMoney(t.equity_nzd)}</span>`).join('');
  tk.innerHTML='<div class="ticker-track">'+titems+items+titems+items+'</div>';
}
function exitReasonPill(reason){
  if(reason==='take_profit')return '<span class="pilltag ok">TAKE PROFIT</span>';
  if(reason==='stop_loss')return '<span class="pilltag no">STOP LOSS</span>';
  if(reason==='max_hold')return '<span class="pilltag ok">MAX HOLD</span>';
  if(reason==='trail_stop')return '<span class="pilltag ok">TRAIL STOP</span>';
  return `<span class="pilltag">${reason||'-'}</span>`;
}
async function renderSurvival(){
  const s=await load('survival');
  if(!s)return;
  if(TAB!=='survival')return;
  const p=document.getElementById('tab-panels');
  const ed=s.edge||{};
  const nzfx=1.67;
  const esc=(ed.paper_net_usd||0)>=0?'pos':'neg';
  const netNzd=(ed.paper_net_usd||0)*nzfx;
  // Inventory rows: live = real wallet fills, paper = shadow incubator.
  const invRow=(it,kind)=>{
    const amt=kind==='live'?(it.amount_atomic||0)/Math.pow(10,it.decimals||0):(it.qty||0);
    return `<tr><td>${it.ticker||it.mint||'-'}</td>
      <td style="font:10px 'IBM Plex Mono';color:var(--text-faint)" title="${it.mint||''}">${(it.mint||'').slice(0,8)}&hellip;</td>
      <td>${fmt(amt)}</td><td>${fmt(it.entry_usd)}</td>
      <td>${fmtMoney(it.entry_usd*amt)}</td>
      <td class="${(it.peak_usd||0)>=(it.entry_usd||0)?'pos':'neg'}">${fmt(it.peak_usd)}</td>
      <td><span class="pilltag ${kind==='live'?'ok':'no'}">${kind==='live'?'LIVE':'PAPER'}</span></td></tr>`;
  };
  const liveRows=(s.live_positions||[]).map(x=>invRow(x,'live')).join('')
    ||'<tr><td colspan="7" style="color:var(--text-faint)">no live fills yet &middot; wallet stays untouched until the strategy qualifies over 50 closed paper trades at &ge;66.7%</td></tr>';
  const paperRows=(s.paper_positions||[]).map(x=>invRow(x,'paper')).join('')
    ||'<tr><td colspan="7" style="color:var(--text-faint)">no open paper positions</td></tr>';
  // Wash signature for display (full sig shown in tooltip).
  const sigShort=sig=>sig?`<span style="font:10px 'IBM Plex Mono';color:var(--text-faint)" title="${sig}">${sig.slice(0,10)}&hellip;</span>`:'-';
  // REAL on-chain fill history (live). One row per settled mainnet tx.
  const liveTr=(s.live_trades||[]).map(t=>{
    let detail={};
    try{detail=(typeof t.details==='string')?JSON.parse(t.details):(t.details||{});}catch(e){}
    const out=detail.out_atomic;
    const side=(t.side||'').toUpperCase();
    // coins in live_logs are display tickers here (SOL/JUP), not mints.
    const token=t.coin||t.symbol||'-';
    return `<tr><td>${token}</td><td>${side}</td>
      <td>${fmtTime(t.ts)}</td>
      <td>${t.signature?sigShort(t.signature):'-'}</td>
      <td class="${(detail.state||'').toUpperCase()==='RECONCILED'?'pos':'no'}">${detail.state||'-'}</td>
      <td style="color:var(--text-dim)">${detail.order_id||'-'}</td></tr>`;
  }).join('')||'<tr><td colspan="6" style="color:var(--text-faint)">no on-chain fills on the survival wallet yet</td></tr>';
  // Paper history (shadow incubator) — explicitly labelled, never presented as live.
  const paperTr=(s.paper_trades||[]).slice(0,40).map(t=>{
    const pl=t.realized_usd||0,cls=(pl>=0?'pos':'neg'),pct=t.realized_pct;
    return `<tr><td>${t.symbol||t.coin||'-'}</td><td>${(t.side||'').toUpperCase()}</td>
      <td>${t.entry_px!=null?fmt(t.entry_px):'-'}</td><td>${t.exit_px!=null?fmt(t.exit_px):'-'}</td>
      <td class="${cls}">${pct!=null?(pct>0?'+':'')+(pct*100).toFixed(2)+'%':'-'}</td>
      <td class="${cls}">${fmtMoney(pl*1.67)}</td>
      <td>${exitReasonPill(t.exit_reason)}</td><td>${t.close_ts?fmtTime(t.close_ts):'-'}</td><td><span class="pilltag">PAPER</span></td></tr>`;
  }).join('')||'<tr><td colspan="9" style="color:var(--text-faint)">no closed paper incubator trades yet</td></tr>';
  // Decision log (newest first).
  const cyc=(s.cycles||[]).slice(0,25).map(c=>{
    const d=c.decision||{};
    const act=d.action||'-';
    const actCls=act==='BUY'?'ok':act==='SELL'?'no':'';
    const sym=(d.symbol||'').slice(0,12);
    const when=c.ts?fmtTime(c.ts):'-';
    const reason=c.reason||'-';
    return `<tr><td>${when}</td><td><span class="pilltag ${actCls}">${act}</span></td>
      <td style="font:10.5px 'IBM Plex Mono'">${sym}</td>
      <td class="${c.state==='HOLD'?'no':'pos'}">${c.state||'-'}</td>
      <td style="color:var(--text-dim)">${reason}</td></tr>`;
  }).join('')||'<tr><td colspan="5" style="color:var(--text-faint)">decision log empty</td></tr>';
  const byreason=(s.exit_reasons||{});
  const reasonRows=Object.entries(byreason).map(([r,n])=>
    `<tr><td>${exitReasonPill(r)}</td><td style="font:13px 'IBM Plex Mono'">${n}</td></tr>`
  ).join('')||'<tr><td colspan="2" style="color:var(--text-faint)">no paper exits recorded</td></tr>';
  const notional=s.notional||{paper_usd:0,live_usd:0};
  const liveN=ed.live_fills||0, liveC=ed.live_closes||0;
  p.innerHTML=`
  <div class="grid">
    <div class="card"><h3>Live wallet &middot; Xora-Survival (real on-chain fills)</h3>
      <div style="display:flex;gap:22px;flex-wrap:wrap">
        <div><div class="lbl" style="font-size:11px;color:var(--text-dim)">On-chain fills</div><div style="font-size:24px;font-weight:600;color:var(--text)">${ed.live_n||0}</div></div>
        <div><div class="lbl" style="font-size:11px;color:var(--text-dim)">Buys</div><div style="font-size:20px;font-weight:600" class="${liveN?"pos":''}">${liveN}</div></div>
        <div><div class="lbl" style="font-size:11px;color:var(--text-dim)">Closes/Sells</div><div style="font-size:20px;font-weight:600">${liveC}</div></div>
      </div>
      <div style="font-size:10.5px;color:var(--text-faint);margin-top:8px">live notional <b>${fmtMoney(notional.live_usd)}</b> &middot; real wallet fills only (no paper). When the strategy clears the gate this is where the survival wallet acts.</div></div>
    <div class="card"><h3>Paper incubator &middot; shadow (go-live gate)</h3>
      <div style="display:flex;gap:22px;flex-wrap:wrap">
        <div><div class="lbl" style="font-size:11px;color:var(--text-dim)">Paper net</div><div class="val ${esc}" style="font-size:24px">${netNzd>=0?'+':''}${fmtMoney(Math.abs(netNzd))}</div></div>
        <div><div class="lbl" style="font-size:11px;color:var(--text-dim)">Closed</div><div style="font-size:24px;font-weight:600;color:var(--text)">${ed.paper_n||0}</div></div>
        <div><div class="lbl" style="font-size:11px;color:var(--text-dim)">W : L</div><div style="font-size:24px;font-weight:600" class="${(ed.paper_win_rate||0)>=0.5?'pos':'neg'}">${ed.paper_wins||0} : ${ed.paper_losses||0}</div></div>
        <div><div class="lbl" style="font-size:11px;color:var(--text-dim)">Win rate</div><div style="font-size:24px;font-weight:600" class="${(ed.paper_win_rate||0)>=0.667?'pos':'neg'}">${((ed.paper_win_rate||0)*100).toFixed(1)}%</div></div>
      </div>
      <div style="font-size:10.5px;color:var(--text-faint);margin-top:8px">Go-live gate: needs ${ed.paper_n||0}/50 closed paper trades at &ge;66.7% win rate and positive net &middot; paper notional <b>${fmtMoney(notional.paper_usd)}</b></div></div>
  </div>
  <div class="grid">
    <div class="card"><h3>Open paper incubator positions</h3><table><tr><th>Ticker</th><th>Mint</th><th>Qty</th><th>Entry</th><th>Notional</th><th>Peak</th><th>Mode</th></tr>${paperRows}</table></div>
    <div class="card"><h3>Open live wallet positions</h3><table><tr><th>Ticker</th><th>Mint</th><th>Qty</th><th>Entry</th><th>Notional</th><th>Peak</th><th>Mode</th></tr>${liveRows}</table></div>
  </div>
  <div class="card"><h3>Exit reasons &middot; paper incubator</h3><table><tr><th>Reason</th><th>Count</th></tr>${reasonRows}</table></div>
  <div class="card"><h3>Active exit params &middot; auto-tuned from history</h3><table><tr><th>Class</th><th>Take profit</th><th>Stop loss</th><th>Max hold</th><th>Source</th></tr>
    ${((s.risk_params||{}).MEME?'<tr><td><span class="pilltag no">MEME</span></td><td>'+(s.risk_params.MEME.take_profit_pct*100).toFixed(0)+'%</td><td>'+(s.risk_params.MEME.stop_loss_pct*100).toFixed(0)+'%</td><td>'+Math.round(s.risk_params.MEME.max_hold_seconds/60)+' min</td><td>'+(s.risk_params.MEME.source||'default')+'</td></tr>':'')}
    ${((s.risk_params||{}).SERIOUS?'<tr><td><span class="pilltag ok">SERIOUS</span></td><td>'+(s.risk_params.SERIOUS.take_profit_pct*100).toFixed(0)+'%</td><td>'+(s.risk_params.SERIOUS.stop_loss_pct*100).toFixed(0)+'%</td><td>'+Math.round(s.risk_params.SERIOUS.max_hold_seconds/3600*10)/10+' h</td><td>'+(s.risk_params.SERIOUS.source||'default')+'</td></tr>':'')}
  </table><div style="font-size:10.5px;color:var(--text-faint);margin-top:6px">The autonomous autotuner rewrites these from real closed-trade history (per-trade peak/trough/hold) once &ge;30 closed trades per class exist and a candidate strictly beats the incumbent out-of-sample. Until then defaults apply.</div></div>
  <div class="card"><h3>Survival &middot; live trade history (on-chain)</h3><div class="scroll-wrap"><table><tr><th>Coin</th><th>Side</th><th>When</th><th>Signature</th><th>State</th><th>Order</th></tr>${liveTr}</table></div></div>
  <div class="card"><h3>Paper incubator &middot; trade history</h3><div class="scroll-wrap"><table><tr><th>Coin</th><th>Side</th><th>Entry</th><th>Exit</th><th>P/L%</th><th>P/L$ (NZD)</th><th>Reason</th><th>Close</th><th>Mode</th></tr>${paperTr}</table></div></div>
  <div class="card"><h3>Decision log &middot; autonomous cycles</h3><div class="scroll-wrap"><table><tr><th>When</th><th>Action</th><th>Asset</th><th>State</th><th>Reason</th></tr>${cyc}</table></div></div>`;
}
async function run(){
  const sum=await load('summary');if(!sum)return;
  renderKpis(sum);renderTicker(sum);
  const cw=document.getElementById('coin-wrap');
  if(TAB==='overview'){await renderMiniMarket();await renderOverview(sum);}
  else{
    cw.innerHTML=''; // trader/market/gate tabs show only their own wallet (in-panel), never the other wallets
    if(TAB==='strategies')await renderScalper();
    else if(TAB==='reasoner')await renderReasoner();
    else if(TAB==='whales')await renderWhales();
    else if(TAB==='memecoin')await renderMemecoin();
    else if(TAB==='grid')await renderGrid();
    else if(TAB==='market')await renderMarket();
    else if(TAB==='gate')await renderGate();
    else if(TAB==='survival')await renderSurvival();
  }
}
run();
</script></body></html>"""