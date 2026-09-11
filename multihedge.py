"""
MultiHedge — multi-coin Solana paper-trading engine (separate app).

run_tick() evaluates EVERY configured coin once: prices the universe, runs each
coin's active strategy setup, opens/holds/closes paper positions. It never
writes on-chain; live_bridge is the only module that can, and it is gated.

CLI:
  python multihedge.py tick        # run one evaluation pass across all coins
  python multihedge.py status      # paper + live-gate status
  python multihedge.py history     # recent paper trades
  python multihedge.py loom        # continuous loop (every trade_cadence_s)
"""

import json
import sys
import time
from pathlib import Path

import yaml

import paper
import pricefeed
import strategy as strat
import agent_architecture as agents

CFG_PATH = Path(__file__).parent / "config.yaml"


def load_config():
    return yaml.safe_load(CFG_PATH.read_text(encoding="utf-8"))


def seed_accounts(cfg):
    p = cfg.get("paper", {})
    start = p.get("starting_equity_usd", 24.0)
    # The scalper (fast engine) owns its own independent wallet at the start amount.
    paper.ensure_account(paper.TRADER_SCALPER, start)


def _ensure_pxhist(reconnect=True):
    con = paper._connect()
    con.execute("CREATE TABLE IF NOT EXISTS mh_pxhist ("
                "coin TEXT, ts REAL, px REAL)")
    con.commit()
    if reconnect:
        con.close()
    return con  # reuse handle


def run_tick(cfg):
    """One evaluation pass: close expiring positions, open new signals, price the universe."""
    p = cfg.get("paper", {})
    cadence = p.get("trade_cadence_s", 30)
    pos_frac = p.get("position_fraction", 0.20)
    min_usd = p.get("min_trade_value_usd", 5.0)
    max_open = p.get("max_open_per_coin", 1)

    results = []
    _ensure_pxhist()
    for coin_cfg in cfg.get("coins", []):
        sym = coin_cfg["symbol"]
        mint = coin_cfg["mint"]
        # 1. live price
        px = pricefeed.live_price(mint, sym)
        if px is None or px <= 0:
            results.append({"coin": sym, "action": "no_price"})
            continue

        # 2. Advisory work: run agent pipeline after price fetch, before open/close logic
        # Fetch history for agent's price context (ascending order: oldest to most recent)
        con = paper._connect()
        rows = con.execute("SELECT px FROM mh_pxhist WHERE coin=? ORDER BY ts ASC LIMIT 40", (sym,)).fetchall()
        con.close()
        closes = [r["px"] for r in rows]   # oldest to most recent

        # Calculate price context from recent closes
        if len(closes) >= 5:
            recent_avg = sum(closes[-5:]) / 5   # average of the 5 most recent
        else:
            recent_avg = px   # if less than 5, use px to avoid division by zero
        if recent_avg != 0:
            deviation_pct = ((px - recent_avg) / recent_avg) * 100.0
        else:
            deviation_pct = 0.0
        if len(closes) >= 5:
            trend = "up" if px > closes[-5] else "down"
        else:
            trend = "down"   # fallback when not enough history

        price_ctx = {"deviation_pct": deviation_pct, "trend": trend}

        # News context from mh_news_bias table (same as before)
        news_ctx = {"bias_label": "neutral", "bias_score": 0.0}
        con = paper._connect()
        try:
            row = con.execute("SELECT direction,confidence,ts FROM mh_news_bias WHERE symbol=? AND COALESCE(provider,'') NOT IN ('whale_tracker','memecoin_tracker') ORDER BY ts DESC LIMIT 1", (sym,)).fetchone()
            if row and time.time()-row["ts"] <= 3600:
                news_ctx = {"bias_label": row["direction"], "bias_score": row["confidence"] * {"UP":1, "DOWN":-1}.get(row["direction"], 0)}
        except Exception:
            pass
        finally:
            con.close()

        # Agent inference is asynchronous and advisory. It is submitted only
        # after deterministic exit handling so it can never delay a safe exit.

        # 3. Insert current price into history for scalper signals (existing logic)
        con = paper._connect()
        con.execute("INSERT INTO mh_pxhist(coin,ts,px) VALUES(?,?,?)", (sym, time.time(), px))
        con.commit()
        rows = con.execute("SELECT px FROM mh_pxhist WHERE coin=? ORDER BY ts DESC LIMIT 40", (sym,)).fetchall()
        closes_scalper = [r["px"] for r in reversed(rows)]   # most recent to oldest
        con.close()

        # 4. close checks on any open position
        poses = paper.open_positions(sym)
        closed_here = 0
        for pos in poses:
            reason = paper.close_checks(pos, px, cfg)
            # persist peak / trail-arm state so trailing stop works across ticks
            side = pos["side"]
            peak = pos.get("peak_px") or pos["entry_px"]
            if (side == "LONG" and px > peak) or (side == "SHORT" and px < peak):
                peak = px
            armed = pos.get("trail_armed")
            if not armed and reason is None:
                pct = (px - pos["entry_px"]) / pos["entry_px"] if side == "LONG" else (pos["entry_px"] - px) / pos["entry_px"]
                if pct >= 0.012:
                    armed = 1
            paper.update_peak(pos["id"], peak, armed or 0)
            if reason:
                r = paper.close_position(pos, px, reason)
                if r is None:
                    continue
                closed_here += 1
                results.append({"coin": sym, "action": "close",
                                "reason": reason, "pct": round(r["pct"], 4)})

        risk_state = {
            "POSITION_FRACTION": cfg.get("reasoner", {}).get("POSITION_FRACTION", 0.5),
            "paused": paper.is_paused(paper.TRADER_SCALPER),
        }
        agents.submit_pipeline(sym, price_ctx, news_ctx, risk_state)

        # 5. open new position if signal calls and we have room
        paper.kill_switch_check(paper.TRADER_SCALPER,
                                cfg.get("paper", {}).get("max_drawdown_kill", paper.KILL_DD))
        if not paper.is_paused(paper.TRADER_SCALPER) \
                and len(paper.open_positions(sym)) < max_open and closed_here == 0:
            configured = cfg.get("strategies")
            enabled = {s["name"] for s in configured if s.get("enabled", True)} if configured is not None else set(strat.SETUP_SIGNALS)
            setup = strat.choose_setup(sym, enabled=enabled)
            if setup:
                setup_signal = strat.SETUP_SIGNALS[setup]
                sig = setup_signal(closes_scalper) if len(closes_scalper) >= 2 else "FLAT"
                if sig in ("LONG", "SHORT"):
                    # Scalper is LONG-only by design (Kelly: no shorts on the scalper).
                    # SHORT signals are observed but never opened, so they can't appear
                    # in history/stats again.
                    sig = "LONG" if sig == "LONG" else None
                if sig == "LONG":
                    qty = paper.size_trade(paper.TRADER_SCALPER, px, pos_frac, min_usd)
                    if qty > 0:
                        paper.open_position(sym, px, qty, sig, setup)
                        results.append({"coin": sym, "action": "open",
                                        "side": sig, "setup": setup,
                                        "qty": round(qty, 6), "px": px})
                    else:
                        results.append({"coin": sym, "action": "skip", "reason": "dust"})
                else:
                    results.append({"coin": sym, "action": "flat", "setup": setup})
    return results
def status(cfg):
    seed_accounts(cfg)
    out = {"paper": {}, "live_gate": {}}
    con = paper._connect()
    out["paper"]["wallets"] = {
        "started": paper.DEFAULT_EQUITY,
        "wallets": {t: paper.equity(t) for t in paper.TRADERS},
    }
    for t in paper.TRADERS:
        out["live_gate"][t] = paper.paper_gate_status(t, cfg)
    # recent trades
    rows = con.execute("SELECT * FROM mh_trades ORDER BY id DESC LIMIT 10").fetchall()
    out["recent"] = [dict(r) for r in rows]
    con.close()
    return out


def history(cfg, n=20):
    con = paper._connect()
    rows = con.execute("SELECT * FROM mh_trades ORDER BY id DESC LIMIT ?", (n,)).fetchall()
    con.close()
    return [dict(r) for r in rows]


def loom(cfg):
    seed_accounts(cfg)
    cadence = cfg.get("paper", {}).get("trade_cadence_s", 30)
    print(f"MultiHedge paper loom running every {cadence}s. Ctrl-C to stop.")
    while True:
        try:
            tick = run_tick(cfg)
            for r in tick:
                print(json.dumps(r, default=str))
            time.sleep(cadence)
        except KeyboardInterrupt:
            print("stopped.")
            break
        except Exception as e:
            print("tick error:", type(e).__name__, str(e)[:140])
            time.sleep(cadence)


if __name__ == "__main__":
    cfg = load_config()
    seed_accounts(cfg)
    mode = sys.argv[1] if len(sys.argv) > 1 else "status"
    if mode == "tick":
        print(json.dumps(run_tick(cfg), indent=2, default=str))
    elif mode == "status":
        print(json.dumps(status(cfg), indent=2, default=str))
    elif mode == "history":
        print(json.dumps(history(cfg), indent=2, default=str))
    elif mode == "loom":
        loom(cfg)
    else:
        print("usage: multihedge.py {tick|status|history|loom}")