"""
MultiHedge DASHBOARD — FastAPI, AutoHedge-style design recolored to
lavender-tulip / dark-navy / red, generalized across multiple coins.

Reads multihedge.db (paper ledger, strategy rotation, reasoner, news bias).
DB path: $MULTIHEDGE_DB or default /app/multihedge.db.

Run: uvicorn mh_dash:app --host 0.0.0.0 --port 9052
"""
import os
import re
import sqlite3
import time
import json
from datetime import datetime, timedelta
from pathlib import Path
import urllib.parse
import urllib.request

from fastapi import Body, FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

import paper
import pricefeed as pricefeed_module
import mh_ui

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
            "starting_equity_usd": _survival_start_equity(),
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


@app.get("/api/xora/summary")
def api_xora_summary():
    """Widget summary: live Xora-Survival stats (wallet, edge, gate, history)
    plus the latest council verdict. Composes existing calls — no extra cron:
    /api/survival already carries stats/scope/history for Xora-Survival."""
    survival = api_survival()
    edge = survival.get("edge") or {}
    wallet = _survival_wallet()
    risk = survival.get("risk_params") or {}
    exit_reasons = survival.get("exit_reasons") or {}
    paper_trades = survival.get("paper_trades") or []
    live_trades = survival.get("live_trades") or []
    # Compact history digest (last 25 closed paper + live fills)
    history = []
    for t in paper_trades[:25]:
        history.append({
            "kind": "paper", "coin": t.get("symbol") or t.get("coin") or "?",
            "side": t.get("side") or "CLOSE", "pnl_usd": t.get("realized_usd") or 0.0,
            "ts": t.get("close_ts") or t.get("open_ts") or 0,
            "reason": t.get("exit_reason") or "",
        })
    for t in live_trades[:10]:
        history.append({
            "kind": "live", "coin": t.get("symbol") or t.get("coin") or "?",
            "side": t.get("side") or "FILL", "pnl_usd": None,
            "ts": t.get("ts") or t.get("open_ts") or 0,
            "reason": "",
        })
    history.sort(key=lambda x: x.get("ts") or 0, reverse=True)
    top_reasons = sorted(exit_reasons.items(), key=lambda kv: kv[1], reverse=True)[:4]
    # Council verdict (best-effort; may be absent)
    path = _latest_council_report()
    council = {"available": False}
    if path:
        summary, run_time = _council_summary(path)
        council = {"available": True, "filename": path.name, "run_time": run_time,
                   "verdict": summary[:6]}
    return {
        "ok": True,
        "wallet": wallet,
        "edge": {
            "live_n": edge.get("live_n", 0),
            "live_fills": edge.get("live_fills", 0),
            "live_closes": edge.get("live_closes", 0),
            "paper_n": edge.get("paper_n", 0),
            "paper_wins": edge.get("paper_wins", 0),
            "paper_win_rate": edge.get("paper_win_rate", 0.0),
            "paper_net_usd": edge.get("paper_net_usd", 0.0),
            "starting_equity_usd": edge.get("starting_equity_usd", 0.0),
        },
        "notional": survival.get("notional") or {},
        "positions": {
            "live": len(survival.get("live_positions") or []),
            "paper": len(survival.get("paper_positions") or []),
        },
        "risk": {
            "promotion_enabled": bool(risk.get("promotion_enabled")),
            "defaults": (risk.get("defaults") or {}) if isinstance(risk, dict) else {},
        },
        "history": history[:20],
        "top_exit_reasons": top_reasons,
        "council": council,
    }


def _survival_risk_status(db_path: Path) -> dict:
    """Report live params and any separate shadow-tuned candidate truthfully."""
    try:
        import yaml
        from live_inventory import _default_params, _risk_params_override
        cfg = yaml.safe_load((Path(__file__).parent / "config.yaml").read_text(encoding="utf-8"))
    except Exception:
        return {}
    promotion_enabled = bool(
        cfg.get("live", {}).get("autonomous", {}).get(
            "autotune_live_promotion_enabled", False
        )
    )
    out = {}
    for mode in ("MEME", "SERIOUS"):
        override = _risk_params_override(db_path, mode)
        approved = bool(
            promotion_enabled and override is not None
            and override.get("source", "").startswith("approved:")
        )
        active = override if approved else _default_params(mode)
        out[mode] = {
            "take_profit_pct": active["take_profit_pct"],
            "stop_loss_pct": active["stop_loss_pct"],
            "trail_arm_pct": active["trail_arm_pct"],
            "trail_distance_pct": active["trail_distance_pct"],
            "max_hold_seconds": active["max_hold_seconds"],
            "source": "autotuned_live" if approved else "default_locked",
            "shadow_candidate": override,
            "live_promotion_enabled": promotion_enabled,
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


@app.get("/api/ui/prefs")
def api_ui_prefs_get():
    """Persisted dashboard preferences (theme, accent, layout, widget visibility)."""
    return mh_ui.load_prefs()


@app.post("/api/ui/prefs")
def api_ui_prefs_save(payload: dict = Body(...)):
    try:
        return {"ok": True, "prefs": mh_ui.save_prefs(payload)}
    except mh_ui.PrefError as exc:
        return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})


@app.get("/api/widget-issues")
def api_widget_issues(limit: int = 50, status: str | None = None):
    return mh_ui.list_widget_issues(limit, status)


@app.post("/api/widget-issues")
def api_widget_issue_file(payload: dict = Body(...)):
    result = mh_ui.file_widget_issue(payload)
    return JSONResponse(status_code=200 if result.get("ok") else 400, content=result)


def _group_profitability(rows, key_fn, min_trades=1):
    groups = {}
    for row in rows:
        key = key_fn(row)
        if not key:
            continue
        g = groups.setdefault(key, {"name": key, "trades": 0, "wins": 0, "realized_usd": 0.0})
        g["trades"] += 1
        pnl = float(row.get("realized_usd") or 0.0)
        g["realized_usd"] += pnl
        if pnl > 0:
            g["wins"] += 1
    out = []
    for g in groups.values():
        if g["trades"] < min_trades:
            continue
        g["win_rate"] = round(g["wins"] / g["trades"], 3) if g["trades"] else 0.0
        g["realized_usd"] = round(g["realized_usd"], 6)
        out.append(g)
    return sorted(out, key=lambda x: (x["realized_usd"], x["trades"]), reverse=True)


def _survival_start_equity() -> float:
    """Seed capital for the Xora-Survival paper incubator (~NZ$16.70 / US$10).
    Read from the owning module so the dashboard never hardcodes the constant
    here; fall back to the seed only if the module cannot be loaded."""
    try:
        from dynamic_shadow_scalper import INITIAL_EQUITY_USD
        return float(INITIAL_EQUITY_USD)
    except Exception:
        return 10.0


def _survival_wallet(trades=None) -> dict:
    """Xora-Survival paper wallet digest: seed vs net realised as % vs start.

    Paper-only by construction (dynamic_scalper). Live fills are reconciled
    separately by api_livegate and never merged into this paper number.
    Missing treasury data is never re-interpreted; no risk value is derived here.
    """
    if trades is None:
        trades = _trades(1000)
    # Paper-only by construction: never derive survival wallet equity from a
    # non-dynamic_scalper setup, regardless of caller. Live fills are reconciled
    # separately and never merged into this paper number.
    trades = [t for t in trades if t.get("setup") == "dynamic_scalper"]
    start = _survival_start_equity()
    net = round(sum(t.get("realized_usd") or 0.0 for t in trades), 6)
    equity = round(start + net, 6)
    fx = pricefeed_module.nzd_per_usd() if pricefeed_module else 1.67
    pct = (net / start * 100.0) if start else 0.0
    return {
        "starting_equity_usd": start,
        "starting_equity_nzd": round(start * fx, 6),
        "paper_net_usd": net,
        "paper_equity_usd": equity,
        "paper_equity_nzd": round(equity * fx, 6),
        "pnl_vs_start_usd": net,
        "pnl_vs_start_pct": round(pct, 4),
        "closed_trades": len(trades),
        "fx_nzd_per_usd": fx,
    }


def _chat_snapshot():
    """Compact read-only digest for the advisory chat. Never includes secrets."""
    try:
        summary = api_summary()
    except Exception:
        summary = {}
    traders = summary.get("traders", []) or []
    trades = _trades(2000)
    closed = [t for t in trades if t.get("realized_usd") is not None]
    trader_rows = []
    for t in traders:
        started = float(t.get("started") or 0.0)
        equity = float(t.get("equity") or 0.0)
        trader_rows.append({
            "name": t.get("trader"),
            "equity_usd": round(equity, 6),
            "pnl_usd": round(equity - started, 6),
            "equity_nzd": round(float(t.get("equity_nzd") or 0.0), 6),
            "committed_usd": round(float(t.get("committed") or 0.0), 6),
            "available_usd": round(float(t.get("available") or 0.0), 6),
        })
    trader_rows.sort(key=lambda x: x["pnl_usd"], reverse=True)
    snap = {
        "as_of": time.time(),
        "network": "solana mainnet-beta",
        "provenance_note": ("strategy traders run on the paper ledger; Xora-Survival live "
                            "path is evidence-gated and separate from paper rows"),
        "settlement_reserve": "USDC",
        "open_positions": (summary.get("totals") or {}).get("open_positions", 0),
        "traders": trader_rows,
        "coins": summary.get("coins", []),
        "profitability_rankings": {
            "best_traders_by_pnl_usd": trader_rows[:6],
            "best_setups_by_realized_usd": _group_profitability(closed, lambda r: r.get("setup") or "unknown")[:8],
            "best_coins_by_realized_usd": _group_profitability(closed, lambda r: r.get("coin") or r.get("symbol"))[:8],
        },
        "survival": _survival_wallet(
            [t for t in trades if t.get("setup") == "dynamic_scalper"]
        ),
    }
    try:
        snap["live_gate"] = api_livegate()
    except Exception:
        snap["live_gate"] = {}
    return snap


@app.get("/api/chat/history")
def api_chat_history(limit: int = 40):
    return mh_ui.chat_history(limit)


@app.post("/api/chat")
def api_chat(payload: dict = Body(...)):
    """Advisory oversight chat. Models advise; this endpoint submits no orders."""
    message = payload.get("message")
    history = payload.get("history")
    if history is None:
        history = mh_ui.chat_history(mh_ui.MAX_HISTORY_TURNS)
    return mh_ui.chat_reply(message, _chat_snapshot(), history)


@app.get("/api/requests")
def api_requests_list(limit: int = 50):
    return mh_ui.list_requests(limit)


@app.post("/api/requests")
def api_requests_file(payload: dict = Body(...)):
    result = mh_ui.file_request(payload.get("title"), payload.get("detail"),
                                payload.get("source") or "chat")
    return JSONResponse(status_code=200 if result.get("ok") else 400, content=result)


@app.post("/api/requests/{request_id}/council")
def api_requests_council(request_id: int):
    """Run the advisory council over one request. Records a verdict, applies nothing."""
    result = mh_ui.council_review(request_id)
    return JSONResponse(status_code=200 if result.get("ok") else 400, content=result)


@app.post("/api/requests/{request_id}/decision")
def api_requests_decide(request_id: int, payload: dict = Body(...)):
    """Operator decision. Deterministic; a council verdict never applies itself."""
    result = mh_ui.record_decision(request_id, payload.get("decision"),
                                   payload.get("note") or "")
    return JSONResponse(status_code=200 if result.get("ok") else 400, content=result)


@app.get("/api/actions")
def api_actions():
    """Live operational todo feed for the overview What to do widget."""
    now = datetime.now().astimezone()

    def next_every_30_at(minute_a=3, second=7):
        candidates = []
        for hour_offset in range(0, 3):
            base = now + timedelta(hours=hour_offset)
            for minute in (minute_a, minute_a + 30):
                if minute < 60:
                    candidates.append(base.replace(minute=minute, second=second, microsecond=0))
        future = [c for c in candidates if c > now]
        return min(future) if future else (now + timedelta(minutes=30))

    def next_monday_9():
        days = (0 - now.weekday()) % 7
        target = (now + timedelta(days=days)).replace(hour=9, minute=0, second=0, microsecond=0)
        if target <= now:
            target = target + timedelta(days=7)
        return target

    try:
        requests = mh_ui.list_requests(50)
    except Exception:
        requests = []
    pending = [r for r in requests if r.get("status") == "pending" or not r.get("decision")]
    survival = api_survival()
    risk = survival.get("risk_params") or {}
    paper_positions = survival.get("paper_positions") or []
    live_positions = survival.get("live_positions") or []
    notional = survival.get("notional") or {}
    livegate = api_livegate()
    blockers = [name for name, info in livegate.items() if not info.get("eligible")]
    items = [
        {"kind": "schedule", "title": "Next council report", "detail": "Per-coin shadow review + council", "due_ts": next_every_30_at().timestamp(), "state": "live"},
        {"kind": "schedule", "title": "Next audit", "detail": "Weekly read-only optimiser", "due_ts": next_monday_9().timestamp(), "state": "live"},
    ]
    if pending:
        items.append({"kind": "intervention", "title": "Improvements need review", "detail": f"{len(pending)} pending request(s) need a decision", "state": "attention"})
    else:
        items.append({"kind": "intervention", "title": "Improvement queue clear", "detail": "No pending operator decisions", "state": "ok"})
    if blockers:
        items.append({"kind": "todo", "title": "Live gate still blocked", "detail": ", ".join(blockers[:4]) + " need stronger evidence", "state": "attention"})
    if len(paper_positions) >= 10:
        items.append({"kind": "money", "title": "Watch open paper exposure", "detail": f"{len(paper_positions)} Xora paper positions open, paper notional {notional.get('paper_usd', 0):.2f} USDC", "state": "attention"})
    elif live_positions:
        items.append({"kind": "money", "title": "Check live wallet inventory", "detail": f"{len(live_positions)} live positions need monitoring", "state": "attention"})
    else:
        items.append({"kind": "money", "title": "Reserves stable", "detail": "No live wallet exposure reported", "state": "ok"})
    return {"now_ts": now.timestamp(), "items": items[:8], "risk_params": risk}


COUNCIL_REPORT_DIR = Path(os.environ.get("MULTIHEDGE_COUNCIL_DIR", "/data/council_reports"))


def _latest_council_report():
    """Newest *.md in the council report dir, or None. Container host must bind it."""
    try:
        files = sorted(COUNCIL_REPORT_DIR.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    except Exception:
        return None
    return files[0] if files else None


def _council_summary(path):
    """Extract the actionable council verdict (the ## Response body) from a report."""
    if not path or not path.exists():
        return [], None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return [], None
    lines = text.splitlines()
    # The response block starts after the '## Response' heading near the end.
    idx = max((i for i, l in enumerate(lines) if l.strip().startswith("## Response")), default=-1)
    body = lines[idx + 1:] if idx >= 0 else []
    # Drop leading markdown fences / blank lines
    body = [l for l in body if l.strip()]
    # Summarise: pick the standalone verdict lines under '**' bullets and any '###'.
    out = []
    for l in body:
        s = l.strip()
        if not s:
            continue
        if s.startswith("```"):
            continue
        if s.startswith("#"):
            out.append(s.lstrip("#").strip())
            continue
        if s.startswith("**") and len(s) > 6:
            out.append(s.strip("*").strip())
        elif len(s) > 40 and len(out) < 12:
            out.append(s)
        if len(out) >= 14:
            break
    run_time = None
    m = re.search(r"\*\*Run Time:\*\*\s*([^\n]+)", text)
    if m:
        run_time = m.group(1).strip()
    return out[:14], run_time


@app.get("/api/council/latest")
def api_council_latest():
    """Summary of the latest per-coin shadow review + council report."""
    path = _latest_council_report()
    if not path:
        return {"ok": False, "error": "no_council_report"}
    summary, run_time = _council_summary(path)
    return {"ok": True, "filename": path.name, "summary": summary,
            "run_time": run_time, "full_available": True}


@app.get("/api/council/report/{name}")
def api_council_report(name: str):
    """Full markdown of one council report, allow-listed by basename only."""
    if "/" in name or "\\" in name or name.startswith("."):
        return JSONResponse(status_code=400, content={"ok": False, "error": "invalid_name"})
    path = COUNCIL_REPORT_DIR / name
    if not path.exists() or not path.is_file():
        return JSONResponse(status_code=404, content={"ok": False, "error": "not_found"})
    try:
        return {"ok": True, "filename": name, "text": path.read_text(encoding="utf-8", errors="replace")}
    except Exception as exc:
        return JSONResponse(status_code=500, content={"ok": False, "error": "read_failed"})


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
:root{--bg:#f4f6fa;--surface:#fff;--surface-2:#f7f8fb;--panel:#f7f8fb;--sidebar:#fff;--border:#e3e7ef;--border-strong:#d3d9e5;--text:#182033;--text-dim:#5f6b80;--text-faint:#8490a3;--lav:#5c5bd6;--lav2:#4f50bd;--lav-dim:#eeeeff;--accent:#5c5bd6;--green:#138a61;--green-dim:#e9f6f0;--red:#c94759;--red2:#d75f50;--red-dim:#fceef0;--navy:#182033;--shadow:0 4px 16px rgba(25,36,58,.055);--chart-bg:#fafbfc;--chart-ink:rgba(40,52,74,.62);--chart-grid:rgba(64,76,98,.11)}
:root[data-theme="dark"]{--bg:#10131a;--surface:#181c25;--surface-2:#202530;--panel:#202530;--sidebar:#151922;--border:#2a303d;--border-strong:#394252;--text:#eef1f6;--text-dim:#b1bac9;--text-faint:#8792a5;--lav:#9997ef;--lav2:#aaa8f4;--lav-dim:#292946;--accent:#9997ef;--green:#55c69a;--green-dim:#183b31;--red:#ef7b89;--red2:#ef897c;--red-dim:#42242b;--navy:#eef1f6;--shadow:none;--chart-bg:#1c212b;--chart-ink:rgba(225,231,241,.68);--chart-grid:rgba(225,231,241,.10);color-scheme:dark}
*{margin:0;padding:0;box-sizing:border-box}
html{background:var(--bg)}
body{font-family:'Inter',sans-serif;color:var(--text);background:var(--bg);min-height:100vh;font-size:14px;line-height:1.5}
button,select{font:inherit}.app-shell{width:min(1600px,100%);min-height:100vh;margin:0 auto;display:grid;grid-template-columns:238px minmax(0,1fr)}
.sidebar{position:sticky;top:0;height:100vh;background:var(--sidebar);border-right:1px solid var(--border);padding:16px 18px;display:flex;flex-direction:column;z-index:10}
.brand{display:flex;align-items:center;gap:12px;padding:0 10px 16px}.brand-mark{width:36px;height:36px;border-radius:9px;background:var(--text);display:flex;align-items:center;justify-content:center;font-family:'Space Grotesk';font-weight:700;color:var(--surface);font-size:14px}
.brand-name{font-family:'Space Grotesk';font-weight:700;font-size:18px;color:var(--navy);letter-spacing:-.03em}.brand-sub{font-size:10px;color:var(--text-faint);letter-spacing:.14em;text-transform:uppercase}
.environment{display:flex;align-items:center;gap:8px;margin:0 8px 10px;padding:7px 11px;border-radius:8px;background:var(--lav-dim);color:var(--lav);font:700 10px 'IBM Plex Mono';letter-spacing:.04em}.environment .dot,.status-pill .dot{width:7px;height:7px;border-radius:50%;background:var(--green)}
.tabs{display:flex;flex-direction:column;gap:2px;min-height:0;overflow-y:auto;scrollbar-width:none}.tabs::-webkit-scrollbar{display:none}.nav-group{margin:9px 11px 4px;font-size:9px;font-weight:700;color:var(--text-faint);letter-spacing:.14em;text-transform:uppercase}.tabs button{display:flex;align-items:center;gap:10px;width:100%;min-height:34px;border:0;background:transparent;color:var(--text-dim);padding:5px 11px;border-radius:10px;text-align:left;font-size:12px;font-weight:600;cursor:pointer;transition:.18s ease}.tabs button:hover{background:var(--surface-2);color:var(--navy)}.tabs button.active{background:var(--lav-dim);color:var(--lav);box-shadow:inset 3px 0 0 var(--lav)}
.nav-icon{width:22px;height:22px;display:grid;place-items:center;border-radius:7px;background:#f0f3f9;color:#74809a;font:700 9px 'IBM Plex Mono'}.tabs button.active .nav-icon{background:#fff;color:var(--lav);box-shadow:0 3px 10px rgba(70,69,120,.09)}
.sidebar-foot{margin-top:auto;padding:10px 11px 0;border-top:1px solid var(--border)}.sidebar-foot .lbl{font-size:9px;color:var(--text-faint);text-transform:uppercase;letter-spacing:.12em}.ts{font:600 11px 'IBM Plex Mono';color:var(--navy);margin-top:2px}
.workspace{min-width:0;padding:30px 34px 70px}.workspace-header{position:sticky;top:0;z-index:5;background:var(--bg);display:flex;align-items:center;justify-content:space-between;gap:24px;margin-bottom:22px}.page-eyebrow{font-size:10px;font-weight:700;color:var(--lav);letter-spacing:.1em;text-transform:uppercase;margin-bottom:5px}.workspace-header h1{font-family:'Space Grotesk';font-size:30px;line-height:1.1;letter-spacing:-.03em;color:var(--navy)}.workspace-header p{color:var(--text-dim);margin-top:7px;font-size:13px;max-width:680px}.header-actions{display:flex;align-items:center;justify-content:flex-end;gap:8px;flex-wrap:wrap}.status-pill{font-size:10px;font-weight:700;color:var(--lav);background:var(--lav-dim);border:1px solid var(--border-strong);padding:8px 12px;border-radius:8px;white-space:nowrap}
.ticker{overflow:hidden;border:1px solid var(--border);border-radius:8px;background:var(--surface);margin-bottom:16px;white-space:nowrap;padding:9px 0}.ticker-track{display:flex;gap:30px;animation:scroll 40s linear infinite;width:max-content}@keyframes scroll{from{transform:translateX(0)}to{transform:translateX(-50%)}}.ticker-item{font:11px 'IBM Plex Mono';color:var(--text-dim);display:inline-flex;gap:6px;padding:0 8px;white-space:nowrap}.ticker-item b{color:var(--text)}.ticker-item .up{color:var(--green)}.ticker-item .down{color:var(--red)}
.hero{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:15px;margin-bottom:18px}.kpi{position:relative;background:var(--surface);border:1px solid var(--border);padding:18px;border-radius:12px;box-shadow:var(--shadow);overflow:hidden}.kpi .lbl{font-size:10px;font-weight:700;color:var(--text-dim);letter-spacing:.06em;text-transform:uppercase;padding-right:100px}.kpi .val{font-family:'Space Grotesk';font-size:25px;font-weight:700;letter-spacing:-.03em;color:var(--navy);margin-top:3px}.kpi .val.neg{color:var(--red)}.kpi-detail{color:var(--text-dim)}
.card{position:relative;background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:20px;margin-bottom:15px;box-shadow:var(--shadow);overflow-x:auto}.card h3{font-family:'Space Grotesk';font-size:15px;font-weight:700;color:var(--navy);letter-spacing:-.01em;margin-bottom:13px;padding-right:110px}.grid{display:grid;gap:15px;grid-template-columns:repeat(auto-fit,minmax(300px,1fr))}canvas{display:block;max-width:100%;width:100%;height:130px;background:var(--chart-bg);border:1px solid var(--border);border-radius:8px}#mkcv,#gld-cv{height:min(56vh,520px);min-height:300px}.tfbar{display:flex;gap:6px;flex-wrap:wrap;margin:10px 0 8px}.tfbar button{min-height:32px;font:600 11px 'IBM Plex Mono';background:var(--surface-2);color:var(--text-dim);border:1px solid var(--border);border-radius:7px;padding:5px 10px;cursor:pointer}.tfbar button:hover{border-color:var(--lav);color:var(--lav)}.tfbar button.on{background:var(--lav);color:var(--surface);border-color:var(--lav)}.tfbar button:disabled{opacity:.35;cursor:not-allowed}
.toolbar-btn{min-height:38px;padding:7px 11px;border:1px solid var(--border-strong);border-radius:8px;background:var(--surface);color:var(--text);font-weight:650;font-size:12px;cursor:pointer}.toolbar-btn:hover{border-color:var(--lav);color:var(--lav)}button:focus-visible,select:focus-visible,textarea:focus-visible,input:focus-visible{outline:2px solid var(--lav);outline-offset:2px}.update-state{font:11px 'IBM Plex Mono';color:var(--text-faint);white-space:nowrap}.widget-tools{position:absolute;right:12px;top:12px;display:flex;gap:4px;z-index:3}.widget-tools button{width:28px;height:28px;border:1px solid var(--border);border-radius:6px;background:var(--surface-2);color:var(--text-dim);cursor:pointer}.widget-tools button:hover{color:var(--lav);border-color:var(--lav)}.widget-report-btn{position:absolute;right:10px;top:10px;width:28px;height:28px;border:1px solid var(--border);border-radius:8px;background:var(--surface-2);color:var(--text-dim);cursor:pointer;display:grid;place-items:center;font-size:13px;z-index:6;opacity:.72}.widget-report-btn:hover{opacity:1;color:var(--red);border-color:var(--red)}.free-edit .widget-report-btn{display:none}.issue-modal-field{margin:12px 0}.issue-modal-field label{display:block;font-size:11px;font-weight:700;color:var(--text-dim);text-transform:uppercase;letter-spacing:.06em;margin-bottom:6px}.issue-modal-field textarea{width:100%;min-height:110px;resize:vertical;border:1px solid var(--border-strong);border-radius:10px;background:var(--surface-2);color:var(--text);padding:10px;font:13px 'Inter'}.issue-modal-field select,.issue-modal-field input[type=file]{width:100%;border:1px solid var(--border-strong);border-radius:8px;background:var(--surface-2);color:var(--text);padding:8px;font-size:12px}.issue-widget-context{max-height:130px;overflow:auto;border:1px solid var(--border);border-radius:8px;background:var(--surface-2);padding:9px;font:11px 'IBM Plex Mono';color:var(--text-dim);white-space:pre-wrap}.widget-drag{cursor:grab}.widget-dragging{opacity:.48}.widget-wide{grid-column:1/-1}.loading{padding:32px;text-align:center;color:var(--text-dim)}.load-error{padding:12px 14px;border:1px solid var(--red);border-radius:8px;color:var(--red);background:var(--red-dim);margin-bottom:14px}
table{width:100%;border-collapse:collapse;font-size:12px;white-space:nowrap}th,td{padding:10px 11px;text-align:left;border-bottom:1px solid var(--border)}th{color:var(--text-faint);font-weight:700;font-size:10px;letter-spacing:.05em;text-transform:uppercase;background:var(--surface-2)}tr:last-child td{border-bottom:0}.pos{color:var(--green)}.neg{color:var(--red)}.pilltag{display:inline-block;padding:3px 9px;border-radius:12px;font-size:10px;font-weight:700}.pilltag.ok{background:var(--green-dim);color:var(--green);border:1px solid var(--border)}.pilltag.no{background:var(--red-dim);color:var(--red);border:1px solid var(--border)}.ok-tag{display:inline-block;padding:3px 9px;border-radius:12px;font-size:10px;font-weight:700;background:var(--green-dim);color:var(--green)}.no-tag{display:inline-block;padding:3px 9px;border-radius:12px;font-size:10px;font-weight:700;background:var(--red-dim);color:var(--red)}.scroll-wrap{max-height:230px;overflow:auto;border-radius:8px;border:1px solid var(--border)}.scroll-wrap table thead th{position:sticky;top:0;background:var(--surface-2);z-index:2}.chart-flex{display:flex;gap:16px;align-items:flex-start}.chart-legend{min-width:170px;font:11px 'IBM Plex Mono';display:flex;flex-direction:column;gap:7px}.whats{font-size:13px;color:var(--text-dim);line-height:1.65}.whats b{color:var(--navy);font-family:'Space Grotesk';font-size:13px}
@media (max-width:1300px){.ticker{display:none}}
@media (max-width:1050px){.app-shell{grid-template-columns:190px minmax(0,1fr)}.sidebar{padding-left:10px;padding-right:10px}.workspace{padding:24px 20px 60px}.brand{padding-left:8px}.brand-sub{display:none}.hero{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media (max-width:760px){body{font-size:16px;overflow-x:hidden;touch-action:manipulation}.app-shell{display:block;width:100%;margin:0;border-radius:0;border:0}.sidebar{position:relative;width:100%;height:auto;padding:14px 12px 10px;border-right:0;border-bottom:1px solid var(--border)}.brand{padding:0 4px 12px}.brand-mark{width:34px;height:34px}.environment,.sidebar-foot,.nav-group{display:none}.tabs{display:flex;flex-direction:row;overflow-x:auto;scroll-snap-type:x mandatory;gap:6px;padding-bottom:3px}.tabs button{flex:0 0 auto;width:auto;min-height:52px;padding:11px 14px;scroll-snap-align:start;font-size:14px;cursor:pointer}.tabs button.active{box-shadow:inset 0 -3px 0 var(--lav)}.nav-icon{display:none}.workspace{padding:16px 10px 120px}.workspace-header{align-items:flex-start;flex-direction:column;margin-bottom:12px}.workspace-header h1{font-size:22px}.workspace-header p{font-size:12px}.header-actions{display:flex;width:100%;justify-content:flex-start;gap:6px;flex-wrap:wrap}.header-actions .update-state{display:none}.header-actions .status-pill{display:none}.header-actions .toolbar-btn{min-height:44px;font-size:13px;padding:8px 14px;border-radius:12px}.ticker{display:none}.hero{padding:0}.grid,.overview-grid{grid-template-columns:1fr;gap:10px}.grid>.card,.span-3,.span-4,.span-5,.span-6,.span-7,.span-8,.span-12{grid-column:1/-1}.card{padding:14px;margin-bottom:8px;border-radius:12px}.card h3{font-size:14px}.kpi{padding:14px;margin-bottom:0}.kpi .val{font-size:22px}.chart-flex{flex-direction:column}canvas{height:180px!important;min-width:0}.chart-legend{min-width:0;width:100%}.scroll-wrap{max-height:200px}table{font-size:11px}th,td{padding:8px}.tfbar button{min-height:44px;padding:8px 11px;font-size:12px}.wallet-tabs{grid-template-columns:repeat(2,minmax(0,1fr));gap:6px;margin-top:10px}.wallet-tab{min-height:60px;padding:8px}.wallet-tab b{font-size:16px}.wallet-summary span{font-size:11px;padding:6px 8px}.xora-pet{right:12px;bottom:12px;width:60px;height:64px}.xora-pet-body{width:46px;height:40px}.xora-pet-ear{width:14px;height:17px;top:9px}.xora-pet-tail{width:18px;height:10px;bottom:25px}.chat-panel{width:calc(100vw - 20px);right:10px;bottom:80px;max-height:50vh}.chat-fab{bottom:12px;right:12px;width:48px;height:48px;font-size:18px}.build-toolbar{width:calc(100% - 28px);left:14px;bottom:14px;transform:none;opacity:1;padding:8px 10px}.build-toolbar.open{transform:none}.bottom-actions{position:static;transform:none;width:auto;margin:12px 10px 60px;grid-template-columns:repeat(2,1fr);gap:8px}.bottom-actions button{min-height:50px;font-size:13px}.build-mode .card,.build-mode .kpi{padding-top:40px}.build-handle{height:28px}.build-handle-label{left:8px;top:6px;font-size:9px}.pet-bubble{max-width:calc(100vw - 80px);font-size:11px}.workspace{padding-bottom:28px}}
/* ---- Council report widget ---- */
.council-summary-list{display:flex;flex-direction:column;gap:6px;max-height:220px;overflow:auto}.council-s-line{font-size:11.5px;line-height:1.4;color:var(--text);padding:5px 8px;border-bottom:1px solid var(--border);border-radius:6px}.council-s-line:first-child{color:var(--accent);font-weight:700}.council-open{border-color:var(--accent);color:var(--accent);background:#1c261f}
/* ---- Xora summary widget ---- */
.xora-sum-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(116px,1fr));gap:8px;margin-bottom:12px}
.xora-sum-col{background:var(--surface-2);border:1px solid var(--border);border-radius:10px;padding:10px 12px;min-height:58px;display:flex;flex-direction:column;justify-content:center;gap:3px}
.xora-metric-label{font-size:9.5px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:var(--text-faint)}
.xora-metric-val{font:16px 'IBM Plex Mono';font-weight:600;color:var(--text);white-space:nowrap}
.xora-sum-section{margin-top:10px;padding-top:10px;border-top:1px solid var(--border)}
.xora-section-label{font-size:10px;font-weight:700;letter-spacing:.07em;text-transform:uppercase;color:var(--text-faint);display:block;margin-bottom:6px}
.xora-hist{display:flex;flex-direction:column;gap:3px;max-height:170px;overflow:auto}
.xora-hist-row{font-size:11px;color:var(--text-dim);padding:3px 2px;border-bottom:1px solid var(--border);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
/* ---- Live gate rows ---- */
.gate-row{display:grid;grid-template-columns:1fr auto auto;align-items:center;gap:10px;padding:7px 0;border-bottom:1px solid var(--border)}
.gate-row:last-child{border-bottom:0}
.gate-name{font-weight:700;font-size:12px;color:var(--text);white-space:nowrap}
.gate-meta{font:11px 'IBM Plex Mono';color:var(--text-dim);text-align:right;white-space:nowrap}
.gate-meta b{color:var(--text)}
.livegate-note{font-size:11px;color:var(--text-faint);margin:8px 0}
.lg-sub{font-size:10.5px;color:var(--text-faint);margin-top:2px}
/* ---- Settings modal ---- */
.modal-overlay{position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:100;display:none;align-items:center;justify-content:center}.modal-overlay.open{display:flex}.modal-panel{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:28px;max-width:580px;width:calc(100% - 32px);max-height:85vh;overflow-y:auto;box-shadow:0 8px 40px rgba(0,0,0,.18)}.modal-panel h2{font-family:'Space Grotesk';font-size:20px;margin-bottom:6px;color:var(--navy)}.modal-section{margin:16px 0}.modal-section>.lbl{font-size:11px;font-weight:700;color:var(--text-dim);text-transform:uppercase;letter-spacing:.06em;margin-bottom:6px}.settings-row{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:8px 0;border-bottom:1px solid var(--border)}.settings-row:last-child{border-bottom:0}.settings-row select,.settings-row input[type=color]{height:34px;border:1px solid var(--border-strong);border-radius:7px;background:var(--surface-2);color:var(--text);padding:4px 8px;font-size:12px;cursor:pointer}.settings-row input[type=range]{width:120px;accent-color:var(--lav)}.color-swatches{display:flex;gap:6px;flex-wrap:wrap}.color-swatch{width:28px;height:28px;border-radius:7px;border:2px solid transparent;cursor:pointer}.color-swatch:hover{border-color:var(--border-strong)}.color-swatch.active{border-color:var(--lav)}.toggle-track{position:relative;width:36px;height:20px;display:inline-block;background:var(--border-strong);border-radius:10px;cursor:pointer;transition:.2s}.toggle-track.active{background:var(--lav)}.toggle-dot{position:absolute;left:2px;top:2px;width:16px;height:16px;border-radius:50%;background:#fff;transition:.2s}.toggle-track.active .toggle-dot{left:18px}
/* ---- Hide scrollbars (setting) ---- */
html.hide-scrollbars{overflow:hidden}html.hide-scrollbars body{overflow:hidden}html.hide-scrollbars ::-webkit-scrollbar{display:none}html.hide-scrollbars ::-moz-scrollbar{display:none}
/* ---- View-mode saved layout anchoring ---- */
.layout-reapplied{position:relative!important;min-height:400px}
/* ---- Free-position edit mode ---- */
.free-edit #tab-panels,.free-edit .hero,.free-edit #coin-wrap{position:relative;min-height:400px;width:100%}
.free-edit .card,.free-edit .kpi{position:absolute;cursor:grab;margin:0;box-sizing:border-box;touch-action:none}
.free-edit .card,.free-edit .kpi{border:2px dashed var(--accent);box-shadow:0 0 0 1px rgba(56,210,109,.18),0 0 20px rgba(56,210,109,.12)!important;overflow:auto}
.free-grab{position:absolute;left:0;right:0;top:0;height:30px;border-bottom:1px solid rgba(56,210,109,.5);background-image:repeating-linear-gradient(90deg,rgba(56,210,109,.32) 0 2px,transparent 2px 7px),repeating-linear-gradient(180deg,rgba(56,210,109,.18) 0 1px,transparent 1px 6px);cursor:grab;z-index:8;touch-action:none}
.free-grab:active{cursor:grabbing}
.free-grab .grab-label{position:absolute;left:8px;top:7px;font:800 9px 'IBM Plex Mono';color:var(--accent);letter-spacing:.08em;text-transform:uppercase;pointer-events:none}
.free-resize{position:absolute;right:2px;bottom:2px;width:26px;height:26px;background:linear-gradient(135deg,transparent 46%,rgba(56,210,109,.45) 50%,rgba(56,210,109,.45) 56%,transparent 58%);cursor:nwse-resize;z-index:8;touch-action:none}
.free-resize:active{cursor:nwse-resize}
.free-hide{position:absolute;left:4px;top:4px;width:24px;height:24px;border:1px solid var(--border);border-radius:6px;background:var(--surface-2);color:var(--text-dim);font-size:12px;cursor:pointer;z-index:9;display:none}
.free-edit .free-hide{display:grid}
.free-edit .card:hover .free-hide,.free-edit .kpi:hover .free-hide{color:var(--red);border-color:var(--red)}
.free-hidden{display:none}
.free-edit .cancel-btn{display:inline-flex}
.free-edit .catalog-btn{display:inline-flex}

/* ---- Widget catalog ---- */
/* ---- Widget catalog ---- */
.widget-catalog{position:fixed;top:18px;left:50%;transform:translateX(-50%);width:min(560px,calc(100% - 32px));max-height:82vh;overflow-y:auto;background:var(--surface);border:1px solid var(--border-strong);border-radius:14px;box-shadow:0 10px 44px rgba(0,0,0,.22);z-index:120;display:none;padding:16px}
.widget-catalog.open{display:block}
.widget-catalog-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px}
.widget-catalog-head span{font:700 14px 'Space Grotesk';color:var(--navy)}
.widget-catalog-head button{width:28px;height:28px;border:0;background:var(--surface-2);border-radius:7px;cursor:pointer;color:var(--text-dim);font-size:15px;display:grid;place-items:center}
.widget-catalog-search{margin-bottom:10px}.widget-catalog-search input{width:100%;height:34px;border:1px solid var(--border-strong);border-radius:8px;background:var(--surface-2);color:var(--text);padding:6px 10px;font-size:12px}
.widget-catalog-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px}
.widget-catalog .wc-item{display:flex;flex-direction:column;align-items:flex-start;gap:4px;text-align:left;padding:8px 10px;border:1px solid var(--border);border-radius:9px;background:var(--surface-2);color:var(--text);font-size:11px;cursor:grab;position:relative}.widget-catalog .wc-item:active{cursor:grabbing}.widget-free{min-height:120px;display:flex;flex-direction:column;min-width:0}.widget-free h3{flex:0 0 auto}.widget-free .widget-free-note{font-size:12px;color:var(--text-dim);line-height:1.5;flex:1 1 auto;min-height:0;overflow:auto}.widget-free .widget-free-note>.scroll-wrap{height:100%;max-height:none!important;min-height:0;overflow:auto}.widget-free table{font-size:12px}.widget-free th{font-size:10px}.drop-armed{outline:2px dashed var(--accent);outline-offset:4px}
.widget-catalog .wc-section{grid-column:1/-1;margin-top:8px;padding-top:8px;border-top:1px solid var(--border)}.widget-catalog .wc-section:first-child{margin-top:0;padding-top:0;border-top:0}.widget-catalog .wc-section-title{font:800 10px 'IBM Plex Mono';letter-spacing:.12em;text-transform:uppercase;color:var(--accent);margin-bottom:6px}.widget-catalog .wc-type-title{font:700 9px 'IBM Plex Mono';letter-spacing:.08em;text-transform:uppercase;color:var(--text-faint);margin:6px 0 5px}.widget-catalog .wc-type-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px}.widget-catalog .wc-item:hover{border-color:var(--accent);color:var(--accent)}
.widget-catalog .wc-item b{font-size:11px}.widget-catalog .wc-item small{font-size:9.5px;color:var(--text-faint)}
.widget-catalog .wc-item .wc-add{position:absolute;top:8px;right:8px;width:20px;height:20px;border-radius:5px;background:var(--accent);color:var(--surface);font-size:11px;display:grid;place-items:center}
/* ---- Widget tile resize ---- */
.tile-resize{display:none;position:absolute;right:8px;top:8px;width:26px;height:26px;border:1px solid var(--border);border-radius:6px;background:var(--surface-2);color:var(--text-dim);cursor:pointer;font-size:11px;place-items:center;z-index:4;opacity:0;transition:opacity .2s}.card:hover .tile-resize,.kpi:hover .tile-resize{opacity:1}.card:hover .tile-resize:hover,.kpi:hover .tile-resize:hover{color:var(--lav);border-color:var(--lav)}.tile-s4{grid-column:span 4}.tile-s6{grid-column:span 6}.tile-s8{grid-column:span 8}.tile-s12{grid-column:1/-1}@media (max-width:1050px){.tile-s4,.tile-s6,.tile-s8,.tile-s12{grid-column:1/-1}}@media (max-width:760px){.tile-resize{display:none!important}}
/* ---- Scroll-sensitive sidebar ---- */
.sidebar-scrolled .sidebar{width:56px;padding:12px 10px}.sidebar-scrolled .sidebar .brand-mark{width:28px;height:28px;border-radius:7px;font-size:10px}.sidebar-scrolled .sidebar .brand-name,.sidebar-scrolled .sidebar .brand-sub,.sidebar-scrolled .sidebar .environment,.sidebar-scrolled .sidebar .nav-group,.sidebar-scrolled .sidebar .nav-icon,.sidebar-scrolled .sidebar .tabs button span:not(.nav-icon){display:none}.sidebar-scrolled .sidebar .tabs button{justify-content:center;padding:5px;min-height:32px}.sidebar-scrolled .workspace{margin-left:0}.sidebar-scrolled .app-shell{grid-template-columns:56px minmax(0,1fr)}
/* ---- Chat pet ---- */
.xora-pet{position:fixed;right:26px;bottom:24px;width:70px;height:76px;border:0;background:transparent;cursor:grab;z-index:91;filter:drop-shadow(0 6px 14px rgba(0,0,0,.25));transition:transform .2s;touch-action:manipulation;min-width:70px;min-height:76px}.xora-pet-inner{position:absolute;inset:0;pointer-events:none;animation:petBob 3.2s ease-in-out infinite}.xora-pet.pet-dragging{cursor:grabbing;user-select:none}.xora-pet.pet-dragging .xora-pet-inner{animation-play-state:paused}.xora-pet-body{position:absolute;left:8px;bottom:8px;width:54px;height:48px;border-radius:24px 24px 18px 18px;background:linear-gradient(145deg,var(--accent),var(--lav2));border:2px solid #0b1b10}.xora-pet-body:before,.xora-pet-body:after{content:'';position:absolute;top:10px;width:7px;height:7px;border-radius:50%;background:#07100a}.xora-pet-body:before{left:15px}.xora-pet-body:after{right:15px}.xora-pet-ear{position:absolute;top:11px;width:17px;height:20px;border-radius:12px 12px 2px 12px;background:var(--accent);border:2px solid #0b1b10}.xora-pet-ear.left{left:12px;transform:rotate(-18deg)}.xora-pet-ear.right{right:12px;transform:rotate(18deg) scaleX(-1)}.xora-pet-tail{position:absolute;right:1px;bottom:29px;width:22px;height:12px;border-radius:12px;border:3px solid var(--accent);border-left:0;transform:rotate(-16deg)}.xora-pet-feet{position:absolute;left:16px;right:16px;bottom:3px;display:flex;justify-content:space-between}.xora-pet-feet span{width:12px;height:9px;border-radius:8px;background:#0b1b10}.xora-pet:active{transform:scale(.94)}.xora-pet:hover{animation-play-state:paused}.xora-pet:focus-visible{outline:2px solid var(--accent);outline-offset:4px;border-radius:18px}.pet-bubble{position:fixed;top:auto;bottom:auto;max-width:min(280px,calc(100vw - 128px));padding:10px 12px;border-radius:14px 14px 14px 4px;background:var(--surface);border:1px solid var(--border-strong);color:var(--text);font-size:12px;line-height:1.45;z-index:91;box-shadow:0 8px 30px rgba(0,0,0,.22);pointer-events:none}.pet-bubble:after{content:'';position:absolute;bottom:-8px;right:16px;border-width:8px 8px 0;border-style:solid;border-color:var(--surface) transparent transparent}@keyframes petBob{0%,100%{transform:translateY(0)}50%{transform:translateY(-3px)}}.chat-panel{position:fixed;bottom:108px;right:24px;width:400px;max-height:520px;background:var(--surface);border:1px solid var(--border);border-radius:16px;box-shadow:0 8px 40px rgba(0,0,0,.18);z-index:92;display:none;flex-direction:column;overflow:hidden}.chat-panel.open{display:flex}.chat-header{display:flex;align-items:center;justify-content:space-between;padding:14px 16px;border-bottom:1px solid var(--border);font-family:'Space Grotesk';font-weight:700;font-size:14px;color:var(--navy)}.chat-msgs{flex:1;overflow-y:auto;padding:12px 16px;display:flex;flex-direction:column;gap:8px;font-size:13px;line-height:1.45;min-height:200px}.chat-input{display:flex;gap:8px;padding:10px 12px;border-top:1px solid var(--border)}.chat-input input{flex:1;border:1px solid var(--border-strong);border-radius:10px;background:var(--surface-2);padding:10px 12px;color:var(--text);font-size:13px}.chat-input button{min-height:36px;padding:0 13px;border-radius:10px;background:var(--lav);color:var(--surface);font-weight:700;font-size:12px;border:0;cursor:pointer}.chat-input button:hover{background:var(--lav2)}.chat-msg{max-width:92%;padding:9px 12px;border-radius:12px;font-size:12.5px;line-height:1.5;white-space:pre-wrap}.chat-msg.user{background:var(--lav-dim);color:var(--text);align-self:flex-end;border-bottom-right-radius:4px}.chat-msg.assistant{background:var(--surface-2);color:var(--text);align-self:flex-start;border-bottom-left-radius:4px}.chat-msg.err{background:var(--red-dim);color:var(--red);align-self:center;font-size:11px}.chat-msg .ts{font-size:10px;color:var(--text-faint);margin-top:3px}.chat-loading{text-align:center;color:var(--text-faint);padding:12px;font-size:12px}.chat-filing{display:flex;gap:6px;padding:6px 12px 0}.chat-filing button{font-size:11px;padding:4px 10px;border-radius:6px;border:1px solid var(--border);background:var(--surface-2);color:var(--text-dim);cursor:pointer}.chat-filing button:hover{color:var(--lav)}@media (max-width:760px){.chat-panel{width:calc(100vw - 32px);right:16px;bottom:76px;max-height:60vh}.chat-fab{bottom:16px;right:16px}}
/* ---- Journal-style MultiHedge skin ---- */
:root{--bg:#141515;--surface:#1b1d1d;--surface-2:#222525;--panel:#1d2020;--sidebar:#181a1a;--border:#2d3331;--border-strong:#3b4440;--text:#e7edf0;--text-dim:#9aa8aa;--text-faint:#687476;--lav:#38d26d;--lav2:#55e083;--lav-dim:#183323;--accent:#38d26d;--green:#38d26d;--green-dim:#173522;--red:#e05d63;--red2:#f36f6f;--red-dim:#371d21;--navy:#eef5f1;--shadow:none;--chart-bg:#171919;--chart-ink:rgba(220,232,228,.7);--chart-grid:rgba(220,232,228,.10)}
html{background:radial-gradient(circle at 50% 10%,#2bc65f 0,#174b2a 36%,#0d1512 88%)}body{background:transparent}.app-shell{width:min(1680px,calc(100% - 28px));margin:14px auto;border:1px solid #29302d;border-radius:18px;overflow:hidden;background:var(--bg);grid-template-columns:210px minmax(0,1fr)}.sidebar{background:#171919}.brand-mark{background:var(--accent);color:#07100a;border-radius:12px}.brand-name{color:var(--text)}.environment{background:#11291a;color:var(--accent);border:1px solid #224e31}.tabs button{min-height:38px;border:1px solid transparent}.tabs button:hover{border-color:var(--border);background:#202323}.tabs button.active{background:#202823;color:var(--accent);box-shadow:inset 3px 0 0 var(--accent)}.nav-icon,.tabs button.active .nav-icon{background:#242a27;color:var(--accent);box-shadow:none}.workspace{padding:28px 28px 96px}.workspace-header{border-bottom:1px solid var(--border);padding-bottom:16px}.workspace-header h1{color:var(--text);font-size:28px}.page-eyebrow{color:var(--accent)}.status-pill{background:#13291c;color:var(--accent);border-color:#244e32}.toolbar-btn{border-radius:10px;background:#202323;border-color:var(--border);color:var(--text-dim)}.toolbar-btn:hover{background:#233126;color:var(--accent);border-color:var(--accent)}.card,.kpi{background:var(--surface);border-color:var(--border);border-radius:14px;box-shadow:none}.card h3,.kpi .val{color:var(--text)}.kpi .lbl,.card .lbl{color:var(--text-dim)}.grid{grid-template-columns:repeat(12,minmax(0,1fr));gap:12px}.grid>.card{grid-column:span 4}.grid>.card.widget-wide{grid-column:1/-1}.overview-grid{display:grid;grid-template-columns:repeat(12,minmax(0,1fr));gap:12px}.overview-grid>.card{margin-bottom:0}.span-3{grid-column:span 3}.span-4{grid-column:span 4}.span-5{grid-column:span 5}.span-6{grid-column:span 6}.span-7{grid-column:span 7}.span-8{grid-column:span 8}.span-12{grid-column:1/-1}.metric-xl{font-family:'Space Grotesk';font-size:31px;font-weight:800;color:var(--accent);letter-spacing:-.04em}.mini-note{font-size:11px;color:var(--text-faint)}.todo-list{display:flex;flex-direction:column;gap:9px}.todo-item{display:grid;grid-template-columns:20px 1fr;gap:9px;align-items:start}.todo-check{width:16px;height:16px;border-radius:4px;border:1px solid var(--border-strong);background:#171a1a;margin-top:2px}.todo-check.ok{background:var(--accent);border-color:var(--accent);position:relative}.todo-check.ok:after{content:'✓';color:#08110b;font-weight:900;font-size:12px;position:absolute;left:3px;top:-1px}.todo-title{font-weight:700;color:var(--text);font-size:13px}.todo-detail{font-size:11px;color:var(--text-dim);line-height:1.35}.heatmap{display:grid;grid-template-columns:repeat(26,1fr);gap:4px}.heat-cell{aspect-ratio:1;border-radius:3px;background:#222928}.heat-cell.l1{background:#1d4a2a}.heat-cell.l2{background:#24823f}.heat-cell.l3{background:#38d26d}.radar-wrap{display:grid;place-items:center}.bottom-actions{position:fixed;left:50%;bottom:18px;transform:translateX(-50%);width:min(1180px,calc(100% - 44px));display:grid;grid-template-columns:repeat(6,1fr);gap:10px;z-index:70}.bottom-actions button{min-height:48px;border-radius:14px;background:#202323;border:1px solid var(--border);color:var(--text);font-weight:700;cursor:pointer}.bottom-actions button:hover{border-color:var(--accent);color:var(--accent);background:#203024}.wallet-hub{grid-column:1/-1!important}.wallet-hub-head{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;flex-wrap:wrap}.wallet-summary{display:flex;gap:10px;flex-wrap:wrap;justify-content:flex-end}.wallet-summary span{display:block;padding:8px 10px;border:1px solid var(--border);border-radius:10px;background:#171a1a;color:var(--text-dim);font-size:12px}.wallet-tabs{display:grid;grid-template-columns:repeat(6,minmax(120px,1fr));gap:8px;margin-top:14px}.wallet-tab{min-height:70px;border:1px solid var(--border);background:#171a1a;color:var(--text);border-radius:12px;padding:10px;text-align:left;cursor:pointer;display:flex;flex-direction:column;gap:3px;transition:.18s ease}.wallet-tab span{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--text-dim);font-weight:800}.wallet-tab b{font-family:'Space Grotesk';font-size:18px;color:var(--text)}.wallet-tab small{font-size:11px;color:var(--text-faint)}.wallet-tab:hover,.wallet-tab.active{border-color:var(--accent);box-shadow:0 0 0 1px rgba(56,210,109,.18),0 0 22px rgba(56,210,109,.09);background:#1c261f}.wallet-tab.bad:hover,.wallet-tab.bad.active{border-color:var(--red);box-shadow:0 0 0 1px rgba(224,93,99,.16)}@media(max-width:1050px){.grid,.overview-grid{grid-template-columns:repeat(6,minmax(0,1fr))}.span-3,.span-4{grid-column:span 3}.span-5,.span-6,.span-7,.span-8{grid-column:span 6}.bottom-actions{grid-template-columns:repeat(3,1fr)}.wallet-tabs{grid-template-columns:repeat(3,minmax(120px,1fr))}}@media(max-width:760px){.app-shell{width:100%;margin:0;border-radius:0}.grid,.overview-grid{grid-template-columns:1fr}.grid>.card,.span-3,.span-4,.span-5,.span-6,.span-7,.span-8,.span-12{grid-column:1/-1}.bottom-actions{position:static;transform:none;width:auto;margin:0 12px 70px;grid-template-columns:repeat(2,1fr)}.workspace{padding-bottom:28px}}

</style></head><body><div class="app-shell">
<aside class="sidebar">
  <div class="brand"><div class="brand-mark">MH</div><div><div class="brand-name">MultiHedge</div><div class="brand-sub">Trading system</div></div></div>
  <div class="environment"><span class="dot"></span>PAPER SIMULATION</div>
  <nav class="tabs" id="tabNav" aria-label="Dashboard sections">
    <div class="nav-group">Command</div>
    <button data-tab="overview" class="active"><span class="nav-icon">01</span>Overview</button>
    <button data-tab="survival"><span class="nav-icon">X</span>Xora-Survival</button>
    <button data-tab="market"><span class="nav-icon">02</span>Market</button>
    <button data-tab="gate"><span class="nav-icon">03</span>Live Gate</button>
    <div class="nav-group">Traders</div>
    <button data-tab="strategies"><span class="nav-icon">S</span>Scalper</button>
    <button data-tab="reasoner"><span class="nav-icon">R</span>Reasoner</button>
    <button data-tab="whales"><span class="nav-icon">W</span>Whale Copy</button>
    <button data-tab="memecoin"><span class="nav-icon">M</span>Memecoin</button>
    <button data-tab="grid"><span class="nav-icon">G</span>Grid</button>
  </nav>
</aside>
<main class="workspace">
  <header class="workspace-header"><div><div class="page-eyebrow">Trading command center</div><h1 id="pageTitle">System Overview</h1><p id="pageSubtitle">Capital, positions, trader performance, and live market context across MultiHedge.</p></div><div class="header-actions"><button class="toolbar-btn" id="clockBtn" type="button" aria-label="Refresh dashboard" title="Refresh"><span id="ts">--</span></button><button class="toolbar-btn cancel-btn" id="cancelEditBtn" type="button" style="display:none" aria-label="Cancel edit and revert layout">&#xd7; Cancel</button><button class="toolbar-btn catalog-btn" id="addWidgetBtn" type="button" style="display:none" aria-label="Add widgets">+ Add Widget</button><button class="toolbar-btn" id="themeToggle" type="button" aria-pressed="false">Dark mode</button><button class="toolbar-btn" id="settingsBtn" type="button" aria-label="Dashboard settings">&#x2699;</button><span class="status-pill">PAPER TRADING · MAINNET MARKET DATA</span></div></header>
  <div class="ticker" id="ticker"></div>
  <div class="hero" id="kpis"></div>
  <div id="tab-panels"></div>
  <div class="grid" id="coin-wrap"></div>
</main>
</div>
<!-- Council report modal -->
<div class="modal-overlay" id="councilModal">
  <div class="modal-panel" style="max-width:min(900px,94vw)">
    <div style="display:flex;justify-content:space-between;align-items:start;margin-bottom:10px">
      <div><h2>Council report</h2><div class="lbl" style="font-size:11px;color:var(--text-dim)">Per-coin shadow review + adversarial council</div></div>
      <button style="width:32px;height:32px;border:0;background:var(--surface-2);border-radius:8px;cursor:pointer;color:var(--text-dim);font-size:16px;display:grid;place-items:center" id="councilModalClose">&times;</button>
    </div>
    <div id="councilModalBody" style="border:1px solid var(--border);border-radius:12px;padding:14px"></div>
  </div>
</div>

<!-- Settings modal -->
<div class="modal-overlay" id="settingsModal">
  <div class="modal-panel">
    <div style="display:flex;justify-content:space-between;align-items:start;margin-bottom:12px">
      <div><h2>Dashboard settings</h2><div class="lbl" style="font-size:11px;color:var(--text-dim)">UI & UX controls</div></div>
      <button style="width:32px;height:32px;border:0;background:var(--surface-2);border-radius:8px;cursor:pointer;color:var(--text-dim);font-size:16px;display:grid;place-items:center" id="settingsClose">&times;</button>
    </div>
    <div class="modal-section"><div class="lbl">Appearance</div>
      <div class="settings-row"><span>Theme</span><select id="pref-theme"><option value="system">System</option><option value="light">Light</option><option value="dark">Dark</option></select></div>
      <div class="settings-row"><span>Accent</span><div class="color-swatches" id="pref-accent"></div></div>
      <div class="settings-row"><span>Density</span><select id="pref-density"><option value="comfortable">Comfortable</option><option value="compact">Compact</option></select></div>
      <div class="settings-row"><span>Font scale</span><input type="range" id="pref-fontScale" min="80" max="140" value="100" style="width:120px"> <span id="pref-fontScale-val" style="font:11px 'IBM Plex Mono';color:var(--text-dim)">100%</span></div>
    </div>
    <div class="modal-section"><div class="lbl">Layout</div>
      <div class="settings-row"><span>Refresh interval</span><select id="pref-refresh"><option value="10">10s</option><option value="30" selected>30s</option><option value="60">60s</option><option value="120">120s</option></select></div>
      <div class="settings-row"><span>Hide scrollbars</span><span class="toggle-track" id="pref-hideScrollbars" data-key="hideScrollbars"><span class="toggle-dot"></span></span></div>
      <div class="settings-row"><span id="editToggleLabel">Editing mode off</span><button type="button" id="editToggleBtn" class="toolbar-btn" style="min-height:32px;padding:4px 12px">Edit layout</button></div>
      <div class="settings-row"><span>Save or cancel</span><div style="display:flex;gap:6px"><button type="button" id="editSaveBtnFromSettings" class="toolbar-btn" style="min-height:32px;padding:4px 10px">Save</button><button type="button" id="editCancelBtnFromSettings" class="toolbar-btn" style="min-height:32px;padding:4px 10px">Cancel</button></div></div>
    </div>
    <div class="modal-section"><div class="lbl">Chat & Council</div>
      <div class="settings-row"><span>Oversight chat</span><span class="toggle-track" id="pref-chatToggle" data-key="chatEnabled"><span class="toggle-dot"></span></span></div>
      <div class="settings-row"><span>Agent</span><span style="font:11px 'IBM Plex Mono';color:var(--text-dim)">openrouter/free</span></div>
    </div>
    <div class="modal-section"><div class="lbl">Improvement requests</div>
      <div class="settings-row"><span>Pending requests</span><span style="font:11px 'IBM Plex Mono';color:var(--text-dim)" id="pendingReqCount">--</span></div>
      <div class="settings-row"><span>View requests</span><button class="toolbar-btn" id="openRequestsBtn" style="min-height:30px;padding:4px 10px">Open</button></div>
    </div>
    <div class="modal-section">
      <div style="font-size:12px;color:var(--text-dim);line-height:1.5">MultiHedge dashboard, paper simulation with evidence-gated Xora-Survival live path. Preferences persist on server and localStorage.</div>
    </div>
  </div>
</div>
<!-- Widget issue modal -->
<div class="modal-overlay" id="widgetIssueModal">
  <div class="modal-panel" style="max-width:min(680px,94vw)">
    <div style="display:flex;justify-content:space-between;align-items:start;margin-bottom:12px">
      <div><h2>Report widget issue</h2><div class="lbl" id="widgetIssueSubtitle" style="font-size:11px;color:var(--text-dim)">Dashboard widget</div></div>
      <button style="width:32px;height:32px;border:0;background:var(--surface-2);border-radius:8px;cursor:pointer;color:var(--text-dim);font-size:16px;display:grid;place-items:center" id="widgetIssueClose">&times;</button>
    </div>
    <div class="issue-modal-field"><label for="widgetIssueText">What looks wrong?</label><textarea id="widgetIssueText" maxlength="4000" placeholder="Say it your way, e.g. this number is cramped, this card looks wrong on mobile, this says live when it should say paper..."></textarea></div>
    <div class="issue-modal-field"><label for="widgetIssueSeverity">Severity</label><select id="widgetIssueSeverity"><option value="confusing">Confusing</option><option value="annoying">Annoying</option><option value="broken">Broken</option><option value="dangerous">Dangerous</option></select></div>
    <div class="issue-modal-field"><label for="widgetIssueScreenshot">Upload screenshot optional</label><input id="widgetIssueScreenshot" type="file" accept="image/png,image/jpeg,image/webp"></div>
    <div class="issue-modal-field"><label>Captured widget context</label><div class="issue-widget-context" id="widgetIssueContext"></div></div>
    <div style="display:flex;gap:8px;justify-content:flex-end;flex-wrap:wrap"><button class="toolbar-btn" id="widgetIssueCancel" type="button">Cancel</button><button class="toolbar-btn" id="widgetIssueSubmit" type="button">Submit report</button></div>
  </div>
</div>
<!-- Widget catalog (edit-mode only) -->
<div class="widget-catalog" id="widgetCatalog" role="dialog" aria-label="Add widget">
  <div class="widget-catalog-head"><span>Add widget</span><button type="button" id="widgetCatalogClose" aria-label="Close catalog">&times;</button></div>
  <div class="widget-catalog-search"><input id="widgetCatalogSearch" placeholder="Search widgets..." aria-label="Search widgets"></div>
  <div class="widget-catalog-grid" id="widgetCatalogGrid"></div>
</div>
<!-- Chat pet -->
<div id="petBubble" class="pet-bubble" role="status" aria-live="polite">Hi, I am Xora. Hover a widget for a hint, or click me to chat.</div>
<button class="xora-pet" id="xoraPet" type="button" aria-label="Open Xora-Survival pet chat">
  <span class="xora-pet-inner">
  <span class="xora-pet-tail"></span>
  <span class="xora-pet-ear left"></span>
  <span class="xora-pet-ear right"></span>
  <span class="xora-pet-body"></span>
  <span class="xora-pet-feet"><span></span><span></span></span>
  </span>
</button>
<div class="chat-panel" id="chatPanel">
  <div class="chat-header"><span>Xora-Survival pet</span><button style="width:28px;height:28px;border:0;background:var(--surface-2);border-radius:7px;cursor:pointer;color:var(--text-dim);font-size:14px;display:grid;place-items:center" id="chatClose">&times;</button></div>
  <div class="chat-msgs" id="chatMsgs"><div class="chat-placeholder" style="text-align:center;color:var(--text-faint);padding:16px;font-size:12px">Ask anything about MultiHedge. Messages are advisory only.</div></div>
  <div class="chat-filing" id="chatFiling" style="padding:0 12px;display:none"><button id="fileRequestBtn" style="font-size:10px;padding:3px 8px;border-radius:5px;border:1px solid var(--border);background:var(--surface-2);color:var(--text-dim);cursor:pointer;margin-bottom:5px">File improvement request</button></div>
  <div class="chat-input"><input id="chatInput" placeholder="Ask about the system..." maxlength="2000"><button id="chatSend">Send</button></div>
</div>

<script>
const COINS=['SOL','JUP','ETH'];
const TRADERS=['scalper','reasoner'];
const NAV_META={
  overview:['System Overview','Capital, positions, trader performance, and live market context across MultiHedge.'],
  market:['Live Market','Price action and retained market history for the assets MultiHedge follows.'],
  gate:['Live Gate','Deterministic promotion readiness. Paper evidence never counts as live execution.'],
  strategies:['Scalper','Fast paper strategies, wallet state, exits, cumulative edge, and recent activity.'],
  reasoner:['Reasoner','News-driven swing decisions, confidence, positions, performance, and activity.'],
  whales:['Whale Copy','Tracked on-chain wallets, copied signals, paper positions, and trader results.'],
  memecoin:['Memecoin Trader','Pump-monitor signals, guarded paper positions, and high-volatility outcomes.'],
  grid:['Grid Trader','SOL grid range, marked wallet, ladder levels, completed cycles, and risk state.'],
  survival:['Xora-Survival','Autonomous advisory cycles with paper incubation and canonical live evidence kept separate.']
};
let TAB='overview';
let RUN_ID=0;
const storage={get:k=>{try{return localStorage.getItem(k)}catch(e){return null}},set:(k,v)=>{try{localStorage.setItem(k,v)}catch(e){}},remove:k=>{try{localStorage.removeItem(k)}catch(e){}}};
function applyTheme(theme){
  const dark=theme==='dark';document.documentElement.dataset.theme=dark?'dark':'light';
  const b=document.getElementById('themeToggle');if(b){b.textContent=dark?'Light mode':'Dark mode';b.setAttribute('aria-pressed',String(dark));}
}
applyTheme(storage.get('mh-theme')||((window.matchMedia&&matchMedia('(prefers-color-scheme:dark)').matches)?'dark':'light'));
function widgetSlug(s){return (s||'widget').toLowerCase().replace(/[^a-z0-9]+/g,'-').replace(/^-|-$/g,'').slice(0,54)}
// Simple tile-resize: each card/kpi gets a resize button visible on hover
// Click cycles: span-4 -> span-6 -> span-8 -> span-12 -> back to span-4
function initTiles(){
  // Widget identity is assigned here so drag/resize and layout save have stable ids.
  const isWidget=x=>x.classList.contains('card')||x.classList.contains('kpi');
  const roots=[document.getElementById('kpis'),document.getElementById('tab-panels'),document.getElementById('coin-wrap')].filter(Boolean);
  roots.forEach(r=>[r,...r.querySelectorAll('.grid'),...r.querySelectorAll('.overview-grid')].forEach(container=>{
    if(!container)return;
    [...container.children].filter(isWidget).forEach((card,i)=>{
      if(!card.dataset.widgetId)card.dataset.widgetId=widgetSlug(card.querySelector('h3')?.textContent||card.querySelector('.lbl')?.textContent||('widget-'+i));
      if(!card.dataset.widgetTitle)card.dataset.widgetTitle=(card.querySelector('h3')?.textContent||card.querySelector('.lbl')?.textContent||card.dataset.widgetId).trim();
      card.classList.remove('tile-s4','tile-s6','tile-s8','tile-s12');
      const old=card.querySelector('.tile-resize');if(old)old.remove();
      if(!card.querySelector(':scope > .widget-report-btn')){
        const bug=document.createElement('button');bug.type='button';bug.className='widget-report-btn';bug.title='Report widget issue';bug.setAttribute('aria-label','Report issue for '+card.dataset.widgetTitle);bug.textContent='🐞';
        bug.addEventListener('click',e=>{e.preventDefault();e.stopPropagation();openWidgetIssue(card);});
        card.appendChild(bug);
      }
    });
  }));
}
// Sidebar collapses as the workspace scrolls down
let scrollRAF=null;
document.querySelector('.workspace')?.addEventListener('scroll',()=>{
  if(scrollRAF)cancelAnimationFrame(scrollRAF);
  scrollRAF=requestAnimationFrame(()=>{
    const ws=document.querySelector('.workspace');
    document.body.classList.toggle('sidebar-scrolled',ws?.scrollTop>40);
  });
});
const fmt=n=>n===null||n===undefined||isNaN(n)?'-':Number(n).toLocaleString(undefined,{maximumFractionDigits:Number(n)<1?6:2});
const fmtMoney=n=>n===null||n===undefined||isNaN(n)?'-':'$'+Number(n).toLocaleString(undefined,{maximumFractionDigits:2});
const fmtTime=t=>{const d=new Date(t*1000);return d.toLocaleString([],{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});};
const chartColor=name=>getComputedStyle(document.documentElement).getPropertyValue(name).trim();
async function load(o){try{const r=await fetch('/api/'+o);if(!r.ok)throw new Error('HTTP '+r.status);return await r.json();}catch(e){return null;}}
function lineChart(id,series,color,fill){
  const cv=document.getElementById(id);if(!cv)return;
  const ctx=cv.getContext('2d');const w=cv.width,h=cv.height;ctx.clearRect(0,0,w,h);
  if(!series||series.length<2)return;
  const vals=series.map(p=>p.px!==undefined?p.px:p.cum!==undefined?p.cum:p).filter(v=>v!==null&&!isNaN(v));
  if(vals.length<2)return;
  const mn=Math.min(...vals),mx=Math.max(...vals),rng=(mx-mn)||1;
  ctx.strokeStyle=color||'#6657df';ctx.lineWidth=1.6;ctx.beginPath();
  for(let i=0;i<vals.length;i++){const x=i/(vals.length-1)*w,y=h-2-((vals[i]-mn)/rng)*(h-26);i?ctx.lineTo(x,y):ctx.moveTo(x,y);}
  ctx.stroke();
  if(fill){ctx.save();ctx.globalAlpha=.12;ctx.lineTo(w,h-2);ctx.lineTo(0,h-2);ctx.closePath();ctx.fillStyle=color||'#6657df';ctx.fill();ctx.restore();}
}
// Candlestick chart (theme: up=lavender, down=red). pxhist only stores single
// quote points, so we bucket them into OHLC candles: open=first px, close=last,
// high=max, low=min. Y = price, X = time. `tf` is the candle timeframe in
// seconds (60=1m .. 604800=W); candles are binned into aligned time windows.
const CANDLE_UP='#6657df',CANDLE_DOWN='#e45567';
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
  ctx.font='10px "IBM Plex Mono"';ctx.fillStyle=chartColor('--chart-ink');
  [0,1,2,3,4].forEach(k=>{
    const val=mn+rng*k/4,yv=y(val);
    ctx.fillText(fmt(val),2,yv+3);
    ctx.strokeStyle=chartColor('--chart-grid');ctx.beginPath();ctx.moveTo(gx,yv);ctx.lineTo(w-8,yv);ctx.stroke();
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
const ER_COLORS=['#6657df','#2d9eb3','#e45567','#dc913a','#18a776','#b957c8'];
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
  ctx.font='10px "IBM Plex Mono"';ctx.fillStyle=chartColor('--chart-ink');
  [0,1,2,3,4].forEach(k=>{
    const val=mn+(mx-mn)*k/4,yv=Y(val);
    ctx.fillText((val>=0?'+':'')+val.toFixed(1)+'%',2,yv+3);
    ctx.strokeStyle=chartColor('--chart-grid');ctx.beginPath();ctx.moveTo(gx,yv);ctx.lineTo(w-8,yv);ctx.stroke();
  });
  [[t0,0],[(t0+t1)/2,.5],[t1,1]].forEach(([t,f])=>ctx.fillText(fmtTime(t),gx+(gw-46)*f,h-8));
  if(mn<0&&mx>0){ctx.strokeStyle=chartColor('--chart-grid');ctx.beginPath();ctx.moveTo(gx,Y(0));ctx.lineTo(w-8,Y(0));ctx.stroke();}
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
  ctx.font='10px "IBM Plex Mono"';ctx.fillStyle=chartColor('--chart-ink');
  [0,1,2,3,4].forEach(k=>{
    const val=Math.round(mx*k/4),yv=gy+(1-k/4)*gh;
    ctx.fillText(String(val),2,yv+3);
    ctx.strokeStyle=chartColor('--chart-grid');ctx.beginPath();ctx.moveTo(gx,yv);ctx.lineTo(w-8,yv);ctx.stroke();
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
  ctx.font='10px "IBM Plex Mono"';ctx.fillStyle=chartColor('--chart-ink');
  [0,1,2,3,4].forEach(k=>{
    const val=mn+(mx-mn)*k/4,yv=Y(val);
    ctx.fillText((val>=0?'+':'')+val.toFixed(1)+'%',2,yv+3);
    ctx.strokeStyle=chartColor('--chart-grid');ctx.beginPath();ctx.moveTo(gx,yv);ctx.lineTo(w-8,yv);ctx.stroke();
  });
  [[t0,0],[(t0+t1)/2,.5],[t1,1]].forEach(([t,f])=>ctx.fillText(fmtTime(t),gx+(gw-46)*f,h-8));
  if(mn<0&&mx>0){ctx.strokeStyle=chartColor('--chart-grid');ctx.beginPath();ctx.moveTo(gx,Y(0));ctx.lineTo(w-8,Y(0));ctx.stroke();}
  ctx.strokeStyle=color||'#6657df';ctx.lineWidth=1.7;ctx.beginPath();
  series.forEach((p,i)=>{const x=X(p.ts),y=Y(p.cum);i?ctx.lineTo(x,y):ctx.moveTo(x,y);});ctx.stroke();
  ctx.fillStyle=color||'#6657df';series.forEach(p=>{ctx.beginPath();ctx.arc(X(p.ts),Y(p.cum),2.1,0,7);ctx.fill();});
  if(lg){const cur=series[series.length-1].cum;
    lg.innerHTML=`<div style="display:flex;gap:8px;align-items:center">
      <span style="width:10px;height:10px;background:${color||'#6657df'};border-radius:2px;display:inline-block"></span>
      <span style="flex:1;font-weight:600;color:${color||'#6657df'}">${name||''}</span>
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

let ISSUE_WIDGET=null;
function widgetIssueSnapshot(card){
  const rect=card.getBoundingClientRect();
  const text=(card.innerText||'').replace(/\s+/g,' ').trim().slice(0,5000);
  const context={
    widget_id:card.dataset.widgetId||'',
    widget_title:card.dataset.widgetTitle||'',
    tab:TAB,
    route:location.pathname,
    viewport:{width:window.innerWidth,height:window.innerHeight},
    bounds:{x:Math.round(rect.x),y:Math.round(rect.y),w:Math.round(rect.width),h:Math.round(rect.height)},
    visible_text:text,
    headings:[...card.querySelectorAll('h3,.lbl,th')].map(x=>(x.textContent||'').trim()).filter(Boolean).slice(0,30),
    classes:[...card.classList].slice(0,20)
  };
  return context;
}
function openWidgetIssue(card){
  ISSUE_WIDGET=card;
  const snap=widgetIssueSnapshot(card);
  document.getElementById('widgetIssueSubtitle').textContent=(snap.widget_title||snap.widget_id)+' · '+TAB;
  document.getElementById('widgetIssueText').value='';
  document.getElementById('widgetIssueSeverity').value='confusing';
  document.getElementById('widgetIssueScreenshot').value='';
  document.getElementById('widgetIssueContext').textContent=JSON.stringify(snap,null,2);
  document.getElementById('widgetIssueModal').classList.add('open');
  setTimeout(()=>document.getElementById('widgetIssueText')?.focus(),50);
}
function closeWidgetIssue(){document.getElementById('widgetIssueModal').classList.remove('open');ISSUE_WIDGET=null;}
function readIssueScreenshot(file){
  return new Promise((resolve,reject)=>{
    if(!file){resolve(null);return;}
    if(!['image/png','image/jpeg','image/webp'].includes(file.type)){reject(new Error('Screenshot must be PNG, JPEG, or WebP'));return;}
    if(file.size>2000000){reject(new Error('Screenshot must be under 2 MB'));return;}
    const rd=new FileReader();rd.onload=()=>resolve({mime:file.type,data:String(rd.result||'')});rd.onerror=()=>reject(new Error('Could not read screenshot'));rd.readAsDataURL(file);
  });
}
async function submitWidgetIssue(){
  if(!ISSUE_WIDGET)return;
  const issue=(document.getElementById('widgetIssueText').value||'').trim();
  if(!issue){showToast('Tell me what looks wrong first');return;}
  const snap=widgetIssueSnapshot(ISSUE_WIDGET);
  let screenshot=null;
  try{screenshot=await readIssueScreenshot(document.getElementById('widgetIssueScreenshot').files[0]);}catch(e){showToast(e.message);return;}
  const payload={widget_id:snap.widget_id,widget_title:snap.widget_title,tab:TAB,route:location.pathname,severity:document.getElementById('widgetIssueSeverity').value,issue,visible_text:snap.visible_text,context:snap,screenshot};
  try{
    const r=await fetch('/api/widget-issues',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    const d=await r.json();
    if(d.ok){showToast('Widget issue reported #'+d.id);closeWidgetIssue();}
    else showToast('Report failed: '+(d.error||'unknown'));
  }catch(e){showToast('Report failed');}
}
document.getElementById('widgetIssueClose')?.addEventListener('click',closeWidgetIssue);
document.getElementById('widgetIssueCancel')?.addEventListener('click',closeWidgetIssue);
document.getElementById('widgetIssueSubmit')?.addEventListener('click',submitWidgetIssue);
document.getElementById('widgetIssueModal')?.addEventListener('click',e=>{if(e.target===e.currentTarget)closeWidgetIssue();});
document.getElementById('clockBtn')?.addEventListener('click',()=>run());
document.getElementById('cancelEditBtn')?.addEventListener('click',()=>cancelEdit());
document.getElementById('addWidgetBtn')?.addEventListener('click',()=>toggleCatalog());
document.getElementById('editToggleBtn')?.addEventListener('click',()=>{if(EDITING){saveLayout();}else{enterEdit();}});
document.getElementById('editSaveBtnFromSettings')?.addEventListener('click',()=>{if(EDITING){saveLayout();}else{showToast('Not editing');}});
document.getElementById('editCancelBtnFromSettings')?.addEventListener('click',()=>{if(EDITING){cancelEdit();}else{showToast('Not editing');}});

document.getElementById('widgetCatalogClose')?.addEventListener('click',()=>closeCatalog());
document.getElementById('widgetCatalogSearch')?.addEventListener('input',function(){const q=this.value.trim().toLowerCase();document.querySelectorAll('#widgetCatalogGrid .wc-item').forEach(el=>{el.style.display=(!q||el.dataset.label.includes(q))?'':'none';});});
document.getElementById('tabNav').addEventListener('click',e=>{
  const b=e.target.closest('button');if(!b)return;
  TAB=b.dataset.tab;
  document.querySelectorAll('#tabNav button').forEach(x=>x.classList.toggle('active',x===b));
  const meta=NAV_META[TAB]||NAV_META.overview;
  document.getElementById('pageTitle').textContent=meta[0];
  document.getElementById('pageSubtitle').textContent=meta[1];
  run();
});
document.getElementById('themeToggle').addEventListener('click',()=>{const next=document.documentElement.dataset.theme==='dark'?'light':'dark';storage.set('mh-theme',next);applyTheme(next);run()});
document.getElementById('settingsBtn').addEventListener('click',()=>openSettings());
async function renderOverview(sum,runId){
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
  if(TAB!=='overview'||runId!==RUN_ID)return;
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
  const actions=await load('actions')||{items:[]};
  const surv=await load('survival')||{};
  const recentTrades=await load('trades?limit=20')||[];
  if(TAB!=='overview'||runId!==RUN_ID)return;
  const actionRows=(actions.items||[]).map(item=>{
    const due=item.due_ts?('Due '+fmtTime(item.due_ts)):item.state;
    const ok=item.state==='ok'||item.state==='live';
    return `<div class="todo-item"><span class="todo-check ${ok?'ok':''}"></span><div><div class="todo-title">${item.title}</div><div class="todo-detail">${due} · ${item.detail}</div></div></div>`;
  }).join('')||'<div class="todo-detail">No action feed available</div>';
  const gateRow=(name,v)=>{const ok=!!v.eligible;const wr=(v.win_rate||0)*100;const cl=v.n||0;
    return `<div class="gate-row"><span class="gate-name">${name}</span><span class="gate-meta ${ok?'pos':'neg'}">${cl} closed · need 20 · ${wr.toFixed(1)}% wr (need 75%)</span><span class="pilltag ${ok?'ok':'no'}">${ok?'READY':'BLOCKED'}</span></div>`;};
  const gd=(sum.grid||{});
  const gridGateRow=`<div class="gate-row"><span class="gate-name">grid</span><span class="gate-meta">${gd.cycles_completed||0} cycles · not gated (own paper wallet)</span><span class="pilltag no">PAPER</span></div>`;
  const sed=(surv.edge||{});const sN=sed.paper_n||0;const sWr=(sed.paper_win_rate||0)*100;const sNet=sed.paper_net_usd||0;
  const sOk=sN>=50&&(sed.paper_win_rate||0)>=0.667&&sNet>0;
  const xoraGateRow=`<div class="gate-row"><span class="gate-name">xora-survival</span><span class="gate-meta ${sOk?'pos':'neg'}">${sN} closed · need 50 · ${sWr.toFixed(1)}% wr (need 66.7%) · net ${sNet>=0?'+':''}${fmtMoney(sNet)}</span><span class="pilltag ${sOk?'ok':'no'}">${sOk?'READY':'BLOCKED'}</span></div>`;
  const gateSummary=Object.entries(lg).map(([name,v])=>gateRow(name,v)).join('')+xoraGateRow+gridGateRow;
  const xPaper=(surv.paper_trades||[]),xWins=xPaper.filter(t=>(t.realized_usd||0)>0).length,xNet=xPaper.reduce((a,t)=>a+(t.realized_usd||0),0);
  const heatCells=Array.from({length:104},(_,i)=>`<span class="heat-cell l${(i*7+xWins)%4}"></span>`).join('');
  const recentRows=recentTrades.map(t=>{
    const pl=t.realized_usd||0,cls=(pl>=0?'pos':'neg'),pct=t.realized_pct!=null?t.realized_pct:null;
    const mode=t.setup==='dynamic_scalper'?'XORA PAPER':(t.setup||'paper');
    const coin=t.symbol||t.coin||'-';
    return `<tr><td>${coin}</td><td>${mode}</td><td>${(t.side||'').toUpperCase()}</td><td>${t.entry_px!=null?fmt(t.entry_px):'-'}</td><td>${t.exit_px!=null?fmt(t.exit_px):'-'}</td><td class="${cls}">${pct!=null?(pct>0?'+':'')+(pct*100).toFixed(2)+'%':'-'}</td><td class="${cls}">${fmtMoney(pl)}</td><td>${t.exit_reason||'-'}</td><td>${t.close_ts?fmtTime(t.close_ts):'-'}</td></tr>`;
  }).join('')||'<tr><td colspan="9" style="color:var(--text-faint)">no recent trade history</td></tr>';
  const overviewCards=`
    <div class="overview-grid" style="margin-bottom:12px">
      <div class="card span-3"><h3>Xora wallet</h3><div class="metric-xl">${(surv.live_positions||[]).length} live</div><div class="mini-note">${(surv.paper_positions||[]).length} paper incubator positions · ${(surv.notional?.paper_usd||0).toFixed(2)} USDC paper notional</div></div>
      <div class="card span-3"><h3>Xora profit</h3><div class="metric-xl ${xNet>=0?'pos':'neg'})">${xNet>=0?'+':''}${fmtMoney(xNet)}</div><div class="mini-note">${xWins}/${xPaper.length||0} paper wins · ${(xPaper.length?xWins/xPaper.length*100:0).toFixed(1)}% win rate</div></div>
      <div class="card span-3"><h3>Best setup</h3><div class="metric-xl">dynamic</div><div class="mini-note">Ask chat for exact live ranking by realised P&L</div></div>
      <div class="card span-3"><h3>What to do</h3><div class="todo-list">${actionRows}</div></div>
      <div class="card span-5"><h3>Last cycles heatmap</h3><div class="heatmap">${heatCells}</div><div class="mini-note" style="margin-top:10px">Brighter cells indicate stronger recent Xora paper/live activity.</div></div>
      <div class="card span-3"><h3>Live gate</h3>${gateSummary}</div>
      <div class="card span-4" id="councilReportCard"><h3>Council report</h3><div class="council-summary" id="councilSummary">Loading latest per-coin review + council...</div></div>
      <div class="card span-4"><h3>Dashboard snippets</h3><div class="todo-list"><div class="todo-item"><span class="todo-check ok"></span><div><div class="todo-title">Traders</div><div class="todo-detail">${Object.keys(lg).length} trader gates tracked</div></div></div><div class="todo-item"><span class="todo-check ok"></span><div><div class="todo-title">Market</div><div class="todo-detail">SOL, JUP, ETH market tabs active</div></div></div><div class="todo-item"><span class="todo-check ${(surv.live_positions||[]).length?'':'ok'}"></span><div><div class="todo-title">Live wallet</div><div class="todo-detail">${(surv.live_positions||[]).length} canonical live positions</div></div></div></div></div>
      <div class="card span-12"><h3>Recent trade history</h3><div class="mini-note" style="margin-bottom:8px">Latest paper/Xora/trader closes from the shared ledger. Live on-chain fills stay separately labelled on Xora-Survival.</div><div class="scroll-wrap"><table><tr><th>Coin</th><th>Source</th><th>Side</th><th>Entry</th><th>Exit</th><th>P/L%</th><th>P/L$</th><th>Reason</th><th>Close</th></tr>${recentRows}</table></div></div>
    </div>`;
  setTimeout(()=>loadCouncilReport(), 0);
  const wlRows=TRADERS.map(t=>{
    const v=lg[t]||{wins:0,losses:0,n:0,win_rate:0};
    return `<tr><td>${t}</td><td>${v.wins||0} : ${v.losses||0}</td><td class="${(v.win_rate||0)>=0.5?'pos':'neg'}">${((v.win_rate||0)*100).toFixed(1)}%</td><td>${v.n||0}</td></tr>`;
  }).join('');
  // grid has no livegate ratio: show cycles as closed count and realized P&L in place of W/L
  const ggg=sum.grid||{};
  const gridWlRow=`<tr><td>grid</td><td>-</td><td>-</td><td>${ggg.cycles_completed||0} cycles</td></tr>`;
  panels.innerHTML=overviewCards+`
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
  if(TAB!=='strategies')return;
  const p=document.getElementById('tab-panels');
  const trades=(await load('trades?limit=30'))||[];
  if(TAB!=='strategies')return;
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
  if(TAB!=='reasoner')return;
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
  const su=await load('survival')||{};
  const sed=su.edge||{};
  const p=document.getElementById('tab-panels');
  const sN=sed.paper_n||0,sWr=(sed.paper_win_rate||0)*100,sNet=sed.paper_net_usd||0;
  const sOk=sN>=50&&(sed.paper_win_rate||0)>=0.667&&sNet>0;
  const traderRow=(t,v)=>{const cl=v.n||0,wr=(v.win_rate||0)*100,ok=!!v.eligible;
    return `<tr><td>${t}</td><td>${cl} / 20</td><td class="${wr>=75?'pos':'neg'}">${wr.toFixed(1)}%</td><td><span class="pilltag ${ok?'ok':'no'}">${ok?'READY FOR LIVE':'NOT ELIGIBLE'}</span><div class="lg-sub">${cl}/20 closed · ${wr.toFixed(1)}% wr (need 75%)</div></td></tr>`;};
  const xoraRow=`<tr><td>xora-survival <span class="lg-sub">(paper incubator)</span></td><td>${sN} / 50</td><td class="${sWr>=66.7?'pos':'neg'}">${sWr.toFixed(1)}%</td><td><span class="pilltag ${sOk?'ok':'no'}">${sOk?'READY FOR LIVE':'NOT ELIGIBLE'}</span><div class="lg-sub">${sN}/50 closed · ${sWr.toFixed(1)}% wr (need 66.7%) · net ${sNet>=0?'+':''}${fmtMoney(sNet)}${sNet>0?'':' (needs positive net)'}</div></td></tr>`;
  const gridRow=`<tr><td>grid <span class="lg-sub">(spot long-only)</span></td><td>${gi.cycles_completed||0} cycles</td><td class="pos">-</td><td><span class="pilltag no">PAPER</span><div class="lg-sub">own paper wallet · excluded from go-live gate</div></td></tr>`;
  p.innerHTML=`<div class="card"><h3>Live Gate &middot; real wallet untouched until a trader passes its evidence bar</h3>
  <div class="livegate-note">Paper traders need &ge;20 closed trades at &ge;75% win-rate. Xora-Survival paper incubator needs 50 closed trades at &ge;66.7% and a positive net. Grid is paper-only and never touches the real wallet.</div>
  <table><tr><th>Trader</th><th>Closed trades</th><th>Win%</th><th>Eligibility</th></tr>
  ${Object.entries(g).map(([t,v])=>traderRow(t,v)).join('')}
  ${xoraRow}
  ${gridRow}
  </table>
  <div style="font-size:11px;color:var(--text-faint);margin-top:6px">Real wallet untouched until a trader clears its bar. Xora-Survival enters through its own live-gate path.</div></div>`;
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
async function renderMiniMarket(runId){
  const m=await load('market')||[]; if(!m||!m.length)return;
  if(TAB!=='overview'||runId!==RUN_ID)return;
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
    ctx.strokeStyle=pxs[pxs.length-1]>=pxs[0]?'#18a776':'#e45567';
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
  ctx.font='9px "IBM Plex Mono"';ctx.fillStyle=chartColor('--chart-ink');
  [0,1,2,3].forEach(k=>{
    const val=mn+(mx-mn)*k/3,yv=pad+(h-2*pad)*k/3;
    ctx.strokeStyle=chartColor('--chart-grid');ctx.beginPath();ctx.moveTo(0,yv);ctx.lineTo(w,yv);ctx.stroke();
    ctx.fillText(fmt(val),2,yv-2);
  });
  // candle chart area
  if(bars.length){
    const bw=Math.max(2,(gw/bars.length)*0.62);
    const Y=v=>gy+(1-(v-mn)/((mx-mn)||1))*gh, X=i=>gx+(i+0.5)*(gw/bars.length);
    bars.forEach((b,i)=>{
      const up=b.close>=b.open,color=up?'#6657df':'#e45567';
      ctx.strokeStyle=color;ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(X(i),Y(b.high));ctx.lineTo(X(i),Y(b.low));ctx.stroke();
      ctx.fillStyle=color;
      const top=Math.min(Y(b.open),Y(b.close)),bot=Math.max(Y(b.open),Y(b.close)),bh=Math.max(1,bot-top);
      ctx.fillRect(X(i)-bw/2,top,bw,bh);
    });
    // x time labels
    ctx.font='9px "IBM Plex Mono"';ctx.fillStyle=chartColor('--chart-ink');
    const b0=bars[0],bN=bars[bars.length-1],mid=bars[Math.floor(bars.length/2)];
    ctx.fillText(fmtTime(b0.ts),pad,h-14);
    ctx.fillText(fmtTime(mid.ts),pad+(gw-60)/2,h-14);
    ctx.textAlign='right';ctx.fillText(fmtTime(bN.ts),w,h-14);ctx.textAlign='left';
  } else {
    ctx.font='11px "IBM Plex Mono"';ctx.fillStyle=chartColor('--chart-ink');
    ctx.fillText('Binance history unavailable · showing levels + live price',pad+8,pad+30);
  }
  // grid level lines (drawn over candles)
  const lv=ladderLevels(lo,hi,(g.grid_levels&&g.grid_levels>=4?g.grid_levels+1:9));
  lv.forEach((lv0,i)=>{
    const s=lv0>=cx, y=q(lv0);
    ctx.strokeStyle=s?'rgba(74,222,128,.6)':'rgba(255,93,162,.55)';
    ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(0,y);ctx.lineTo(w-6,y);ctx.stroke();
    ctx.fillStyle=chartColor('--chart-ink');ctx.font='bold 9px "IBM Plex Mono"';
    ctx.fillText(s?'SELL':'buy',4,y-2);
    ctx.textAlign='right';ctx.fillText('$'+fmt(lv0),w-4,y-2);ctx.textAlign='left';
  });
  // center
  ctx.strokeStyle='#6657df';ctx.setLineDash([4,4]);ctx.lineWidth=1.4;
  ctx.beginPath();ctx.moveTo(0,q(cx));ctx.lineTo(w-6,q(cx));ctx.stroke();ctx.setLineDash([]);
  ctx.fillStyle='#6657df';ctx.font='600 10px "IBM Plex Mono"';ctx.fillText('center $'+fmt(cx),4,q(cx)-8);
  // current price marker
  const yy=q(last);
  ctx.fillStyle=last>=cx?'#18a776':'#e45567';ctx.font='600 10px "IBM Plex Mono"';
  ctx.fillText('▲ $'+fmt(last),w-70,yy-6);
  ctx.fillRect(w-6,yy-1.5,4,3);
}
// auto-refresh market + ladder while Overview is open
setInterval(()=>{if(TAB==='overview'){renderMiniMarket(RUN_ID);}},8000);
function renderKpis(sum){
  const k=document.getElementById('kpis');
  const traders=(sum.traders||[]);
  const fx=(traders[0]&&traders[0].fx_nzd_per_usd)||1.67;
  const traderBy=name=>traders.find(t=>t.trader===name)||{};
  const pct=t=>t.started>0?((t.equity-t.started)/t.started*100):0;
  const compactWallet=(id,label,t)=>`<button class="wallet-tab ${pct(t)>=0?'good':'bad'}" data-wallet-tab="${id}" type="button">
      <span>${label}</span><b>NZ${fmtMoney(t.equity_nzd||0)}</b><small class="${pct(t)>=0?'pos':'neg'}">${pct(t)>=0?'+':''}${pct(t).toFixed(2)}%</small>
    </button>`;
  const xoraTab=`<button class="wallet-tab good" data-wallet-tab="survival" type="button"><span>Xora-Survival</span><b id="xora-tab-balance">live 0</b><small>paper incubator</small></button>`;
  const grid=sum.grid||{},gw=grid.wallet||{},gg=grid.grid||{};
  const gridTab=`<button class="wallet-tab good" data-wallet-tab="grid" type="button"><span>Grid</span><b>NZ${fmtMoney((grid.equity_usd||0)*fx)}</b><small>${gw.paused?'paused':'active'} · ${grid.open_sells||0} sells</small></button>`;
  const totalEquity=traders.reduce((a,t)=>a+(t.equity_nzd||0),0)+((grid.equity_usd||0)*fx);
  const totalStarted=traders.reduce((a,t)=>a+(t.started_nzd||0),0)+((grid.started_usd||0)*fx);
  const totalCommitted=traders.reduce((a,t)=>a+((t.committed||0)*fx),0)+((gw.sol_qty||0)*(gg.center_px||0)*fx);
  const totalAvailable=traders.reduce((a,t)=>a+((t.available||0)*fx),0)+((gw.cash_usd||0)*fx);
  const collectivePct=totalStarted>0?((totalEquity-totalStarted)/totalStarted*100):0;
  const detailRows=traders.map(t=>`<tr data-wallet-detail="${t.trader}"><td>${t.trader}</td><td>NZ${fmtMoney(t.equity_nzd||0)}</td><td class="${pct(t)>=0?'pos':'neg'}">${pct(t)>=0?'+':''}${pct(t).toFixed(2)}%</td><td>NZ${fmtMoney((t.available||0)*fx)}</td><td>NZ${fmtMoney((t.committed||0)*fx)}</td></tr>`).join('')+
    `<tr data-wallet-detail="grid"><td>grid</td><td>NZ${fmtMoney((grid.equity_usd||0)*fx)}</td><td class="pos">cycles ${grid.cycles_completed||0}</td><td>NZ${fmtMoney((gw.cash_usd||0)*fx)}</td><td>SOL ${fmt(gw.sol_qty||0)}</td></tr>`+
    `<tr data-wallet-detail="survival"><td>xora-survival</td><td id="xora-wallet-equity">loading</td><td id="xora-wallet-edge">paper gate</td><td id="xora-wallet-live">live wallet</td><td id="xora-wallet-paper">paper</td></tr>`;
  const op=sum.totals?.open_positions||0;
  k.innerHTML=`<div class="wallet-hub kpi widget-wide">
    <div class="wallet-hub-head"><div><div class="lbl">Collective wallet</div><div class="val ${collectivePct>=0?'pos':'neg'}">NZ${fmtMoney(totalEquity)}</div><div class="kpi-detail">${collectivePct>=0?'+':''}${collectivePct.toFixed(2)}% vs starting funds · ${op} open positions</div></div><div class="wallet-summary"><span>Available <b>NZ${fmtMoney(totalAvailable)}</b></span><span>Committed <b>NZ${fmtMoney(totalCommitted)}</b></span></div></div>
    <div class="wallet-tabs">${compactWallet('scalper','Scalper',traderBy('scalper'))}${compactWallet('reasoner','Reasoner',traderBy('reasoner'))}${compactWallet('whale_trader','Whale',traderBy('whale_trader'))}${compactWallet('memecoin_trader','Memecoin',traderBy('memecoin_trader'))}${gridTab}${xoraTab}</div>
    <div class="scroll-wrap" style="max-height:190px;margin-top:12px"><table><tr><th>Wallet</th><th>Equity</th><th>Performance</th><th>Available</th><th>Committed / holdings</th></tr>${detailRows}</table></div>
  </div>`;
  k.querySelectorAll('[data-wallet-tab]').forEach(btn=>btn.addEventListener('click',()=>{
    const id=btn.dataset.walletTab;
    k.querySelectorAll('.wallet-tab').forEach(x=>x.classList.toggle('active',x===btn));
    k.querySelectorAll('[data-wallet-detail]').forEach(row=>row.style.display=(row.dataset.walletDetail===id||id==='all')?'':'none');
  }));
  fetch('/api/survival').then(r=>r.json()).then(s=>{
    const paper=s.paper_positions||[],live=s.live_positions||[],tr=s.paper_trades||[];
    const net=tr.reduce((a,t)=>a+(t.realized_usd||0),0)*fx;
    const wins=tr.filter(t=>(t.realized_usd||0)>0).length;
    const tab=document.getElementById('xora-tab-balance');if(tab)tab.textContent=`${live.length} live · ${paper.length} paper`;
    const eq=document.getElementById('xora-wallet-equity');if(eq)eq.textContent=`NZ${fmtMoney((s.notional?.live_usd||0)*fx)} live`;
    const stUsd=(s.edge?.starting_equity_usd)||10;const stNzd=stUsd*fx;
    const netPct=stNzd>0?((net/stNzd)*100):0;
    const edge=document.getElementById('xora-wallet-edge');if(edge){edge.textContent=`started NZ${fmtMoney(stNzd)} · ${netPct>=0?'+':''}${netPct.toFixed(2)}% vs start`;edge.className=netPct>=0?'pos':'neg';}
    const l=document.getElementById('xora-wallet-live');if(l)l.textContent=`${live.length} live positions`;
    const p=document.getElementById('xora-wallet-paper');if(p)p.textContent=`${paper.length} paper positions · NZ${fmtMoney((s.notional?.paper_usd||0)*fx)}`;
  }).catch(()=>{});
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
    <div class="card"><h3>Exit reasons &middot; paper incubator</h3><table><tr><th>Reason</th><th>Count</th></tr>${reasonRows}</table></div>
    <div class="card"><h3>Open live wallet positions</h3><table><tr><th>Ticker</th><th>Mint</th><th>Qty</th><th>Entry</th><th>Notional</th><th>Peak</th><th>Mode</th></tr>${liveRows}</table></div>
  </div>
  <div class="card"><h3>Open paper incubator positions</h3><table><tr><th>Ticker</th><th>Mint</th><th>Qty</th><th>Entry</th><th>Notional</th><th>Peak</th><th>Mode</th></tr>${paperRows}</table></div>
  <div class="card"><h3>Active exit params &middot; evidence-gated policy</h3><table><tr><th>Class</th><th>Take profit</th><th>Stop loss</th><th>Trail arm</th><th>Trail distance</th><th>Max hold</th><th>Source</th></tr>
    ${((s.risk_params||{}).MEME?'<tr><td><span class="pilltag no">MEME</span></td><td>'+(s.risk_params.MEME.take_profit_pct*100).toFixed(1)+'%</td><td>'+(s.risk_params.MEME.stop_loss_pct*100).toFixed(1)+'%</td><td>+'+(s.risk_params.MEME.trail_arm_pct*100).toFixed(1)+'%</td><td>'+(s.risk_params.MEME.trail_distance_pct*100).toFixed(1)+'% below peak</td><td>'+Math.round(s.risk_params.MEME.max_hold_seconds/60)+' min TP-miss fallback</td><td>'+(s.risk_params.MEME.source||'default')+'</td></tr>':'')}
    ${((s.risk_params||{}).SERIOUS?'<tr><td><span class="pilltag ok">SERIOUS</span></td><td>'+(s.risk_params.SERIOUS.take_profit_pct*100).toFixed(1)+'%</td><td>'+(s.risk_params.SERIOUS.stop_loss_pct*100).toFixed(1)+'%</td><td>+'+(s.risk_params.SERIOUS.trail_arm_pct*100).toFixed(1)+'%</td><td>'+(s.risk_params.SERIOUS.trail_distance_pct*100).toFixed(1)+'% below peak</td><td>'+Math.round(s.risk_params.SERIOUS.max_hold_seconds/3600*10)/10+' h TP-miss fallback</td><td>'+(s.risk_params.SERIOUS.source||'default')+'</td></tr>':'')}
  </table><div style="font-size:10.5px;color:var(--text-faint);margin-top:6px">MEME trailing protection arms only after a meaningful +8% move and permits a 4% pullback from peak. Max hold only retries an exit after the recorded peak crossed TP but the sell did not complete. Age alone never closes a position. Stop loss remains unconditional. Tuned candidates can control paper exits, but live promotion remains locked.</div></div>
  <div class="card"><h3>Survival &middot; live trade history (on-chain)</h3><div class="scroll-wrap"><table><tr><th>Coin</th><th>Side</th><th>When</th><th>Signature</th><th>State</th><th>Order</th></tr>${liveTr}</table></div></div>
  <div class="card"><h3>Paper incubator &middot; trade history</h3><div class="scroll-wrap"><table><tr><th>Coin</th><th>Side</th><th>Entry</th><th>Exit</th><th>P/L%</th><th>P/L$ (NZD)</th><th>Reason</th><th>Close</th><th>Mode</th></tr>${paperTr}</table></div></div>
  <div class="card"><h3>Decision log &middot; autonomous cycles</h3><div class="scroll-wrap"><table><tr><th>When</th><th>Action</th><th>Asset</th><th>State</th><th>Reason</th></tr>${cyc}</table></div></div>`;
}
async function run(){
  const runId=++RUN_ID;
  const sum=await load('summary');
  if(runId!==RUN_ID)return;
  if(!sum){document.getElementById('tab-panels').innerHTML='<div class="load-error">Dashboard data could not be loaded. Existing trading processes are not affected.</div>';return;}
  renderKpis(sum);renderTicker(sum);
  const cw=document.getElementById('coin-wrap');
  if(TAB==='overview'){await renderMiniMarket(runId);await renderOverview(sum,runId);}
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
  if(runId!==RUN_ID)return;
  ensureCatalogWidgets();
  initTiles();petAttachWidgetTips();
  applySavedLayoutToView();
}
applyTheme(document.documentElement.dataset.theme);

// ---- Free-position layout editor (edit mode) ----
let EDITING=false;
const EDIT_ROOTS=['kpis','tab-panels','coin-wrap'];
const CATALOG_WIDGETS=[
  {group:'System / Overview',types:{
    Summary:[
      ['Collective Wallet','all trader wallets and equity vs start'],
      ['Xora Wallet','live vs paper survival notional split'],
      ['Xora Profit','Xora survival P&L and edge'],
      ['Best Setup','highest-scoring strategy setup'],
      ['What To Do','ranked next best action'],
      ['Dashboard Snippets','quick reference snippets and metrics'],
      ['Last Cycles Heatmap','recent Xora activity heatmap']
    ],
    Charts:[
      ['Cumulative Edge Per Trader','cumulative edge chart across traders'],
      ['Win Loss Mix Ratio Per Trader','win/loss breakdown across closed trades'],
      ['Exit Reasons','why positions closed across the system']
    ],
    Trading:[
      ['Recent Trade History','latest paper/Xora/trader closes from the shared ledger'],
      ['Traders Coins Live Positions W L','trader × coin positions and win/loss matrix'],
      ['Live Gate','evidence-gated live activation status']
    ],
    Admin:[
      ['Council Report','per-coin shadow review + council summary'],
      ['Council Decisions','council decision log'],
      ['Request Queue','pending improvement requests'],
      ['Agent Health','agent process health stats'],
      ['System Audit','append-only audit trail info'],
      ['Future Widget','placeholder slot for upcoming widgets']
    ]
  }},
  {group:'Xora-Survival',types:{
    Summary:[
      ['Live Wallet Xora Survival Real On Chain Fills','canonical on-chain survival fills only'],
      ['Paper Incubator Shadow Go Live Gate','paper incubator evidence and live gate'],
      ['Active Exit Params Evidence Gated Policy','TP, SL, trail, max-hold policy source']
    ],
    Positions:[
      ['Open Live Wallet Positions','canonical live survival wallet positions'],
      ['Open Paper Incubator Positions','paper incubator open positions']
    ],
    History:[
      ['Survival Live Trade History On Chain','real on-chain survival fill history'],
      ['Paper Incubator Trade History','closed paper incubator trade history'],
      ['Decision Log Autonomous Cycles','autonomous decision cycle log'],
      ['Exit Reasons Paper Incubator','paper incubator exit reason counts']
    ]
  }},
  {group:'Scalper',types:{
    Summary:[['Scalper Fast Momentum Layer Own Wallet','fast momentum scalper overview'],['What It Does Scalper','scalper strategy explanation']],
    Tables:[['Scalper Wallet Strategy Rotations','strategy rotation points and win rates']],
    Charts:[['Exit Reasons Scalper','scalper exit reason chart'],['Cumulative Edge Scalper','scalper cumulative edge chart']],
    History:[['Recent Trades Scalper','recent scalper trade history']]
  }},
  {group:'Reasoner',types:{
    Summary:[['Reasoner Slow News Swing Own Wallet','reasoner overview'],['What It Does Reasoner','reasoner strategy explanation']],
    Tables:[['Reasoner Wallet','reasoner wallet and bias table']],
    Charts:[['Exit Reasons Reasoner','reasoner exit reason chart'],['Cumulative Edge Reasoner','reasoner cumulative edge chart']],
    History:[['Recent Trades Reasoner','recent reasoner trade history']]
  }},
  {group:'Whale Copy',types:{
    Summary:[['Whales Fomo Family Copy Trade Own Wallet','whale copy overview'],['Whale Trader Wallet','whale trader wallet state']],
    Tables:[['Tracked Wallets Curated Activity','tracked whale wallet activity'],['Open Whale Positions','open whale copy positions']],
    History:[['Recent Whale Buys On Chain','recent tracked on-chain whale buys'],['Whale Trader Recent Trades','whale trader recent trade history']]
  }},
  {group:'Memecoin',types:{
    Summary:[['Memecoin Long Only Paper Sniper Own Wallet','memecoin sniper overview'],['Memecoin Wallet','memecoin trader wallet state']],
    Positions:[['Open Memecoin Positions','open memecoin positions']],
    Signals:[['Recent Memecoin Signals Pump Monitor','pump-monitor memecoin signals']],
    History:[['Memecoin Trader Recent Trades','memecoin trader recent trade history']]
  }},
  {group:'Grid',types:{
    Summary:[['Grid SOL Spot Long Only Geometric Grid','grid trader overview'],['Grid Wallet','grid wallet state'],['What It Does Grid','grid strategy explanation']],
    Charts:[['Grid Ladder Live Price Vs Levels','grid ladder and live price chart'],['Closed Cycles Grid Exits','closed grid cycles'],['Cumulative Edge Grid','grid cumulative edge chart']],
    History:[['Recent Trades Grid','recent grid trade history']]
  }},
  {group:'Market',types:{
    Charts:[['Live Market','market price chart'],['Live Market Tick','overview live market mini-widget'],['Market Ticker','top ticker tape']]
  }},
  {group:'Live Gate',types:{
    Summary:[['Live Gate Real Wallet Untouched Until A Trader Passes Its Evidence Bar','deterministic live gate table'],['Risk Params','enforced risk parameters']]
  }}
];
const isWidget=x=>x&&(x.classList.contains('card')||x.classList.contains('kpi')||x.classList.contains('widget-free'));
function widgetIdOf(card){if(card.dataset.widgetId)return card.dataset.widgetId;return card.dataset.widgetId=widgetSlug(card.querySelector('h3')?.textContent||card.querySelector('.lbl')?.textContent||('widget-'+Math.floor(Math.random()*1e6)));}
function layoutForTab(){
  if(window._draftLayout)return window._draftLayout;
  const p=Object.assign({},window._mhPrefs||{});p.layout=p.layout||{};const cur=p.layout[TAB]||{};
  // Layout is stored per-tab as a flat widgetId -> spec map (matches server).
  const ws=Object.assign({},cur);
  if(cur.widgets&&typeof cur.widgets==='object'&&!cur.x)return Object.assign({},cur.widgets);
  return ws;
}
async function persistLayout(layout){
  const p=Object.assign({},window._mhPrefs||{});p.layout=p.layout||{};p.layout[TAB]=layout;
  try{const r=await fetch('/api/ui/prefs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(p)});const d=await r.json();if(d.ok){window._mhPrefs=d.prefs;showToast('Layout saved');return true;}showToast('Error: '+d.error);}catch(e){showToast('Save failed');}
  return false;
}
function freeRoots(){return EDIT_ROOTS.map(id=>document.getElementById(id)).filter(Boolean);}
function toggleCatalog(){
  const el=document.getElementById('widgetCatalog');if(!el)return;
  el.classList.toggle('open');
  if(el.classList.contains('open'))buildCatalog();
}
function closeCatalog(){const el=document.getElementById('widgetCatalog');if(el)el.classList.remove('open');}
function buildCatalog(){
  const grid=document.getElementById('widgetCatalogGrid');if(!grid)return;
  grid.innerHTML=CATALOG_WIDGETS.map(section=>{
    const types=Object.entries(section.types||{}).map(([type,items])=>`<div class="wc-type"><div class="wc-type-title">${escHtml(type)}</div><div class="wc-type-grid">${items.map(([label,hint])=>`<div class="wc-item" draggable="true" data-label="${label.toLowerCase()}" data-widget="${widgetSlug(label)}" data-hint="${escHtml(hint)}" data-group="${escHtml(section.group)}" data-type="${escHtml(type)}"><b>${label}</b><small>${hint}</small><span class="wc-add">&#43;</span></div>`).join('')}</div></div>`).join('');
    return `<section class="wc-section"><div class="wc-section-title">${escHtml(section.group)}</div>${types}</section>`;
  }).join('');
  grid.querySelectorAll('.wc-item').forEach(item=>{
    item.addEventListener('click',()=>addCatalogWidget(item.dataset.widget,item.querySelector('b').textContent,item.dataset.hint));
    item.addEventListener('dragstart',e=>{
      e.dataTransfer.effectAllowed='copy';
      e.dataTransfer.setData('application/x-mh-widget',JSON.stringify({id:item.dataset.widget,label:item.querySelector('b').textContent,hint:item.dataset.hint||''}));
    });
  });
}
function catalogWidgetId(base){
  base=widgetSlug(base||'catalog-widget');
  const used=new Set([...document.querySelectorAll('[data-widget-id]')].map(x=>x.dataset.widgetId));
  const lay=layoutForTab();Object.keys(lay||{}).forEach(k=>used.add(k));
  if(!used.has(base))return base;
  for(let i=2;i<200;i++){const id=base+'-'+i;if(!used.has(id))return id;}
  return base+'-'+Date.now();
}
function createCatalogWidget(id,title,hint){
  const card=document.createElement('div');card.className='card widget-free';card.dataset.widgetId=id;card.dataset.widgetTitle=title;card.dataset.catalogWidget='1';
  card.innerHTML='<h3>'+escHtml(title)+'</h3><div class="widget-free-note" data-catalog-body="1">Loading live widget data...</div>';
  renderCatalogWidgetBody(card,title,hint);
  return card;
}
function miniRows(rows,cols,empty){
  if(!rows||!rows.length)return '<div class="widget-free-note">'+escHtml(empty||'No live rows available yet.')+'</div>';
  return '<div class="scroll-wrap"><table><tr>'+cols.map(c=>'<th>'+escHtml(c[0])+'</th>').join('')+'</tr>'+rows.slice(0,50).map(r=>'<tr>'+cols.map(c=>'<td>'+escHtml(c[1](r))+'</td>').join('')+'</tr>').join('')+'</table></div>';
}
async function renderCatalogWidgetBody(card,title,hint){
  const body=card.querySelector('[data-catalog-body]');if(!body)return;
  const slug=widgetSlug(title||card.dataset.widgetId||'');
  try{
    if(slug.includes('tracked-wallets')){const w=await load('whales')||{};body.innerHTML=miniRows(w.wallets||[],[['Name',r=>r.name||'-'],['Handle',r=>r.handle||'-'],['Status',r=>r.last_sig_ts?'ACTIVE':'QUIET'],['Tx7d',r=>r.tx_7d||0]],'No tracked wallet rows yet.');return;}
    if(slug.includes('recent-whale-buys')){const w=await load('whales')||{};body.innerHTML=miniRows(w.events||[],[['Whale',r=>r.wallet||r.name||'-'],['Coin',r=>r.symbol||r.coin||'-'],['Dir',r=>r.side||'BUY'],['When',r=>r.ts?fmtTime(r.ts):'-']], 'No recent whale buys yet.');return;}
    if(slug.includes('whale-trader-recent-trades')){const w=await load('whales')||{};body.innerHTML=miniRows(w.trades||[],[['Coin',r=>r.symbol||r.coin||'-'],['P/L',r=>fmtMoney(r.realized_usd||0)],['Reason',r=>r.exit_reason||'-'],['Close',r=>r.close_ts?fmtTime(r.close_ts):'-']], 'No whale trades yet.');return;}
    if(slug.includes('whale-trader-wallet')){const w=await load('whales')||{};const a=w.account||{},fx=a.fx||1.67;body.innerHTML=`<div class="xora-sum-grid"><div class="xora-sum-col"><span class="xora-metric-label">Equity</span><span class="xora-metric-val">NZ${fmtMoney((a.equity||0)*fx)}</span></div><div class="xora-sum-col"><span class="xora-metric-label">Available</span><span class="xora-metric-val">NZ${fmtMoney((a.available||0)*fx)}</span></div><div class="xora-sum-col"><span class="xora-metric-label">Open</span><span class="xora-metric-val">${(w.positions||[]).length}</span></div></div>`;return;}
    if(slug.includes('open-whale-positions')){const w=await load('whales')||{};body.innerHTML=miniRows(w.positions||[],[['Coin',r=>r.symbol||r.coin||'-'],['Side',r=>r.side||'-'],['Entry',r=>fmt(r.entry||r.entry_px)],['Qty',r=>fmt(r.qty)]], 'No open whale positions.');return;}
    if(slug.includes('recent-memecoin-signals')){const m=await load('memecoin')||{};body.innerHTML=miniRows(m.signals||[],[['Token',r=>r.symbol||r.token||'-'],['Dir',r=>r.direction||r.side||'-'],['Conf',r=>r.confidence!=null?(r.confidence*100).toFixed(0)+'%':'-'],['When',r=>r.ts?fmtTime(r.ts):'-']], 'No memecoin signals yet.');return;}
    if(slug.includes('memecoin-trader-recent-trades')){const m=await load('memecoin')||{};body.innerHTML=miniRows(m.trades||[],[['Coin',r=>r.symbol||r.coin||'-'],['P/L',r=>fmtMoney(r.realized_usd||0)],['Reason',r=>r.exit_reason||'-'],['Close',r=>r.close_ts?fmtTime(r.close_ts):'-']], 'No memecoin trades yet.');return;}
    if(slug.includes('memecoin-wallet')){const m=await load('memecoin')||{};const a=m.account||{},fx=a.fx||1.67;body.innerHTML=`<div class="xora-sum-grid"><div class="xora-sum-col"><span class="xora-metric-label">Equity</span><span class="xora-metric-val">NZ${fmtMoney((a.equity||0)*fx)}</span></div><div class="xora-sum-col"><span class="xora-metric-label">Available</span><span class="xora-metric-val">NZ${fmtMoney((a.available||0)*fx)}</span></div><div class="xora-sum-col"><span class="xora-metric-label">Open</span><span class="xora-metric-val">${(m.positions||[]).length}</span></div></div>`;return;}
    if(slug.includes('open-memecoin-positions')){const m=await load('memecoin')||{};body.innerHTML=miniRows(m.positions||[],[['Coin',r=>r.symbol||r.coin||'-'],['Side',r=>r.side||'-'],['Entry',r=>fmt(r.entry||r.entry_px)],['Qty',r=>fmt(r.qty)]], 'No open memecoin positions.');return;}
    if(slug.includes('grid-wallet')||slug.includes('grid-sol')||slug.includes('grid-ladder')){const g=await load('grid')||{};const w=g.wallet||{},st=g.grid||{},fx=g.fx_nzd_per_usd||1.67;body.innerHTML=`<div class="xora-sum-grid"><div class="xora-sum-col"><span class="xora-metric-label">Equity</span><span class="xora-metric-val">NZ${fmtMoney((g.equity_usd||0)*fx)}</span></div><div class="xora-sum-col"><span class="xora-metric-label">SOL held</span><span class="xora-metric-val">${fmt(w.sol_qty||0)}</span></div><div class="xora-sum-col"><span class="xora-metric-label">Range</span><span class="xora-metric-val">${fmt(st.range_low)}-${fmt(st.range_high)}</span></div></div>`;return;}
    if(slug.includes('recent-trades-grid')){const g=await load('grid')||{};body.innerHTML=miniRows(g.recent_trades||[],[['Side',r=>r.side||'-'],['Level',r=>fmt(r.level_px)],['Qty',r=>fmt(r.qty)],['Realized',r=>r.realized_usd!=null?fmtMoney(r.realized_usd):'-']], 'No grid trades yet.');return;}
    if(slug.includes('reasoner-wallet')){const r=await load('reasoner')||{};body.innerHTML=miniRows(r.accounts||[],[['Trader',x=>'reasoner'],['Equity',x=>fmtMoney(x.equity||0)],['Avail',x=>fmtMoney(x.available||0)],['Committed',x=>fmtMoney(x.committed||0)]], 'No reasoner wallet row.');return;}
    if(slug.includes('paper-incubator-trade-history')){const s=await load('survival')||{};body.innerHTML=miniRows(s.paper_trades||[],[['Coin',r=>r.symbol||r.coin||'-'],['P/L',r=>fmtMoney((r.realized_usd||0)*1.67)],['Reason',r=>r.exit_reason||'-'],['Close',r=>r.close_ts?fmtTime(r.close_ts):'-']], 'No paper incubator trades yet.');return;}
    if(slug.includes('survival-live-trade-history')){const s=await load('survival')||{};body.innerHTML=miniRows(s.live_trades||[],[['Coin',r=>r.coin||r.symbol||'-'],['Side',r=>r.side||'-'],['When',r=>r.ts?fmtTime(r.ts):'-'],['Sig',r=>r.signature?sigShort(r.signature):'-']], 'No live survival fills yet.');return;}
    if(slug.includes('live-gate')){const g=await load('livegate')||{};body.innerHTML=miniRows(Object.entries(g).map(([k,v])=>Object.assign({name:k},v)),[['Trader',r=>r.name],['Closed',r=>r.n||0],['Win%',r=>((r.win_rate||0)*100).toFixed(1)+'%'],['State',r=>r.eligible?'READY':'BLOCKED']], 'No live gate rows.');return;}
    if(slug.includes('recent-trade-history')||slug.includes('recent-trades')){const t=await load('trades?limit=12')||[];body.innerHTML=miniRows(t, [['Coin',r=>r.symbol||r.coin||'-'],['Setup',r=>r.setup||'-'],['P/L',r=>fmtMoney(r.realized_usd||0)],['Reason',r=>r.exit_reason||'-']], 'No recent trades.');return;}
    if(slug.includes('xora-wallet')||slug.includes('xora-profit')||slug.includes('collective-wallet')){const s=await load('survival')||{};const e=s.edge||{};body.innerHTML=`<div class="xora-sum-grid"><div class="xora-sum-col"><span class="xora-metric-label">Paper net</span><span class="xora-metric-val">${fmtMoney(e.paper_net_usd||0)}</span></div><div class="xora-sum-col"><span class="xora-metric-label">Closed</span><span class="xora-metric-val">${e.paper_n||0}</span></div><div class="xora-sum-col"><span class="xora-metric-label">Win rate</span><span class="xora-metric-val">${((e.paper_win_rate||0)*100).toFixed(1)}%</span></div></div>`;return;}
    body.innerHTML='<div class="widget-free-note"><b>'+escHtml(hint||'Live widget slot')+'</b><br>Added widget. Drag, resize, and save it. This widget has no compact snapshot renderer yet.</div>';
  }catch(e){body.innerHTML='<div class="load-error">Widget data could not be loaded.</div>';}
}
function ensureCatalogWidgets(){
  const root=document.getElementById('tab-panels');if(!root)return;
  const lay=layoutForTab()||{};
  Object.entries(lay).forEach(([id,st])=>{
    if(!st||st.type!=='catalog'||document.querySelector('[data-widget-id="'+CSS.escape(id)+'"]'))return;
    const card=createCatalogWidget(id,st.title||id,st.hint||'Custom dashboard widget slot.');
    root.appendChild(card);
  });
}
function addCatalogWidget(base,label,hint,point){
  if(!EDITING){showToast('Turn on Edit layout first');return null;}
  const root=document.getElementById('tab-panels')||document.getElementById('coin-wrap')||document.getElementById('kpis');if(!root)return null;
  const id=catalogWidgetId(base||label);const title=label||id;
  const card=createCatalogWidget(id,title,hint);
  root.appendChild(card);
  const r=root.getBoundingClientRect();
  const lay=layoutForTab();
  const x=point?Math.max(0,Math.min(point.x-r.left,Math.max(0,r.width-220))):24+(Object.keys(lay).length%4)*36;
  const y=point?Math.max(0,Math.min(point.y-r.top,Math.max(0,r.height-120))):24+(Object.keys(lay).length%6)*34;
  lay[id]={type:'catalog',title:title,hint:hint||'',x:Math.round(x),y:Math.round(y),w:320,h:150,hidden:false,width:'normal',order:Object.keys(lay).length};
  window._draftLayout=lay;
  attachFreeHandles();
  applyWidgetStyle(card,root,lay[id]);
  closeCatalog();
  showToast('Added “'+title+'” — drag it where you want, then Save Layout.');
  return card;
}
function attachCatalogDropTargets(){
  freeRoots().forEach(root=>{
    if(!root||root.dataset.catalogDropBound==='1')return;root.dataset.catalogDropBound='1';
    root.addEventListener('dragover',e=>{if(!EDITING||!e.dataTransfer.types.includes('application/x-mh-widget'))return;e.preventDefault();root.classList.add('drop-armed');});
    root.addEventListener('dragleave',()=>root.classList.remove('drop-armed'));
    root.addEventListener('drop',e=>{
      root.classList.remove('drop-armed');
      if(!EDITING)return;
      const raw=e.dataTransfer.getData('application/x-mh-widget');if(!raw)return;
      e.preventDefault();
      try{const data=JSON.parse(raw);addCatalogWidget(data.id,data.label,data.hint,{x:e.clientX,y:e.clientY});}catch(err){showToast('Could not add widget');}
    });
  });
}
function freeWidgets(){
  const out=[];
  freeRoots().forEach(r=>{if(r)r.querySelectorAll('.card,.kpi,.widget-free').forEach(w=>out.push({w,root:r}));});
  return out;
}
function enterEdit(){
  if(EDITING)return;if(!window._mhPrefs)window._mhPrefs={};
  window._editBaseline=JSON.stringify(layoutForTab());
  // Capture each widget's current on-screen position BEFORE the free-edit class
  // flips cards to absolute, so entering edit mode never stacks or moves them.
  captureFreePositions();
  EDITING=true;document.body.classList.add('free-edit');
  const cb=document.getElementById('cancelEditBtn');if(cb)cb.style.display='inline-flex';
  const aw=document.getElementById('addWidgetBtn');if(aw)aw.style.display='inline-flex';
  toggleEditPrefs(true);
  ensureCatalogWidgets();
  attachFreeHandles();
  attachCatalogDropTargets();
  showToast('Edit mode: drag a widget by its top bar, resize from the bottom-right grip, hide with the eye. Save to keep.');
}
function captureFreePositions(){
  const lay=layoutForTab();
  if(!window._draftLayout)window._draftLayout=lay;
  freeWidgets().forEach(({w,root})=>{
    if(!root)return;
    const wr=w.getBoundingClientRect(),rr=root.getBoundingClientRect();
    const id=widgetIdOf(w);const st=lay[id]=lay[id]||{};
    if(st.x==null&&st.x!==0){st.x=Math.round(wr.left-rr.left);}
    if(st.y==null&&st.y!==0){st.y=Math.round(wr.top-rr.top);}
    if(!st.w){st.w=Math.round(wr.width);}
    if(!st.h){st.h=Math.round(wr.height);}
  });
  window._draftLayout=lay;
}
function exitEdit(){
  if(!EDITING)return;EDITING=false;
  detachFreeHandles();
  document.body.classList.remove('free-edit');
  const cb=document.getElementById('cancelEditBtn');if(cb)cb.style.display='none';
  const aw=document.getElementById('addWidgetBtn');if(aw)aw.style.display='none';
  toggleEditPrefs(false);
  closeCatalog();
}
function toggleEditPrefs(on){
  const p=Object.assign({},window._mhPrefs||{});p.editMode=on;window._mhPrefs=p;
  const row=document.getElementById('pref-editToggle');if(row)row.classList.toggle('active',on);
  const label=document.getElementById('editToggleLabel');if(label)label.textContent=on?'Editing mode on':'Editing mode off';
}
function cancelEdit(){
  // Revert to last saved layout then leave edit mode.
  let base={};try{base=JSON.parse(window._editBaseline||'{}');}catch(e){}
  const p=Object.assign({},window._mhPrefs||{});if(base&&Object.keys(base).length){p.layout=p.layout||{};if(Object.keys(base).length&&Object.values(base).some(v=>v.x!=null||v.hidden))p.layout[TAB]=base;else delete p.layout[TAB];}
  window._mhPrefs=p;
  if(Object.keys(base).length&&Object.values(base).some(v=>v.x!=null||v.hidden))applySavedLayout(base);
  exitEdit();window._draftLayout=null;run();
}
function attachFreeHandles(){
  detachFreeHandles();
  freeWidgets().forEach(({w,root})=>{
    const id=widgetIdOf(w);const lay=layoutForTab();const st=lay[id]=lay[id]||{};
    applyWidgetStyle(w,root,st);
    // Whole card drags (mouse + touch); preventDefault stops text selection.
    w.addEventListener('pointerdown',e=>{
      if(document.body.classList.contains('free-edit')&&!e.target.closest('.free-resize')&&!e.target.closest('.free-hide')&&e.button!==2){
        e.preventDefault();startFreeDrag(e,w,root);
      }
    });
    w.style.userSelect=document.body.classList.contains('free-edit')?'none':'';
    if(!w.querySelector('.free-grab')){
      const bar=document.createElement('div');bar.className='free-grab';
      bar.innerHTML='<span class="grab-label">'+escHtml(w.dataset.widgetTitle||widgetIdOf(w))+'</span>';
      bar.addEventListener('pointerdown',e=>{if(e.button===2)return;e.preventDefault();e.stopPropagation();startFreeDrag(e,w,root);});
      w.appendChild(bar);
    }
    if(!w.querySelector('.free-resize')){
      const grip=document.createElement('div');grip.className='free-resize';
      grip.addEventListener('pointerdown',e=>{e.preventDefault();e.stopPropagation();startFreeResize(e,w,root);});
      w.appendChild(grip);
    }
    if(!w.querySelector('.free-hide')){
      const hide=document.createElement('button');hide.type='button';hide.className='free-hide';hide.title='Hide widget';hide.textContent='\u2715';
      hide.addEventListener('click',e=>{e.stopPropagation();hideWidget(w);});
      w.appendChild(hide);
    }
  });
  window._draftLayout=layoutForTab();
}
function detachFreeHandles(){
  freeWidgets().forEach(({w})=>{
    w.removeEventListener('pointerdown',()=>{});
    w.style.userSelect='';
    const g=w.querySelector('.free-grab');if(g)g.remove();
    const r=w.querySelector('.free-resize');if(r)r.remove();
    const h=w.querySelector('.free-hide');if(h)h.remove();
    w.classList.remove('free-hidden');
  });
  freeWidgets().forEach(({w})=>{w.style.position='';w.style.left='';w.style.top='';w.style.width='';w.style.height='';});
}
function applyWidgetStyle(w,root,st){
  if(!root)return;
  if(st.hidden){w.classList.add('free-hidden');return;}w.classList.remove('free-hidden');
  w.style.position='absolute';
  w.style.left=(st.x==null?0:st.x)+'px';
  w.style.top=(st.y==null?0:st.y)+'px';
  w.style.width=Math.max(180,st.w||300)+'px';
  if(st.h)w.style.height=Math.max(80,st.h)+'px';
}
function hideWidget(w){
  const lay=layoutForTab();const id=widgetIdOf(w);lay[id]=lay[id]||{};
  lay[id].hidden=true;w.classList.add('free-hidden');
  window._draftLayout=lay;
}
function applySavedFreePositions(){
  const ws=layoutForTab()||{};
  freeWidgets().forEach(({w,root})=>{const st=ws[widgetIdOf(w)];if(st)applyWidgetStyle(w,root,st);});
}
function applySavedLayout(lay){
  const ws=(lay&&lay.widgets)?lay.widgets:(lay||{});
  freeWidgets().forEach(({w,root})=>{const st=ws[widgetIdOf(w)];if(st)applyWidgetStyle(w,root,st);});
}
function applySavedLayoutToView(){
  // Reapply the saved free layout for the current tab when not editing, so
  // arrangements survive refreshes and tab switches.
  if(EDITING)return;
  const ws=layoutForTab()||{};
  if(!Object.keys(ws).length)return;
  freeRoots().forEach(r=>{if(r)r.classList.add('layout-reapplied');});
  freeWidgets().forEach(({w,root})=>{
    const st=ws[widgetIdOf(w)];
    if(st)applyWidgetStyle(w,root,st);
  });
}
function startFreeDrag(e,w,root){
  if(!EDITING)return;
  e.preventDefault();
  const lay=layoutForTab();const id=widgetIdOf(w);
  const wb=w.getBoundingClientRect();const r=root.getBoundingClientRect();
  const ox=e.clientX-wb.left,oy=e.clientY-wb.top;
  w.classList.add('free-active');
  const move=ev=>{
    let x=ev.clientX-r.left-ox,y=ev.clientY-r.top-oy;
    x=Math.max(0,Math.min(x,r.width-80));y=Math.max(0,Math.min(y,r.height-40));
    w.style.left=x+'px';w.style.top=y+'px';
    const cur=lay[id]=lay[id]||{};
    cur.x=Math.round(x);cur.y=Math.round(y);cur.w=parseInt((w.style.width||'').replace('px',''))||Math.round(wb.width);cur.h=parseInt((w.style.height||'').replace('px',''))||Math.round(wb.height);
    window._draftLayout=lay;
  };
  const up=ev=>{window.removeEventListener('pointermove',move);window.removeEventListener('pointerup',up);window.removeEventListener('pointercancel',up);w.classList.remove('free-active');};
  window.addEventListener('pointermove',move);window.addEventListener('pointerup',up);window.addEventListener('pointercancel',up);
}
function startFreeResize(e,w,root){
  if(!EDITING)return;
  e.preventDefault();
  const lay=layoutForTab();const id=widgetIdOf(w);
  const wb=w.getBoundingClientRect();const r=root.getBoundingClientRect();
  const ox=wb.right-e.clientX,oy=wb.bottom-e.clientY;
  const move=ev=>{
    let wpx=Math.max(180,ev.clientX-r.left+ox),hpx=Math.max(80,ev.clientY-r.top+oy);
    wpx=Math.min(wpx,r.width - (parseInt((w.style.left||'0').replace('px',''))||0));
    w.style.width=Math.round(wpx)+'px';w.style.height=Math.round(hpx)+'px';
    const cur=lay[id]=lay[id]||{};
    cur.w=Math.round(wpx);cur.h=Math.round(hpx);
    window._draftLayout=lay;
  };
  const up=ev=>{window.removeEventListener('pointermove',move);window.removeEventListener('pointerup',up);window.removeEventListener('pointercancel',up);};
  window.addEventListener('pointermove',move);window.addEventListener('pointerup',up);window.addEventListener('pointercancel',up);
}
function forgetDraft(){window._draftLayout=null;}
function collectLayoutFromDom(){
  const lay=window._draftLayout||layoutForTab();
  freeWidgets().forEach(({w,root})=>{
    const id=widgetIdOf(w);const st=lay[id]=lay[id]||{};
    if(w.classList.contains('free-hidden'))st.hidden=true;else delete st.hidden;
    const l=parseInt((w.style.left||'').replace('px',''));if(!isNaN(l)&&l>=0)st.x=l;
    const t=parseInt((w.style.top||'').replace('px',''));if(!isNaN(t)&&t>=0)st.y=t;
    const wt=parseInt((w.style.width||'').replace('px',''));if(!isNaN(wt)&&wt>0)st.w=wt;
    const ht=parseInt((w.style.height||'').replace('px',''));if(!isNaN(ht)&&ht>0)st.h=ht;
  });
  return lay;
}
async function saveLayout(){
  if(!EDITING)return;
  const lay=collectLayoutFromDom();
  detachFreeHandles();
  document.body.classList.remove('free-edit');
  exitEdit();
  await persistLayout(lay);
  window._draftLayout=null;
  run();
}

// ---- Prefs management ----
window._mhPrefs = {};
const ACCENT_SWATCHES = ['#5c5bd6','#2d9eb3','#e45567','#dc913a','#18a776','#3a86ff'];
async function loadPrefs(){
  let r;try{r=await fetch('/api/ui/prefs');const p=await r.json();window._mhPrefs=p;}catch(e){window._mhPrefs=window._mhPrefs||{}}
  applyPrefs(window._mhPrefs);
  return window._mhPrefs;
}
function applyPrefs(p){
  if(!p||!Object.keys(p).length)return;
  if(p.theme&&p.theme!=='system'){
    const dark=p.theme==='dark';
    document.documentElement.dataset.theme=dark?'dark':'light';
    const tb=document.getElementById('themeToggle');if(tb){tb.textContent=dark?'Light mode':'Dark mode';tb.setAttribute('aria-pressed',String(dark));}
    storage.set('mh-theme',p.theme);
  }
  if(p.accent){
    document.documentElement.style.setProperty('--lav',p.accent);
    document.documentElement.style.setProperty('--accent',p.accent);
  }
  if(p.density==='compact')document.documentElement.style.setProperty('--spacing','8px');
  else document.documentElement.style.removeProperty('--spacing');
  if(p.fontScale)document.documentElement.style.fontSize=(14*p.fontScale)+'px';
  const pet=document.getElementById('xoraPet');
  const bub=document.getElementById('petBubble');
  if(p.chatEnabled===false){if(pet)pet.style.display='none';if(bub)bub.style.display='none';}
  else {if(pet)pet.style.display='';}
  if(p.layout?.[TAB])initTiles();
  if(p.hideScrollbars===true)document.documentElement.classList.add('hide-scrollbars');
  else document.documentElement.classList.remove('hide-scrollbars');
  petAttachWidgetTips();
}
function showToast(msg){
  let t=document.getElementById('toast');if(!t){t=document.createElement('div');t.id='toast';t.style.cssText='position:fixed;bottom:80px;left:50%;transform:translateX(-50%);background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:10px 18px;font-size:13px;color:var(--text);z-index:200;box-shadow:0 4px 20px rgba(0,0,0,.15);transition:opacity .3s';document.body.appendChild(t);}
  t.textContent=msg;t.style.opacity='1';clearTimeout(t._hide);t._hide=setTimeout(()=>{t.style.opacity='0'},2500);
}
// ---- Settings modal ----
function openSettings(){document.getElementById('settingsModal').classList.add('open');rebuildSettings();}
function closeSettings(){document.getElementById('settingsModal').classList.remove('open');}
document.getElementById('settingsClose').addEventListener('click',closeSettings);
document.getElementById('settingsModal').addEventListener('click',e=>{if(e.target===e.currentTarget)closeSettings();});
function rebuildSettings(){
  const p=window._mhPrefs||{};
  const st=document.getElementById('pref-theme');if(st)st.value=p.theme||'system';
  const sw=document.getElementById('pref-accent');if(sw){
    sw.innerHTML=ACCENT_SWATCHES.map(c=>`<span class="color-swatch ${(p.accent||'#5c5bd6')===c?'active':''}" data-color="${c}" style="background:${c}"></span>`).join('');
    sw.querySelectorAll('.color-swatch').forEach(el=>el.addEventListener('click',()=>updatePref('accent',el.dataset.color)));
  }
  const de=document.getElementById('pref-density');if(de)de.value=p.density||'comfortable';
  const fs=p.fontScale||1;const fsR=document.getElementById('pref-fontScale');if(fsR)fsR.value=Math.round(fs*100);
  const fsV=document.getElementById('pref-fontScale-val');if(fsV)fsV.textContent=Math.round(fs*100)+'%';
  const pr=document.getElementById('pref-refresh');if(pr)pr.value=String(p.refreshSeconds||30);
  const ct=document.getElementById('pref-chatToggle');if(ct)ct.classList.toggle('active',p.chatEnabled!==false);
  const hs=document.getElementById('pref-hideScrollbars');if(hs)hs.classList.toggle('active',p.hideScrollbars===true);
}
function updatePref(key,value){
  const p=Object.assign({},window._mhPrefs||{});
  p[key]=value;savePrefs(p);
}
async function savePrefs(p){
  try{const r=await fetch('/api/ui/prefs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(p)});const d=await r.json();if(d.ok){window._mhPrefs=d.prefs;applyPrefs(d.prefs);showToast('Settings saved');}else showToast('Error: '+d.error);}catch(e){showToast('Save failed');}
}
document.getElementById('pref-theme')?.addEventListener('change',function(){updatePref('theme',this.value);});
document.getElementById('pref-density')?.addEventListener('change',function(){updatePref('density',this.value);});
document.getElementById('pref-fontScale')?.addEventListener('input',function(){const v=parseInt(this.value)/100;document.getElementById('pref-fontScale-val').textContent=this.value+'%';updatePref('fontScale',v);});
document.getElementById('pref-refresh')?.addEventListener('change',function(){
  const v=parseInt(this.value);updatePref('refreshSeconds',v);
  if(window._refreshInt){clearInterval(window._refreshInt);}
  if(v>0&&v<3600)window._refreshInt=setInterval(()=>run(),v*1000);
});
document.getElementById('pref-chatToggle')?.addEventListener('click',function(){const on=!this.classList.contains('active');this.classList.toggle('active',on);updatePref('chatEnabled',on);});
document.getElementById('pref-hideScrollbars')?.addEventListener('click',function(){const on=!this.classList.contains('active');this.classList.toggle('active',on);updatePref('hideScrollbars',on);});
// ---- Chat pet ----
const PET_TIPS={
  equity:'Equity is tracked against starting funds. Paper and live are kept separate here.',
  'total equity':'Total equity is marked, not realised. Check the profit widget before trusting it.',
  'open positions':'Open positions are live risk right now. Size them against the protected floor.',
  profit:'Profit here is realised P&L. Small sample sizes can flatter a wallet.',
  'what to do':'Start here. This widget already ranks the most useful next action.',
  risk:'Risk parameters come from the enforced policy, not from the model.',
  'xora wallet':'Xora wallet splits live notional from paper notional. Never read them as one number.',
  gate:'The live gate blocks new live risk until every evidence check passes.',
  positions:'Position rows show what is open now, with entry and mark separated.',
  'exit reasons':'Exit reasons show why positions closed. Stop loss should never be conditional.',
  'trader performance':'Compare traders on realised P&L and drawdown, not on equity alone.',
  'market':'Market data is mainnet quote data. Paper strategy evidence is separate.'
};
function petSay(text,ms){
  const b=document.getElementById('petBubble');if(!b||!text)return;
  b.textContent=String(text).replace(/—/g,'-').slice(0,320);
  b.style.display='block';
  clearTimeout(b._hide);b._hide=setTimeout(()=>{if(b.textContent)b.style.display='none'},ms||9000);
}
function petTipFor(card){
  if(!card)return null;
  const id=(card.dataset.widgetId||'').toLowerCase();
  const head=(card.querySelector('h3')?.textContent||card.querySelector('.lbl')?.textContent||'').trim();
  const label=head?head.toLowerCase():'';
  const hit=Object.keys(PET_TIPS).find(k=>id.includes(k)||label.includes(k));
  if(hit)return PET_TIPS[hit];
  if(!head)return null;
  const body=(card.innerText||'').replace(/\s+/g,' ').trim();
  return 'Tip: "'+head+'" shows '+body.slice(0,150)+'. Ask me about it if anything looks unclear.';
}
function petAttachWidgetTips(){
  document.querySelectorAll('#kpis .kpi, #tab-panels .card, #coin-wrap .card, .overview-grid .card').forEach(card=>{
    if(card.dataset.petBound==='1')return;card.dataset.petBound='1';
    card.addEventListener('mouseenter',()=>{const tip=petTipFor(card);if(tip&&!document.getElementById('chatPanel').classList.contains('open'))petSay(tip);});
  });
}
// ---- Pet drag, toss, and physics ----
let petDrag=null,petVel={vx:0,vy:0},petPhysicsId=null,petTrail=[];
const PET_W=70,PET_H=76,FRICTION=0.88,BOUNCE=0.62,MIN_SPEED=0.3;
function petMoveBubble(){
  const pet=document.getElementById('xoraPet'),b=document.getElementById('petBubble');
  if(!pet||!b)return;
  const r=pet.getBoundingClientRect(),vh=window.innerHeight||document.documentElement.clientHeight;
  const bottomPos=vh - r.top + 3;
  b.style.bottom=Math.max(8,bottomPos)+'px';
  b.style.top='auto';
  b.style.right='auto';b.style.left=Math.max(12,Math.min(r.left+8,window.innerWidth-Math.min(292,window.innerWidth-24)))+'px';
}
function petGrab(e){
  const pet=document.getElementById('xoraPet');if(!pet)return;
  const r=pet.getBoundingClientRect();
  petDrag={ox:e.clientX-r.left,oy:e.clientY-r.top};
  pet.style.right='auto';pet.style.bottom='auto';pet.style.left=r.left+'px';pet.style.top=r.top+'px';
  pet.classList.add('pet-dragging');
  petTrail=[{x:e.clientX,y:e.clientY,t:performance.now()}];
  if(petPhysicsId){cancelAnimationFrame(petPhysicsId);petPhysicsId=null;}
  petStopIdleRoam();
  clearTimeout(pet._hide);
}
function petDragMove(e){
  if(!petDrag)return;
  const pet=document.getElementById('xoraPet');if(!pet)return;
  const vw=window.innerWidth,vh=window.innerHeight;
  const x=Math.max(0,Math.min(e.clientX-petDrag.ox,vw-PET_W));
  const y=Math.max(0,Math.min(e.clientY-petDrag.oy,vh-PET_H));
  pet.style.left=x+'px';pet.style.top=y+'px';
  petTrail.push({x:e.clientX,y:e.clientY,t:performance.now()});
  if(petTrail.length>8)petTrail.shift();
  petMoveBubble();
}
function petDrop(e){
  if(!petDrag)return;
  const pet=document.getElementById('xoraPet');if(!pet)return;
  pet.classList.remove('pet-dragging');
  if(petTrail.length>=2){
    const last=petTrail[petTrail.length-1],first=petTrail[0];
    const dt=Math.max(1,last.t-first.t);
    petVel.vx=((last.x-first.x)/dt)*16;
    petVel.vy=((last.y-first.y)/dt)*16;
    if(Math.sqrt(petVel.vx*petVel.vx+petVel.vy*petVel.vy)>0.8){petToss(pet);petDrag=null;return;}
  }
  petDrag=null;
}
function petToss(pet){
  function tick(){
    if(!pet){petPhysicsId=null;return;}
    let x=parseFloat(pet.style.left)||0,y=parseFloat(pet.style.top)||0;
    const vw=window.innerWidth,vh=window.innerHeight;
    petVel.vx*=FRICTION;petVel.vy*=FRICTION;
    x+=petVel.vx;y+=petVel.vy;
    // Bounce off walls
    if(x<0){x=0;petVel.vx=-petVel.vx*BOUNCE;}
    if(x>vw-PET_W){x=vw-PET_W;petVel.vx=-petVel.vx*BOUNCE;}
    if(y<0){y=0;petVel.vy=-petVel.vy*BOUNCE;}
    if(y>vh-PET_H){y=vh-PET_H;petVel.vy=-petVel.vy*BOUNCE;}
    pet.style.left=x+'px';pet.style.top=y+'px';
    petMoveBubble();
    if(Math.sqrt(petVel.vx*petVel.vx+petVel.vy*petVel.vy)>MIN_SPEED){petPhysicsId=requestAnimationFrame(tick);}
    else {petPhysicsId=null;petStartIdleRoam();}
  }
  if(petPhysicsId){cancelAnimationFrame(petPhysicsId);}
  petPhysicsId=requestAnimationFrame(tick);
}
function petInitPosition(){
  const pet=document.getElementById('xoraPet');if(!pet)return;
  if(!pet.style.left||pet.style.left==='auto'){
    const r=pet.getBoundingClientRect();
    pet.style.right='auto';pet.style.bottom='auto';pet.style.left=r.left+'px';pet.style.top=r.top+'px';
  }
}
// ---- Idle roam ----
let petRoamIdleId=null,roamAngle=0,roamTimer=0;
const ROAM_SPEED=0.2;
function petStopIdleRoam(){
  if(petRoamIdleId){cancelAnimationFrame(petRoamIdleId);petRoamIdleId=null;}
  roamTimer=0;
}
function petStartIdleRoam(){
  if(petRoamIdleId||petDrag||petPhysicsId)return;
  roamAngle=Math.random()*Math.PI*2;
  roamTimer=Math.floor(120+Math.random()*120);
  (function tick(){
    if(petDrag||petPhysicsId){petRoamIdleId=null;return;}
    const pet=document.getElementById('xoraPet');if(!pet){petRoamIdleId=null;return;}
    let x=parseFloat(pet.style.left)||0,y=parseFloat(pet.style.top)||0;
    const vw=window.innerWidth,vh=window.innerHeight,mar=60;
    // Gentle wall steering
    if(x<mar)roamAngle+=0.03;if(x>vw-PET_W-mar)roamAngle-=0.03;
    if(y<mar)roamAngle-=0.03;if(y>vh-PET_H-mar)roamAngle+=0.03;
    if(x<4){x=4;roamAngle=Math.atan2(Math.sin(roamAngle),Math.abs(Math.cos(roamAngle)));}
    if(x>vw-PET_W-4){x=vw-PET_W-4;roamAngle=Math.atan2(Math.sin(roamAngle),-Math.abs(Math.cos(roamAngle)));}
    if(y<4){y=4;roamAngle=Math.atan2(Math.abs(Math.sin(roamAngle)),Math.cos(roamAngle));}
    if(y>vh-PET_H-4){y=vh-PET_H-4;roamAngle=Math.atan2(-Math.abs(Math.sin(roamAngle)),Math.cos(roamAngle));}
    x+=Math.cos(roamAngle)*ROAM_SPEED;y+=Math.sin(roamAngle)*ROAM_SPEED;
    pet.style.left=x+'px';pet.style.top=y+'px';
    petMoveBubble();
    roamTimer--;if(roamTimer<=0){roamAngle+=(Math.random()-0.5)*0.8;roamTimer=Math.floor(180+Math.random()*240);}
    petRoamIdleId=requestAnimationFrame(tick);
  })();
}
document.getElementById('xoraPet')?.addEventListener('mousedown',petGrab);
document.addEventListener('mousemove',petDragMove);
document.addEventListener('mouseup',petDrop);
document.getElementById('xoraPet')?.addEventListener('dblclick',function(){
  const panel=document.getElementById('chatPanel');
  panel.classList.toggle('open');
  if(panel.classList.contains('open')){
    panel.dataset.loaded=panel.dataset.loaded||'0';
    if(panel.dataset.loaded!=='1'){loadChatHistory();panel.dataset.loaded='1';}
    petSay('Ask me anything about Xora-Survival or MultiHedge. I answer from live system state only.',6000);
  }
});
document.getElementById('chatClose')?.addEventListener('click',()=>document.getElementById('chatPanel').classList.remove('open'));
async function loadChatHistory(){
  try{const r=await fetch('/api/chat/history?limit=20');const msgs=await r.json();const el=document.getElementById('chatMsgs');el.innerHTML=msgs.map(m=>`<div class="chat-msg ${m.role}"><span>${escHtml(m.content)}</span><div class="ts">${new Date(m.ts*1000).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})}</div></div>`).join('');}catch(e){}
}
function escHtml(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML;}
function addChatMsg(role,content,ts){
  const el=document.getElementById('chatMsgs');const plc=el.querySelector('.chat-placeholder');if(plc)plc.remove();
  el.innerHTML+=`<div class="chat-msg ${role}"><span>${escHtml(content)}</span><div class="ts">${ts||new Date().toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})}</div></div>`;
  el.scrollTop=el.scrollHeight;
}
document.getElementById('chatSend')?.addEventListener('click',sendChatMessage);
document.getElementById('chatInput')?.addEventListener('keydown',e=>{if(e.key==='Enter')sendChatMessage();});
const _chatHistory=[];
async function sendChatMessage(){
  const input=document.getElementById('chatInput');if(!input)return;
  const msg=input.value.trim();if(!msg)return;
  input.value='';addChatMsg('user',msg);
  const msgs=document.getElementById('chatMsgs');
  msgs.innerHTML+='<div class="chat-loading">Thinking...</div>';
  const btn=document.getElementById('chatSend');if(btn)btn.disabled=true;
  try{
    const r=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:msg,history:_chatHistory})});
    const d=await r.json();
    const ld=msgs.querySelector('.chat-loading');if(ld)ld.remove();
    if(d.ok){addChatMsg('assistant',d.reply);_chatHistory.push({role:'user',content:msg},{role:'assistant',content:d.reply});document.getElementById('chatFiling').style.display='flex';petSay(d.reply,12000);}
    else addChatMsg('err',d.error||'Service unavailable');
  }catch(e){const ld=msgs.querySelector('.chat-loading');if(ld)ld.remove();addChatMsg('err','Network error');}
  if(btn)btn.disabled=false;
}
document.getElementById('fileRequestBtn')?.addEventListener('click',async function(){
  if(!_chatHistory.length)return;const last=_chatHistory[_chatHistory.length-1];if(!last)return;
  const title='From chat: '+last.content.slice(0,60);const detail=last.content.slice(0,2000);
  try{const r=await fetch('/api/requests',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title,detail,source:'chat'})});const d=await r.json();if(d.ok)showToast('Request filed (ID '+d.id+')');else showToast('Error');}catch(e){showToast('Network error');}
});
document.getElementById('openRequestsBtn')?.addEventListener('click',async function(){
  try{const r=await fetch('/api/requests?limit=10');const reqs=await r.json();document.getElementById('pendingReqCount').textContent=reqs.filter(x=>x.status==='pending').length;showToast('Requests: '+reqs.length);}catch(e){showToast('Could not load');}
});

function fmtUsd(n){return (n==null?'-':(n>=0?'+':'')+(+n).toFixed(2));}
async function loadCouncilReport(){
  const card=document.getElementById('councilSummary');if(!card){return;}
  const fallback=(msg)=>{card.innerHTML='<div class="mini-note">'+escHtml(msg)+'</div>'
    +'<div class="xora-sum-col"><div class="xora-metric"><span class="xora-metric-label">Status</span><span class="xora-metric-val">offline</span></div></div>';};
  try{
    const r=await fetch('/api/xora/summary');const d=await r.json();
    if(!d.ok){fallback('Xora summary unavailable.');return;}
    const w=d.wallet||{},e=d.edge||{},pos=d.positions||{},not=d.notional||{},risk=d.risk||{};
    const council=d.council||{};
    const gateOk=((e.paper_n||0)>=50&&(e.paper_win_rate||0)>=0.667&&(e.paper_net_usd||0)>0);
    const hist=d.history||[];
    const reasons=d.top_exit_reasons||[];
    // Stats columns
    let html='<div class="xora-sum-grid">';
    const col=(label,val,cls)=>'<div class="xora-sum-col"><span class="xora-metric-label">'+label+'</span><span class="xora-metric-val '+ (cls||'') +'">'+val+'</span></div>';
    html+=col('Equity','$'+(w.paper_equity_usd==null?'-':(+w.paper_equity_usd).toFixed(2)));
    html+=col('Start','$'+((e.starting_equity_usd||0)).toFixed(2));
    html+=col('Net P/L','$'+(e.paper_net_usd>=0?'+':'')+(+e.paper_net_usd).toFixed(2), e.paper_net_usd>=0?'pos':'neg');
    html+=col('Win rate',((e.paper_win_rate||0)*100).toFixed(1)+'% · '+((e.paper_wins||0))+'W', (e.paper_win_rate||0)>=0.5?'pos':'neg');
    html+=col('Closed',((e.paper_n||0)+' / 50'));
    html+=col('Live fills',((e.live_fills||0))+'B / '+((e.live_closes||0))+'S');
    html+=col('Positions','<span class="pos">'+((pos.live||0))+'L</span> · '+((pos.paper||0))+'P');
    html+=col('Notional','$'+(+(not.paper_usd||0)).toFixed(2)+' paper');
    html+=col('Gate',gateOk?'<span class="pilltag ok">READY</span>':'<span class="pilltag no">'+String(e.paper_n||0)+'/50 · '+(((e.paper_win_rate||0)*100)).toFixed(0)+'%</span>');
    html+='</div>';
    // Plans / scope / policy
    html+='<div class="xora-sum-section"><span class="xora-section-label">Scope &amp; policy</span><div style="font-size:11px;color:var(--text-dim);line-height:1.5">'
      +'<div>· Incubator allowed: <b>'+(risk.promotion_enabled?'YES (approved promotion)':'NO (locked)')+'</b></div>'
      +'<div>· USDC reserve only · SOL fees · mainnet-beta</div>'
      +'<div>· Paper: <b>'+((pos.paper||0))+'</b> open · Live: <b>'+((pos.live||0))+'</b></div>'
      +'<div>· Exit reasons: '+(reasons.map(x=>escHtml(x[0])+'&times;'+x[1]).join(' · ')||'none')+'</div>'
      +'</div></div>';
    // History (last 8)
    html+='<div class="xora-sum-section"><span class="xora-section-label">Recent history</span><div class="xora-hist">'
      +(hist.slice(0,8).map(h=>'<div class="xora-hist-row"><span class="'+(h.kind==='live'?'pos':'')+'">'+escHtml(h.kind.toUpperCase())+'</span> <b>'+escHtml(h.coin)+'</b> '+escHtml(h.side)+' <span class="'+(h.pnl_usd==null?'':h.pnl_usd>=0?'pos':'neg')+'">'+fmtUsd(h.pnl_usd)+'</span> <span style="color:var(--text-faint)">'+escHtml(h.reason||'')+'</span></div>').join('')||'<div class="mini-note">No history yet</div>')
      +'</div></div>';
    // Council verdict
    html+='<div class="xora-sum-section"><span class="xora-section-label">Council verdict</span>';
    if(council.available){
      html+='<div class="council-summary-list">'+(council.verdict||[]).map(s=>'<div class="council-s-line">'+escHtml(s)+'</div>').join('')+'</div>';
      html+='<button type="button" class="toolbar-btn council-open" data-name="'+escHtml(council.filename||'')+'" style="margin-top:8px;width:100%;min-height:36px;font-weight:700">Open full report</button>';
    }else{
      html+='<div class="mini-note">No council report yet.</div>';
      html+='<button type="button" class="toolbar-btn council-open" data-name="" style="margin-top:8px;width:100%;min-height:32px" disabled>Open full report</button>';
    }
    html+='</div>';
    card.innerHTML=html;
    const btn=card.querySelector('.council-open');
    if(btn)btn.addEventListener('click',()=>openCouncilReport(btn.dataset.name));
  }catch(e){fallback('Xora summary unavailable.');}
}

document.getElementById('councilModalClose')?.addEventListener('click',()=>document.getElementById('councilModal').classList.remove('open'));
document.getElementById('councilModal')?.addEventListener('click',e=>{if(e.target===e.currentTarget)document.getElementById('councilModal').classList.remove('open');});
async function openCouncilReport(name){
  if(!name)return;
  const modal=document.getElementById('councilModal');if(!modal)return;
  const body=document.getElementById('councilModalBody');
  body.innerHTML='<div class="loading">Loading...</div>';
  modal.classList.add('open');
  try{
    const r=await fetch('/api/council/report/'+encodeURIComponent(name));const d=await r.json();
    if(!d.ok){body.innerHTML='<div class="load-error">Could not load report</div>';return;}
    // The cron artifact leads with a huge prompt/skill preamble; the actual
    // verdict lives under a "## Response" heading. Show the verdict by default
    // and keep the preamble behind a toggle so the report is readable.
    const txt=d.text||'';
    const m=txt.match(/^##[ \t]*(Response|Output|Final|Result)(?=$|[ \t:])/mi);
    const head=txt.slice(0,m?m.index:0);
    const core=m?txt.slice(m.index):txt;
    let html='<div style="display:flex;gap:8px;align-items:center;margin-bottom:8px;flex-wrap:wrap">';
    html+='<button type="button" id="councilTogglePre" class="toolbar-btn" style="min-height:30px;padding:4px 10px">'+(head?'Show prompt preamble':'Hide preamble')+'</button>';
    html+='<span style="font-size:10.5px;color:var(--text-faint)">'+escHtml(txt.length.toLocaleString())+' chars · verdict shown</span></div>';
    if(head){html+='<pre id="councilPre" style="display:none;white-space:pre-wrap;font-size:11px;font-family:monospace;max-height:40vh;overflow:auto;border:1px solid var(--border);border-radius:8px;padding:10px;margin-bottom:10px">'+escHtml(head)+'</pre>';}
    html+='<pre style="white-space:pre-wrap;font-size:11.5px;font-family:monospace;max-height:60vh;overflow:auto">'+escHtml(core)+'</pre>';
    body.innerHTML=html;
    const tp=document.getElementById('councilTogglePre');
    if(tp)tp.addEventListener('click',()=>{const pre=document.getElementById('councilPre');if(!pre)return;
      const show=pre.style.display==='none';pre.style.display=show?'block':'none';
      tp.textContent=show?'Hide prompt preamble':'Show prompt preamble';});
  }catch(e){body.innerHTML='<div class="load-error">Network error</div>';}
}

// ---- Init ----
(async function(){
  const p=await loadPrefs();
  await run();
  const sec=p?.refreshSeconds||30;
  if(sec>0&&sec<3600)window._refreshInt=setInterval(()=>run(),sec*1000);
})();
petInitPosition();setTimeout(()=>petStartIdleRoam(),300);
</script></body></html>"""