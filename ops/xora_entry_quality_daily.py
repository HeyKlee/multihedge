#!/usr/bin/env python3
"""Read-only daily XORA-SURVIVAL entry-quality evidence report.

This runner snapshots the evidence DB, validates only immutable observation and
outcome keys, and writes reports outside the trading DB. It never generates an
entry filter or changes a policy. Rejected candidates have no honest future
outcome label yet, so their count is a blocking condition rather than a proxy
for profitability.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MIN_OBSERVATIONS = 100
MIN_OUTCOMES = 30


def snapshot(source: Path, dest: Path) -> None:
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=10)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def finite(row: sqlite3.Row, names: tuple[str, ...]) -> bool:
    try:
        return all(math.isfinite(float(row[name])) for name in names)
    except (KeyError, TypeError, ValueError):
        return False


def analyse(db: Path) -> dict:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        needed = {"mh_shadow_entry_observations", "mh_shadow_entry_outcomes"}
        if not needed <= tables:
            return {"state": "INSUFFICIENT_EVIDENCE", "valid_observations": 0,
                    "valid_outcomes": 0, "orphan_outcomes": 0,
                    "rejected_candidates": 0,
                    "proposal_generation_allowed": False,
                    "blocking_reasons": ["telemetry_tables_missing"]}
        observations = con.execute(
            "SELECT observed_ts,mint,latest_usd,return_5m_pct,return_1h_pct,"
            "buy_volume_5m_usd,sell_volume_5m_usd,entry_signal,entry_opened "
            "FROM mh_shadow_entry_observations ORDER BY observed_ts,mint"
        ).fetchall()
        outcomes = con.execute(
            "SELECT observed_ts,mint,opened_ts,close_ts,exit_reason,realized_pct,realized_usd "
            "FROM mh_shadow_entry_outcomes ORDER BY close_ts,mint"
        ).fetchall()
    finally:
        con.close()

    feature_names = ("observed_ts", "latest_usd", "return_5m_pct", "return_1h_pct",
                     "buy_volume_5m_usd", "sell_volume_5m_usd")
    valid_obs = {(float(r["observed_ts"]), str(r["mint"])): r for r in observations
                 if str(r["mint"]) and finite(r, feature_names)
                 and float(r["latest_usd"]) > 0
                 and float(r["buy_volume_5m_usd"]) >= 0
                 and float(r["sell_volume_5m_usd"]) >= 0}
    valid_outcomes = []
    orphan_outcomes = 0
    for row in outcomes:
        key = (float(row["observed_ts"]), str(row["mint"]))
        if key not in valid_obs or not finite(row, ("observed_ts", "opened_ts", "close_ts", "realized_pct", "realized_usd")):
            orphan_outcomes += 1
            continue
        if float(row["opened_ts"]) != key[0] or float(row["close_ts"]) <= float(row["opened_ts"]):
            orphan_outcomes += 1
            continue
        valid_outcomes.append(row)

    rejected = sum(1 for row in valid_obs.values() if not bool(row["entry_opened"]))
    returns = [float(row["realized_pct"]) for row in valid_outcomes]
    wins = sum(value > 0 for value in returns)
    gross_win = sum(value for value in returns if value > 0)
    gross_loss = abs(sum(value for value in returns if value < 0))
    reasons = []
    if len(valid_obs) < MIN_OBSERVATIONS:
        reasons.append(f"observations_below_{MIN_OBSERVATIONS}")
    if len(valid_outcomes) < MIN_OUTCOMES:
        reasons.append(f"outcomes_below_{MIN_OUTCOMES}")
    if orphan_outcomes:
        reasons.append("orphan_or_invalid_outcomes")
    if rejected:
        reasons.append("rejected_candidates_unlabelled")
    # This runner intentionally cannot produce a trading proposal. It documents
    # when a future, separately designed counterfactual-label collector is needed.
    return {
        "state": "ENTRY_QUALITY_EVIDENCE_REPORT",
        "cohort": {"snapshot_key": hashlib.sha256(db.read_bytes()).hexdigest(),
                   "observation_key": ["observed_ts", "mint"],
                   "outcome_key": ["observed_ts", "mint"],
                   "policy": "immutable append-only observation/outcome linkage"},
        "valid_observations": len(valid_obs),
        "valid_outcomes": len(valid_outcomes),
        "orphan_outcomes": orphan_outcomes,
        "rejected_candidates": rejected,
        "opened_candidates": sum(bool(row["entry_opened"]) for row in valid_obs.values()),
        "metrics": {"mean_realized_pct": sum(returns) / len(returns) if returns else None,
                    "win_rate": wins / len(returns) if returns else None,
                    "profit_factor": gross_win / gross_loss if gross_loss else None},
        "proposal_generation_allowed": False,
        "blocking_reasons": reasons or ["counterfactual_filter_evaluation_not_implemented"],
    }


def render(report: dict) -> str:
    lines = ["# XORA-SURVIVAL Entry-Quality Evidence Report", "",
             f"- Valid observations: {report['valid_observations']}",
             f"- Valid linked outcomes: {report['valid_outcomes']}",
             f"- Rejected candidates without outcome labels: {report['rejected_candidates']}",
             f"- Proposal generation: BLOCKED", "", "## Blocking conditions", ""]
    lines.extend(f"- {reason}" for reason in report["blocking_reasons"])
    lines.extend(["", "This is a read-only evidence report. It never changes entry filters, exits, sizing, configuration, live gates, wallets, or signers."])
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(ROOT / "deploy/data/multihedge.db"))
    parser.add_argument("--output-dir", default=str(ROOT / "entry-quality-results"))
    args = parser.parse_args(argv)
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    snap = out / "authoritative-snapshot.db"
    snapshot(Path(args.db), snap)
    report = analyse(snap)
    (out / "entry_quality.json").write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n")
    (out / "report.md").write_text(render(report))
    print(json.dumps({"state": report["state"], "output_dir": str(out.resolve()),
                      "proposal_generation_allowed": False,
                      "blocking_reasons": report["blocking_reasons"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
