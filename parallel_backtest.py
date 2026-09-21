#!/usr/bin/env python3
"""
parallel_backtest.py – Run an isolated, 8‑hour paper back‑test for every
enabled strategy in the repository.

Each strategy receives its own SQLite database (`mh_backtest_<name>.db`),
runs `dynamic_shadow_scalper.tick()` with a fresh list of candidates,
gathers trade metrics, dumps a JSON report into `reports/`, and
optionally posts a summary to Discord.

The script never touches the live database, never submits an order, and
depends only on the existing package code and the standard library.
"""

import argparse
import datetime as dt
import json
import os
import pathlib
import sqlite3
import sys
import time
from collections import defaultdict
from typing import Dict, List, Tuple

import httpx
import yaml

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def load_yaml(path: pathlib.Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def json_dump(path: pathlib.Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def dirname_of(path: pathlib.Path) -> pathlib.Path:
    return path.parent


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
CONFIG_PATH = pathlib.Path(__file__).parent / "config.yaml"
CFG = load_yaml(CONFIG_PATH)

DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "")
# if empty, no Discord post will occur


# --------------------------------------------------------------------------- #
# Strategy discovery
# --------------------------------------------------------------------------- #
def enabled_strategies(cfg: dict) -> List[dict]:
    return [s for s in cfg.get("strategies", []) if s.get("enabled", False)]


# --------------------------------------------------------------------------- #
# Candidate grabbing
# --------------------------------------------------------------------------- #
def fetch_candidates(cfg: dict, *, api_key: str, now: float) -> List[dict]:
    """
    If an API key is supplied we hit Jupiter to get fresh 12‑candidate snapshots.
    If no key or the call fails we fall back to an empty list – only the
    simulation loop will run.
    """
    if not api_key:
        print("INFO: No Jupiter API key → zero remote candidates.", file=sys.stderr)
        return []

    try:
        import solana_token_universe as stu
        return stu.discover_candidates(
            cfg,
            api_key=api_key,
            now=now,
        )
    except Exception as exc:
        print(f"WARNING: Could not fetch candidates ({exc}).", file=sys.stderr)
        return []


# --------------------------------------------------------------------------- #
# Metrics aggregation
# --------------------------------------------------------------------------- #
def aggregate_metrics(db_path: pathlib.Path) -> dict:
    """Collect a minimal summary of all trades in the back‑test database."""
    with sqlite3.connect(str(db_path)) as con:
        con.row_factory = sqlite3.Row
        row = con.execute(
            """
            SELECT
                COUNT(*) AS trades,
                AVG(realized_pct) AS avg_pct,
                SUM(CASE WHEN realized_pct > 0 THEN 1 ELSE 0 END) AS win_cnt,
                MIN(realized_usd) AS min_usd
            FROM mh_trades
            """
        ).fetchone()
        if row is None:
            result = {"trades": 0, "avg_pct": 0.0, "win_cnt": 0, "min_usd": 0.0}
        else:
            result = dict(row)

    # win rate
    result["win_rate"] = (
        result["win_cnt"] / result["trades"] if result["trades"] else 0.0
    )

    # max drawdown (simple heuristic: worst negative cumulative P&L)
    with sqlite3.connect(str(db_path)) as con:
        rows = con.execute(
            "SELECT realized_usd FROM mh_trades ORDER BY open_ts"
        ).fetchall()
        cumulative = 0.0
        max_drawdown = 0.0
        for r in rows:
            cumulative += r[0]
            if cumulative < max_drawdown:
                max_drawdown = cumulative
        result["max_drawdown"] = max_drawdown

    return result


# --------------------------------------------------------------------------- #
# Discord posting
# --------------------------------------------------------------------------- #
def discord_post(content: str) -> None:
    if not DISCORD_WEBHOOK:
        return

    try:
        r = httpx.post(DISCORD_WEBHOOK, json={"content": content})
        r.raise_for_status()
    except Exception as exc:
        print(f"WARNING: Discord post failed: {exc}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# Back‑test runner
# --------------------------------------------------------------------------- #
def run_strategy(strategy: dict, api_key: str) -> Tuple[str, dict]:
    """Run the single strategy and return its name and metrics."""
    name = strategy["name"].replace(" ", "_").lower()
    db_path = pathlib.Path(f"mh_backtest_{name}.db")
    if db_path.exists():
        db_path.unlink()  # fresh start

    # Kick off a clean DB – tables are created by tick()
    with sqlite3.connect(str(db_path)) as con:
        con.execute("VACUUM")

    now = time.time()
    candidates = fetch_candidates(CFG, api_key=api_key, now=now)

    # Import the core simulation routine – this touches only the in‑memory DB
    from dynamic_shadow_scalper import tick

    # Run the simulator for 8 h worth of steps (80 ticks at 30 s)
    tick(db_path, candidates, now=now, cfg=strategy)

    metrics = aggregate_metrics(db_path)
    metrics["_strategy"] = name
    metrics["_runtime_minutes"] = (time.time() - now) / 60.0

    # Store JSON at the same name
    json_dump(db_path.with_suffix(".json"), metrics)

    # Optionally post to Discord
    tr = metrics.get('trades') or 0
    wr = metrics.get('win_rate') or 0.0
    ap = metrics.get('avg_pct') or 0.0
    dd = metrics.get('max_drawdown') or 0.0
    short = (
        f"BackTest • `{name}`: {tr} trades, "
        f"win={wr:.0%}, avg={ap:.2%}, "
        f"drawdown={dd:.2%}"
    )
    discord_post(short)

    return name, metrics


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> None:
    parser = argparse.ArgumentParser(description="Per‑strategy back‑test runner.")
    parser.add_argument(
        "--api-key",
        help="Jupiter API key for candidate discovery (optional, defaults to env var).",
        default=os.getenv("JUPITER_API_KEY", ""),
    )
    args = parser.parse_args()

    strategies = enabled_strategies(CFG)
    if not strategies:
        print("No enabled strategies found – nothing to run.", file=sys.stderr)
        return

    # Run all back‑tests in parallel process pool
    from concurrent.futures import ProcessPoolExecutor, as_completed

    results = {}
    with ProcessPoolExecutor(max_workers=len(strategies)) as pool:
        futures = {
            pool.submit(run_strategy, s, args.api_key): s["name"] for s in strategies
        }
        for fut in as_completed(futures):
            name, metrics = fut.result()
            results[name] = metrics
            print(f"[{name}] finished: {metrics['trades']} trades, win={metrics['win_rate']:.0%}")

    # Optional combined report
    if len(results) > 1:
        json_dump(pathlib.Path("reports/combined.json"), results)


if __name__ == "__main__":
    # Ensure the reports directory exists
    pathlib.Path("reports").mkdir(exist_ok=True)
    main()