#!/usr/bin/env python3
"""Cost sensitivity sweep for the 5-year backtest.

The 40 bps per-side assumption may be the dominant reason every strategy
bleeds equity. This sweep precomputes per-bar signals once (using the real
strategy functions from strategy.py, with the same trend filter toggle the
shadow pipeline uses), then replays the identical next-bar long-only
simulation at several cost levels to find each coin x strategy breakeven.

Read-only, paper-only. Never touches production databases.
"""
from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from strategy import (  # type: ignore
    momentum_breakout_signal,
    mean_reversion_signal,
    rsi_oversold_signal,
    vwap_reversion_signal,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from importlib import import_module
_bt = import_module("5year_backtest") if False else None

# Import klines fetcher from the sibling script by path (module name starts
# with a digit so a plain import is not possible).
import importlib.util
_spec = importlib.util.spec_from_file_location(
    "five_year_backtest", Path(__file__).resolve().parent / "5year_backtest.py"
)
if _spec is None or _spec.loader is None:
    raise RuntimeError("Could not load 5year_backtest module")
_bt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_bt)

STRATEGIES = {
    "momentum_breakout": momentum_breakout_signal,
    "mean_reversion": mean_reversion_signal,
    "rsi_oversold": rsi_oversold_signal,
    "vwap_reversion": vwap_reversion_signal,
}
WARMUP = {"momentum_breakout": 20, "mean_reversion": 10, "rsi_oversold": 3, "vwap_reversion": 15}
COST_LEVELS_BPS = [5.0, 10.0, 20.0, 40.0]
TREND_PERIOD = 200


def _trend_flags(closes: list[float], enabled: bool) -> list[bool]:
    """Per-bar trend_ok flag: price above SMA(TREND_PERIOD) once enough data."""
    if not enabled:
        return [True] * len(closes)
    flags = []
    running = 0.0
    window: list[float] = []
    for i, price in enumerate(closes):
        window.append(price)
        running += price
        if len(window) > TREND_PERIOD:
            running -= window.pop(0)
        ok = True
        if len(window) == TREND_PERIOD:
            sma = running / TREND_PERIOD
            ok = price > sma
        flags.append(ok)
    return flags


def precompute_signals(closes: list[float], name: str, fn, trend_flags: list[bool]) -> list[bool]:
    """One boolean per bar: would this bar produce a LONG signal (trend-allowed)?"""
    warm = WARMUP[name]
    out = []
    for i in range(1, len(closes)):
        window = closes[max(0, i - 30): i + 1]  # signals look back <= 20 bars
        sig = fn(window)
        out.append(sig == "LONG" and trend_flags[i] and i >= WARMUP[name])
    return out


def simulate(closes: list[float], long_signal: list[bool], starting_equity: float,
             position_fraction: float, per_side_bps: float):
    """Next-bar long-only equity simulation, same semantics as backtest()."""
    cost_mult = per_side_bps / 10_000.0
    cash = starting_equity
    qty = 0.0
    entry_price = 0.0
    n_round_trips = 0
    wins = 0
    pnl_sum = 0.0
    gross_profit = 0.0
    gross_loss = 0.0
    peak = starting_equity
    max_dd = 0.0
    for i in range(1, len(closes)):
        price = closes[i]
        want_long = long_signal[i - 1]
        if want_long and qty == 0.0:
            notional = cash * position_fraction
            cost = notional * cost_mult
            qty = (notional - cost) / price
            cash -= notional
            entry_price = price
        elif not want_long and qty > 0.0:
            gross = qty * price
            cost = gross * cost_mult
            net = gross - cost
            pnl = net - qty * entry_price
            cash += net
            pnl_sum += pnl
            n_round_trips += 1
            if pnl > 0:
                wins += 1
                gross_profit += pnl
            else:
                gross_loss -= pnl
            qty = 0.0
            entry_price = 0.0
        mark = cash + qty * price
        if mark > peak:
            peak = mark
        dd = (peak - mark) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
    if qty > 0.0:
        price = closes[-1]
        gross = qty * price
        cost = gross * cost_mult
        pnl = (gross - cost) - qty * entry_price
        cash += gross - cost
        pnl_sum += pnl
        n_round_trips += 1
        if pnl > 0:
            wins += 1
            gross_profit += pnl
        else:
            gross_loss -= pnl
        qty = 0.0
    wr = wins / n_round_trips if n_round_trips else 0.0
    pf = gross_profit / gross_loss if gross_loss > 0 else None
    total_return = cash / starting_equity - 1.0
    return {
        "n_trades": n_round_trips,
        "win_rate_pct": round(wr * 100, 2),
        "total_return_pct": round(total_return * 100, 4),
        "profit_factor": round(pf, 4) if pf is not None else None,
        "max_drawdown_pct": round(max_dd * 100, 4),
    }


def main():
    cfg_path = Path(__file__).resolve().parent.parent / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text()) or {}
    paper = cfg.get("paper") or {}
    starting_equity = float(paper.get("starting_equity_usd", 24.0))
    position_fraction = float(paper.get("position_fraction", 0.3))
    trend_enabled = bool((cfg.get("shadow") or {}).get("trend_filter_enabled", False))

    now_dt = datetime.now(timezone.utc)
    start_dt = now_dt - timedelta(days=365 * 5 + 1)
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(now_dt.timestamp() * 1000)

    results = {"trend_filter": trend_enabled, "cost_levels_bps": COST_LEVELS_BPS, "coins": {}}
    for coin, symbol in _bt.COIN_SYMBOLS.items():
        try:
            candles = _bt._klines(symbol, start_ms, end_ms)
        except Exception as exc:
            print(f"skip {symbol}: {exc}", file=__import__("sys").stderr)
            continue
        closes = [c["close"] for c in candles]
        trend_flags = _trend_flags(closes, trend_enabled)
        coin_res: dict = {}
        for name, fn in STRATEGIES.items():
            longs = precompute_signals(closes, name, fn, trend_flags)
            levels = {}
            for bps in COST_LEVELS_BPS:
                levels[str(bps)] = simulate(closes, longs, starting_equity, position_fraction, bps)
            # breakeven: smallest tested level with positive return
            breakeven = next((b for b in COST_LEVELS_BPS if levels[str(b)]["total_return_pct"] > 0), None)
            coin_res[name] = {"levels": levels, "breakeven_tested_bps": breakeven}
            print(f"{coin} {name}: " + " | ".join(
                f"{bps}bps: ret={levels[str(bps)]['total_return_pct']}% wr={levels[str(bps)]['win_rate_pct']}%"
                for bps in COST_LEVELS_BPS))
        results["coins"][coin] = coin_res

    out = Path(__file__).resolve().parent.parent / "backtest-results" / "5year" / "cost_sweep.json"
    out.write_text(json.dumps(results, indent=2, sort_keys=True, allow_nan=False), encoding="utf-8")
    print(f"\nwritten: {out}")


if __name__ == "__main__":
    main()
