"""5-year hourly candle back-test for every enabled MultiHedge strategy.

Reads coin list from config.yaml, fetches 1h klines from Binance, drives
the real strategy signal functions from strategy.py, and writes a JSON
summary per coin×strategy into backtest-results/5year/<coin>_<strategy>.json.

Paper assumptions (from config.yaml):
  - starting_equity_usd: 24.0
  - position_fraction: 0.3  (fraction of cash used per buy)
  - per_side_bps: 40  (0.4% quote impact each side; 0.8% round-trip)

Safety / invariants:
  - Read-only: no writes to deploy/data/multihedge.db or any production path.
  - No live discovery, signer, or Jupiter calls.
  - All prices are sampled close prices; no intrabar fills.
  - Results are labelled paper-only and must not be presented as live P&L.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
import time
import urllib.request
from collections import defaultdict
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

STRATEGIES = {
    "momentum_breakout": momentum_breakout_signal,
    "mean_reversion": mean_reversion_signal,
    "rsi_oversold": rsi_oversold_signal,
    "vwap_reversion": vwap_reversion_signal,
}

# Warmup lengths match the minimum closes each signal requires.
WARMUP = {
    "momentum_breakout": 20,
    "mean_reversion": 10,
    "rsi_oversold": 3,
    "vwap_reversion": 15,
}

BINANCE_BASE = "https://api.binance.com/api/v3"
COIN_SYMBOLS = {
    "SOL": "SOLUSDT",
    "JUP": "JUPUSDT",
    "ETH": "ETHUSDT",
}
# Binance caps klines at 1000 per request; we paginate by startTime.
KLINES_LIMIT = 1000


def _klines(symbol: str, start_ms: int, end_ms: int) -> list[dict]:
    rows = []
    cursor = start_ms
    max_retries = 5
    backoff = 1
    while cursor < end_ms:
        url = (
            f"{BINANCE_BASE}/klines?symbol={symbol}&interval=1h"
            f"&limit={KLINES_LIMIT}&startTime={cursor}&endTime={end_ms}"
        )
        data = None
        for attempt in range(max_retries):
            try:
                with urllib.request.urlopen(url, timeout=30) as resp:
                    data = json.loads(resp.read().decode())
                break
            except Exception as exc:
                if attempt == max_retries - 1:
                    raise
                time.sleep(backoff)
                backoff *= 2
        if data is None or not data:
            break
        for k in data:
            ts = k[0] / 1000.0
            close = float(k[4])
            if close <= 0:
                continue
            rows.append({"ts": ts, "close": close})
        cursor = data[-1][0] + 60_000  # next ms after last candle
        if len(data) < KLINES_LIMIT:
            break
        time.sleep(0.15)  # be polite to the API
    rows.sort(key=lambda r: r["ts"])
    return rows


def _load_config() -> dict:
    cfg_path = Path(__file__).resolve().parent.parent / "config.yaml"
    with cfg_path.open() as f:
        return yaml.safe_load(f) or {}


def _starting_equity(cfg: dict) -> float:
    return float((cfg.get("paper") or {}).get("starting_equity_usd", 24.0))


def _position_fraction(cfg: dict) -> float:
    return float((cfg.get("paper") or {}).get("position_fraction", 0.3))


def backtest(
    closes: list[float],
    signal_fn,
    *,
    starting_equity: float,
    position_fraction: float,
    per_side_bps: float = 40.0,
    trend_filter_enabled: bool = False,
    trend_period: int = 200,  # 200 hours ≈ 8 days for 4h-equivalent trend
):
    """Simple next-bar long-only back-test.

    - Buys at candle close when signal turns LONG while flat.
    - Sells at candle close on any non-LONG signal while long.
    - Costs applied as per-side bps on notional.
    - Optional trend filter: only allow LONG when price > SMA(trend_period).
    Returns dict of metrics and a list of trade dicts.
    """
    cost_mult = per_side_bps / 10_000.0
    equity = starting_equity
    cash = starting_equity
    qty = 0.0
    entry_price = 0.0
    trades = []
    equity_curve = [{"index": 0, "equity": equity, "cash": cash, "qty": 0.0}]
    peak = equity
    max_dd = 0.0
    dd_start_idx = 0
    in_position = False

    for i in range(1, len(closes)):
        signal = signal_fn(closes[: i + 1])
        price = closes[i]
        
        # Trend filter: compute SMA if enabled and enough data
        trend_ok = True
        if trend_filter_enabled and i >= trend_period:
            sma = sum(closes[i - trend_period:i]) / trend_period
            if signal == "LONG" and price <= sma:
                trend_ok = False
        
        if signal == "LONG" and not in_position and trend_ok:
            # Buy
            notional = cash * position_fraction
            cost = notional * cost_mult
            qty_bought = (notional - cost) / price
            if qty_bought <= 0:
                continue
            cash -= notional
            qty = qty_bought
            entry_price = price
            in_position = True
            trades.append(
                {
                    "side": "BUY",
                    "index": i,
                    "ts": None,
                    "price": price,
                    "qty": qty_bought,
                    "cost": cost,
                }
            )
        elif in_position and signal != "LONG":
            # Sell
            gross = qty * price
            cost = gross * cost_mult
            net = gross - cost
            cash += net
            pnl = net - qty * entry_price
            trades[-1].update(
                {
                    "side": "SELL",
                    "index": i,
                    "price": price,
                    "gross": gross,
                    "cost": cost,
                    "net": net,
                    "pnl": pnl,
                }
            )
            equity = cash + qty * price
            qty = 0.0
            in_position = False
            entry_price = 0.0
        # Update equity for curve
        mark = cash + qty * price
        if mark > peak:
            peak = mark
        dd = (peak - mark) / peak if peak > 0 else 0.0
        max_dd = max(max_dd, dd)
        equity_curve.append(
            {"index": i, "equity": mark, "cash": cash, "qty": qty}
        )

    # Close any open position at final price
    if in_position:
        price = closes[-1]
        gross = qty * price
        cost = gross * cost_mult
        net = gross - cost
        pnl = net - qty * entry_price
        cash += net
        trades[-1].update(
            {"side": "SELL", "index": len(closes) - 1, "price": price,
             "gross": gross, "cost": cost, "net": net, "pnl": pnl}
        )
        in_position = False

    # Metrics on closed round trips (each BUY has matching SELL now)
    round_trips = [t for t in trades if t["side"] == "SELL"]
    pnls = [t["pnl"] for t in round_trips]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    n = len(round_trips)
    win_rate = len(wins) / n if n else None
    gross_profit = sum(wins)
    gross_loss = -sum(losses)
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else None
    avg_win = sum(wins) / len(wins) if wins else None
    avg_loss = sum(losses) / len(losses) if losses else None
    payoff = avg_win / -avg_loss if avg_win and avg_loss and avg_loss < 0 else None
    total_net = sum(pnls)
    ending_equity = cash  # no open position
    total_return = (ending_equity / starting_equity - 1.0) if starting_equity else 0.0
    # Sortino-like: downside deviation of per-trade returns
    if pnls:
        mean_pnl = sum(pnls) / len(pnls)
        downside_sq = sum((min(0, p) ** 2) for p in pnls) / len(pnls)
        downside_dev = math.sqrt(downside_sq) if downside_sq > 0 else None
        sortino = (
            (mean_pnl / downside_dev) * math.sqrt(len(pnls))
            if downside_dev and downside_dev > 0 else None
        )
    else:
        sortino = None
    # Sharpe-like (no risk-free rate)
    if len(pnls) > 1:
        std = math.sqrt(
            sum((p - sum(pnls) / len(pnls)) ** 2 for p in pnls) / (len(pnls) - 1)
        )
        sharpe = (sum(pnls) / len(pnls)) / std * math.sqrt(len(pnls)) if std > 0 else None
    else:
        sharpe = None

    return {
        "starting_equity_usd": starting_equity,
        "ending_equity_usd": round(ending_equity, 4),
        "total_return_pct": round(total_return * 100, 4),
        "n_trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(win_rate * 100, 2) if win_rate is not None else None,
        "avg_win_usd": round(avg_win, 4) if avg_win is not None else None,
        "avg_loss_usd": round(avg_loss, 4) if avg_loss is not None else None,
        "payoff_ratio": round(payoff, 4) if payoff is not None else None,
        "gross_profit_usd": round(gross_profit, 4),
        "gross_loss_usd": round(gross_loss, 4),
        "profit_factor": round(profit_factor, 4) if profit_factor is not None else None,
        "total_net_pnl_usd": round(total_net, 4),
        "max_drawdown_pct": round(max_dd * 100, 4),
        "sortino_per_trade": round(sortino, 4) if sortino is not None else None,
        "sharpe_per_trade": round(sharpe, 4) if sharpe is not None else None,
        "per_side_bps": 40.0,
        "round_trip_bps": 80.0,
        "position_fraction": position_fraction,
        "closes_used": len(closes),
        "caveats": [
            "Next-bar execution at close; no latency, spread, or partial fills.",
            "Long-only; no shorting.",
            "Costs applied as per-side quote impact only; no fee model.",
            "Signal warmup periods skipped; early candles not traded.",
            "Data gaps in Binance klines are silently skipped; no interpolation.",
            "Paper-only — not a live P&L forecast.",
        ],
        "trades": round_trips,
    }, equity_curve


def _write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def run_one_coin(
    coin: str,
    symbol: str,
    closes: list[float],
    cfg: dict,
    out_dir: Path,
) -> dict:
    starting_equity = _starting_equity(cfg)
    position_fraction = _position_fraction(cfg)
    trend_filter_enabled = (cfg.get("shadow", {}) or {}).get("trend_filter_enabled", False)
    coin_dir = out_dir / coin.lower()
    coin_dir.mkdir(parents=True, exist_ok=True)
    summary = {"coin": coin, "symbol": symbol, "closes": len(closes), "strategies": {}}
    for name, fn in STRATEGIES.items():
        curve = []
        try:
            metrics, curve = backtest(
                closes,
                fn,
                starting_equity=starting_equity,
                position_fraction=position_fraction,
                trend_filter_enabled=trend_filter_enabled,
            )
        except Exception as exc:
            metrics = {"error": str(exc)}
        summary["strategies"][name] = metrics
        # Write metrics JSON
        (coin_dir / f"{name}.json").write_text(
            json.dumps(metrics, indent=2, sort_keys=True, allow_nan=False),
            encoding="utf-8",
        )
        # Write equity curve CSV (only index + equity to keep small)
        _write_csv(
            coin_dir / f"{name}_equity.csv",
            [{"index": r["index"], "equity": r["equity"]} for r in curve],
        )
    (coin_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True, allow_nan=False),
        encoding="utf-8",
    )
    return summary


def main():
    parser = argparse.ArgumentParser(description="5-year hourly back-test")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("backtest-results/5year"),
        help="Folder to write per-coin JSON/CSV results",
    )
    parser.add_argument(
        "--per-side-bps", type=float, default=40.0, help="Quote impact each side"
    )
    args = parser.parse_args()

    cfg = _load_config()
    out_dir = args.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    # Determine 5-year window in UTC
    now_dt = datetime.now(timezone.utc)
    start_dt = now_dt - timedelta(days=365 * 5 + 1)
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(now_dt.timestamp() * 1000)

    global_summary = {"window_utc": [start_dt.isoformat(), now_dt.isoformat()], "coins": {}}
    for coin, symbol in COIN_SYMBOLS.items():
        print(f"Fetching {symbol} 1h candles {start_dt.date()} → {now_dt.date()} …", file=sys.stderr)
        try:
            candles = _klines(symbol, start_ms, end_ms)
        except Exception as exc:
            print(f"ERROR fetching {symbol}: {exc} – skipping this coin", file=sys.stderr)
            continue
        if len(candles) < 100:
            print(f"WARNING: only {len(candles)} candles for {symbol}; skipping", file=sys.stderr)
            continue
        closes = [c["close"] for c in candles]
        print(f"  {symbol}: {len(closes)} closes, running back-test …", file=sys.stderr)
        coin_summary = run_one_coin(coin, symbol, closes, cfg, out_dir)
        global_summary["coins"][coin] = coin_summary

    # Write combined summary
    (out_dir / "combined_summary.json").write_text(
        json.dumps(global_summary, indent=2, sort_keys=True, allow_nan=False),
        encoding="utf-8",
    )
    print(json.dumps({"output_dir": str(out_dir.resolve()), "coins": list(global_summary["coins"])}, indent=2))


if __name__ == "__main__":
    main()