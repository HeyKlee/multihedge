"""Persistent inventory and deterministic exits for dynamic live scalp positions."""

from __future__ import annotations

import sqlite3
from pathlib import Path

TAKE_PROFIT_PCT = 0.03
STOP_LOSS_PCT = -0.02
TRAIL_ARM_PCT = 0.02
TRAIL_DISTANCE_PCT = 0.01
MAX_HOLD_SECONDS = 900


def _connect(path: Path):
    con = sqlite3.connect(Path(path), timeout=30)
    con.row_factory = sqlite3.Row
    con.execute(
        "CREATE TABLE IF NOT EXISTS mh_live_inventory ("
        "mint TEXT PRIMARY KEY,ticker TEXT NOT NULL,decimals INTEGER NOT NULL,"
        "amount_atomic INTEGER NOT NULL,cost_usdc_atomic INTEGER NOT NULL,"
        "entry_usd REAL NOT NULL,peak_usd REAL NOT NULL,opened_ts REAL NOT NULL,"
        "updated_ts REAL NOT NULL)"
    )
    return con


def get_holding(path: Path, mint: str) -> dict | None:
    with _connect(path) as con:
        row = con.execute("SELECT * FROM mh_live_inventory WHERE mint=?", (mint,)).fetchone()
    return dict(row) if row else None


def list_holdings(path: Path) -> list[dict]:
    with _connect(path) as con:
        rows = con.execute("SELECT * FROM mh_live_inventory ORDER BY opened_ts").fetchall()
    return [dict(row) for row in rows]


def record_fill(path: Path, intent, reconciliation: dict, *, ticker: str, decimals: int,
                price_usd: float, now: float) -> None:
    if reconciliation.get("verified") is not True:
        raise ValueError("cannot record an unverified fill")
    if intent.side == "BUY":
        mint = intent.output_mint
        received = int(reconciliation["output_atomic"])
        spent = int(reconciliation["input_atomic"])
        if received <= 0 or spent <= 0:
            raise ValueError("invalid reconciled buy amounts")
        with _connect(path) as con:
            current = con.execute(
                "SELECT * FROM mh_live_inventory WHERE mint=?", (mint,)
            ).fetchone()
            amount = received + (int(current["amount_atomic"]) if current else 0)
            cost = spent + (int(current["cost_usdc_atomic"]) if current else 0)
            entry = (cost / 1_000_000) / (amount / (10 ** decimals))
            opened = float(current["opened_ts"]) if current else now
            peak = max(float(current["peak_usd"]) if current else price_usd, price_usd)
            con.execute(
                "INSERT INTO mh_live_inventory(mint,ticker,decimals,amount_atomic,cost_usdc_atomic,"
                "entry_usd,peak_usd,opened_ts,updated_ts) VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(mint) DO UPDATE SET ticker=excluded.ticker,decimals=excluded.decimals,"
                "amount_atomic=excluded.amount_atomic,cost_usdc_atomic=excluded.cost_usdc_atomic,"
                "entry_usd=excluded.entry_usd,peak_usd=excluded.peak_usd,updated_ts=excluded.updated_ts",
                (mint, ticker[:24], decimals, amount, cost, entry, peak, opened, now),
            )
        return
    mint = intent.input_mint
    sold = int(reconciliation["input_atomic"])
    if sold <= 0:
        raise ValueError("invalid reconciled sell amount")
    with _connect(path) as con:
        current = con.execute(
            "SELECT * FROM mh_live_inventory WHERE mint=?", (mint,)
        ).fetchone()
        if current is None:
            raise ValueError("sold token is not registered inventory")
        remaining = int(current["amount_atomic"]) - sold
        if remaining <= 0:
            con.execute("DELETE FROM mh_live_inventory WHERE mint=?", (mint,))
        else:
            ratio = remaining / int(current["amount_atomic"])
            con.execute(
                "UPDATE mh_live_inventory SET amount_atomic=?,cost_usdc_atomic=?,updated_ts=? WHERE mint=?",
                (remaining, round(int(current["cost_usdc_atomic"]) * ratio), now, mint),
            )


def forced_exit(path: Path, prices: dict[str, float], *, now: float) -> dict | None:
    """Update peaks and return one deterministic risk-reduction decision."""
    with _connect(path) as con:
        positions = con.execute("SELECT * FROM mh_live_inventory ORDER BY opened_ts").fetchall()
        for position in positions:
            try:
                price = float(prices[position["mint"]])
            except (KeyError, TypeError, ValueError):
                continue
            if price <= 0:
                continue
            entry = float(position["entry_usd"])
            peak = max(float(position["peak_usd"]), price)
            con.execute(
                "UPDATE mh_live_inventory SET peak_usd=?,updated_ts=? WHERE mint=?",
                (peak, now, position["mint"]),
            )
            change = price / entry - 1
            reason = None
            if change >= TAKE_PROFIT_PCT:
                reason = "take_profit"
            elif change <= STOP_LOSS_PCT:
                reason = "stop_loss"
            elif now - float(position["opened_ts"]) >= MAX_HOLD_SECONDS:
                reason = "max_hold"
            elif peak / entry - 1 >= TRAIL_ARM_PCT and price / peak - 1 <= -TRAIL_DISTANCE_PCT:
                reason = "trail_stop"
            if reason:
                return {
                    "action": "SELL", "symbol": position["mint"], "confidence": 1.0,
                    "expected_reward_nzd": 0.0, "expected_loss_nzd": 0.0,
                    "exit_reason": reason,
                }
    return None
