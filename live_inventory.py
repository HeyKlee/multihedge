"""Persistent inventory and deterministic exits for dynamic live scalp positions."""

from __future__ import annotations

import sqlite3
from pathlib import Path

# Memecoin scalp (fast): tight targets, short hold. This is the default for
# anything in the dynamic universe that is not a curated backed coin.
MEME_TAKE_PROFIT_PCT = 0.02
MEME_STOP_LOSS_PCT = -0.01
MEME_TRAIL_ARM_PCT = 0.02
MEME_TRAIL_DISTANCE_PCT = 0.01
MEME_MAX_HOLD_SECONDS = 900

# Serious / backed coins (present in the config `coins` list, e.g. JUP, ETH):
# day-traded, allowed to swing over hours instead of a 15-minute scalp.
SERIOUS_TAKE_PROFIT_PCT = 0.05
SERIOUS_STOP_LOSS_PCT = -0.025
SERIOUS_TRAIL_ARM_PCT = 0.04
SERIOUS_TRAIL_DISTANCE_PCT = 0.015
SERIOUS_MAX_HOLD_SECONDS = 6 * 3600

# Legacy names kept so existing imports/tests see the memecoin defaults.
TAKE_PROFIT_PCT = MEME_TAKE_PROFIT_PCT
STOP_LOSS_PCT = MEME_STOP_LOSS_PCT
TRAIL_ARM_PCT = MEME_TRAIL_ARM_PCT
TRAIL_DISTANCE_PCT = MEME_TRAIL_DISTANCE_PCT
MAX_HOLD_SECONDS = MEME_MAX_HOLD_SECONDS


def _serious_mints(cfg: dict | None) -> set[str]:
    if not isinstance(cfg, dict):
        return set()
    return {c["mint"] for c in cfg.get("coins", []) if isinstance(c, dict) and c.get("mint")}


def risk_params(mint: str, cfg: dict | None) -> dict:
    """Return per-coin exit params. Backed coins (in config `coins`) day-trade;
    everything else (dynamic universe memecoin) scalp fast."""
    if mint in _serious_mints(cfg):
        return {
            "take_profit_pct": SERIOUS_TAKE_PROFIT_PCT,
            "stop_loss_pct": SERIOUS_STOP_LOSS_PCT,
            "trail_arm_pct": SERIOUS_TRAIL_ARM_PCT,
            "trail_distance_pct": SERIOUS_TRAIL_DISTANCE_PCT,
            "max_hold_seconds": SERIOUS_MAX_HOLD_SECONDS,
            "mode": "SERIOUS",
        }
    return {
        "take_profit_pct": MEME_TAKE_PROFIT_PCT,
        "stop_loss_pct": MEME_STOP_LOSS_PCT,
        "trail_arm_pct": MEME_TRAIL_ARM_PCT,
        "trail_distance_pct": MEME_TRAIL_DISTANCE_PCT,
        "max_hold_seconds": MEME_MAX_HOLD_SECONDS,
        "mode": "MEME",
    }


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


def forced_exit(path: Path, prices: dict[str, float], *, now: float, cfg: dict | None = None) -> dict | None:
    """Update peaks and return one deterministic risk-reduction decision.

    Exit thresholds are per-coin: backed coins (in config `coins`) day-trade
    over hours; memecoin positions scalp fast.
    """
    with _connect(path) as con:
        positions = con.execute("SELECT * FROM mh_live_inventory ORDER BY opened_ts").fetchall()
        for position in positions:
            try:
                price = float(prices[position["mint"]])
            except (KeyError, TypeError, ValueError):
                continue
            if price <= 0:
                continue
            params = risk_params(position["mint"], cfg)
            entry = float(position["entry_usd"])
            peak = max(float(position["peak_usd"]), price)
            con.execute(
                "UPDATE mh_live_inventory SET peak_usd=?,updated_ts=? WHERE mint=?",
                (peak, now, position["mint"]),
            )
            change = price / entry - 1
            reason = None
            if change >= params["take_profit_pct"]:
                reason = "take_profit"
            elif change <= params["stop_loss_pct"]:
                reason = "stop_loss"
            elif now - float(position["opened_ts"]) >= params["max_hold_seconds"]:
                reason = "max_hold"
            elif (peak / entry - 1 >= params["trail_arm_pct"]
                  and price / peak - 1 <= -params["trail_distance_pct"]):
                reason = "trail_stop"
            if reason:
                return {
                    "action": "SELL", "symbol": position["mint"], "confidence": 1.0,
                    "expected_reward_nzd": 0.0, "expected_loss_nzd": 0.0,
                    "exit_reason": reason, "mode": params["mode"],
                }
    return None
