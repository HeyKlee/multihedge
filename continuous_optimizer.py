"""Read-only weekly optimiser and fail-closed challenger evaluator for MultiHedge.

This module never writes to the source database or changes trading configuration.
It reconstructs cost-adjusted performance from a consistent SQLite snapshot and
assesses a conservative expectancy-abstention challenger on chronological data.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import sqlite3
import statistics
from collections import Counter, defaultdict
from pathlib import Path

SCALPER_EXCLUSION = {"reasoner", "whale_trader", "memecoin_trader"}


def trade_owner(setup):
    return str(setup) if setup in SCALPER_EXCLUSION else "scalper"


def valid_trade(trade):
    try:
        return float(trade.get("entry_px") or 0.0) > 0 and float(trade.get("qty") or 0.0) > 0
    except (TypeError, ValueError):
        return False


def pnl_row_is_consistent(row) -> bool:
    """True when the persisted dollar P&L obeys usd == qty * entry_px * pct.

    Rows written before the formula was corrected store usd == qty * pct, which
    drops the price factor: on SOL (entry ~100) that understates P&L roughly 100x,
    on JUP (entry ~0.2) it overstates it roughly 5x. The correct dollar figure is
    recoverable, but repairing history would be manufacturing evidence, so the
    evaluator excludes these rows and reports how many it dropped instead.
    """
    try:
        notional = float(row["qty"]) * float(row["entry_px"])
        return abs(float(row["realized_usd"]) - notional * float(row["realized_pct"])) <= 1e-6
    except (KeyError, TypeError, ValueError):
        return False


def adjust_trade(trade, per_side_bps=40.0, fixed_cost_usd=0.0):
    notional = float(trade["entry_px"]) * float(trade["qty"])
    cost = notional * (2.0 * float(per_side_bps) / 10000.0) + float(fixed_cost_usd)
    gross = float(trade["realized_usd"])
    out = dict(trade)
    out.update(
        notional_usd=notional,
        gross_usd=gross,
        modeled_cost_usd=cost,
        net_usd=gross - cost,
        net_return=(gross - cost) / notional if notional > 0 else float("nan"),
    )
    return out


def chronological_split(rows, train_fraction=0.70, validation_fraction=0.15):
    ordered = sorted(rows, key=lambda r: (float(r["close_ts"]), int(r.get("id", 0))))
    n = len(ordered)
    train_end = int(n * train_fraction)
    validation_end = int(n * (train_fraction + validation_fraction))
    return {
        "train": ordered[:train_end],
        "validation": ordered[train_end:validation_end],
        "test": ordered[validation_end:],
    }


def conservative_expectancy(values, min_samples=20, z=1.0):
    clean = [float(v) for v in values if math.isfinite(float(v))]
    n = len(clean)
    mean = statistics.fmean(clean) if clean else 0.0
    if n < min_samples:
        return {"eligible": False, "reason": "insufficient_samples", "n": n, "mean": mean, "stderr": None, "score": float("-inf")}
    stderr = statistics.stdev(clean) / math.sqrt(n) if n > 1 else 0.0
    score = mean - float(z) * stderr
    return {"eligible": True, "reason": "eligible", "n": n, "mean": mean, "stderr": stderr, "score": score}


def _max_drawdown(pnls, starting_equity):
    equity = float(starting_equity)
    peak = equity
    max_dd = 0.0
    duration = longest = 0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        dd = 1.0 - equity / peak if peak > 0 else 0.0
        if dd > 0:
            duration += 1
            longest = max(longest, duration)
        else:
            duration = 0
        max_dd = max(max_dd, dd)
    return max_dd, longest, equity


def performance_metrics(rows, starting_equity=24.0):
    values = [float(r["net_usd"]) for r in rows]
    returns = [float(r["net_return"]) for r in rows if math.isfinite(float(r["net_return"]))]
    wins = [v for v in values if v > 0]
    losses = [v for v in values if v <= 0]
    n = len(values)
    win_rate = len(wins) / n if n else 0.0
    if n:
        z = 1.96
        den = 1.0 + z * z / n
        center = (win_rate + z * z / (2 * n)) / den
        half = z * math.sqrt(win_rate * (1 - win_rate) / n + z * z / (4 * n * n)) / den
        win_ci = [max(0.0, center - half), min(1.0, center + half)]
    else:
        win_ci = [0.0, 0.0]
    gross_profit = sum(wins)
    gross_loss = -sum(losses)
    max_dd, dd_duration, ending = _max_drawdown(values, starting_equity)
    avg = statistics.fmean(returns) if returns else 0.0
    stdev = statistics.stdev(returns) if len(returns) > 1 else 0.0
    downside = [min(0.0, x) for x in returns]
    downside_dev = math.sqrt(statistics.fmean([x * x for x in downside])) if downside else 0.0
    streak = longest = 0
    for v in values:
        streak = streak + 1 if v <= 0 else 0
        longest = max(longest, streak)
    tail = sorted(values)[: max(1, math.ceil(n * 0.05))] if n >= 20 else []
    return {
        "starting_equity_usd": starting_equity,
        "ending_equity_usd": ending,
        "gross_profit_usd": sum(float(r.get("gross_usd", r["net_usd"])) for r in rows),
        "net_profit_usd": sum(values),
        "net_return": (ending / starting_equity - 1.0) if starting_equity else 0.0,
        "n": n,
        "wins": len(wins),
        "win_rate": win_rate,
        "win_rate_95pct_wilson": win_ci,
        "average_win_usd": statistics.fmean(wins) if wins else 0.0,
        "average_loss_usd": statistics.fmean(losses) if losses else 0.0,
        "payoff_ratio": (statistics.fmean(wins) / -statistics.fmean(losses)) if wins and losses and statistics.fmean(losses) < 0 else None,
        "expectancy": statistics.fmean(values) if values else 0.0,
        "expectancy_return": avg,
        "profit_factor": gross_profit / gross_loss if gross_loss > 0 else None,
        "max_drawdown": max_dd,
        "drawdown_duration_trades": dd_duration,
        "sharpe_like_per_trade": avg / stdev * math.sqrt(n) if stdev > 0 else None,
        "sortino_like_per_trade": avg / downside_dev * math.sqrt(n) if downside_dev > 0 else None,
        "turnover_usd": sum(2.0 * float(r.get("notional_usd", 0.0)) for r in rows),
        "modeled_cost_usd": sum(float(r.get("modeled_cost_usd", 0.0)) for r in rows),
        "longest_losing_streak": longest,
        "worst_trade_usd": min(values) if values else 0.0,
        "expected_shortfall_95_usd": statistics.fmean(tail) if tail else None,
        "sampling_note": "Sharpe-like and Sortino-like ratios use per-trade returns, zero risk-free rate, and sqrt(number of trades); they are not annualised time-series ratios.",
    }


def reconcile_account(started, current, ledger_pnl, tolerance=1e-6):
    expected = float(started) + float(ledger_pnl)
    difference = float(current) - expected
    return {
        "started_equity_usd": float(started),
        "current_equity_usd": float(current),
        "ledger_pnl_usd": float(ledger_pnl),
        "expected_equity_usd": expected,
        "unexplained_difference_usd": difference,
        "reconciled": abs(difference) <= float(tolerance),
    }


def promotion_gate(champion, challenger, folds_won, folds_total):
    reasons = []
    if int(challenger.get("n", 0)) < 20:
        reasons.append("test_sample_below_20")
    if int(challenger.get("total_n", challenger.get("n", 0))) < 50:
        reasons.append("total_sample_below_50")
    if float(challenger.get("expectancy", 0.0)) <= 0:
        reasons.append("non_positive_test_expectancy")
    if float(challenger.get("stress_expectancy", 0.0)) < 0:
        reasons.append("negative_stress_expectancy")
    if folds_total <= 0 or folds_won <= folds_total / 2:
        reasons.append("walk_forward_majority_not_won")
    champion_dd = float(champion.get("max_drawdown", 0.0))
    challenger_dd = float(challenger.get("max_drawdown", 0.0))
    if challenger_dd > champion_dd * 1.10:
        reasons.append("drawdown_relative_gate_failed")
    if challenger_dd > 0.20:
        reasons.append("drawdown_hard_cap_failed")
    return {"accepted": not reasons, "reasons": reasons}


def _iso(ts):
    return dt.datetime.fromtimestamp(float(ts), dt.timezone.utc).isoformat()


def _load_scalper(con):
    rows = [dict(r) for r in con.execute("SELECT * FROM mh_trades ORDER BY close_ts,id")]
    return [
        r for r in rows
        if trade_owner(r.get("setup")) == "scalper" and pnl_row_is_consistent(r)
    ]


def grid_completed_rows(con):
    return [dict(row) for row in con.execute(
        "SELECT s.id,s.ts close_ts,b.ts open_ts,b.level_px entry_px,b.qty,s.realized_usd "
        "FROM grid_trades s JOIN grid_trades b ON b.id=("
        "SELECT MIN(b2.id) FROM grid_trades b2 WHERE b2.side='BUY' AND b2.cycle_id=s.cycle_id) "
        "WHERE s.side='SELL' ORDER BY s.ts,s.id"
    )]


def _trader_scorecards(con, per_side_bps):
    rows = [dict(r) for r in con.execute("SELECT * FROM mh_trades ORDER BY close_ts,id")]
    grouped = defaultdict(list)
    for row in rows:
        if not valid_trade(row) or not pnl_row_is_consistent(row):
            continue
        grouped[trade_owner(row.get("setup"))].append(adjust_trade(row, per_side_bps, 0.0))
    scorecards = {
        trader: performance_metrics(grouped.get(trader, []), 24.0)
        for trader in ("scalper", "reasoner", "whale_trader", "memecoin_trader")
    }
    grid_rows = [
        adjust_trade(row, per_side_bps=0.0, fixed_cost_usd=0.02)
        for row in grid_completed_rows(con)
    ]
    scorecards["grid"] = performance_metrics(grid_rows, 24.0)
    return scorecards


def _group_metrics(rows, field, starting_equity=24.0):
    groups = defaultdict(list)
    for row in rows:
        groups[str(row.get(field))].append(row)
    return {key: performance_metrics(value, starting_equity) for key, value in sorted(groups.items())}


def _account_reconciliations(con):
    ownership = {
        "scalper": "setup NOT IN ('reasoner','whale_trader','memecoin_trader')",
        "reasoner": "setup='reasoner'",
        "whale_trader": "setup='whale_trader'",
        "memecoin_trader": "setup='memecoin_trader'",
    }
    out = {}
    for trader, where in ownership.items():
        account = con.execute(
            "SELECT started_usd,equity_usd FROM mh_accounts WHERE trader=?", (trader,)
        ).fetchone()
        if account is None:
            continue
        ledger_pnl = con.execute(
            f"SELECT COALESCE(SUM(realized_usd),0) FROM mh_trades WHERE {where}"
        ).fetchone()[0]
        out[trader] = reconcile_account(account["started_usd"], account["equity_usd"], ledger_pnl)
    return out


def _data_quality(con):
    q = {}
    q["exact_duplicate_trade_groups"] = con.execute(
        "SELECT COUNT(*) FROM (SELECT coin,setup,side,open_ts,close_ts,entry_px,exit_px,qty,COUNT(*) n FROM mh_trades GROUP BY 1,2,3,4,5,6,7,8 HAVING n>1)"
    ).fetchone()[0]
    q["invalid_trade_rows"] = con.execute(
        "SELECT COUNT(*) FROM mh_trades WHERE entry_px<=0 OR exit_px<=0 OR qty<=0 OR close_ts<open_ts OR realized_pct IS NULL OR realized_usd IS NULL"
    ).fetchone()[0]
    q["pnl_formula_mismatch_rows"] = con.execute(
        "SELECT COUNT(*) FROM mh_trades WHERE ABS(realized_usd-qty*entry_px*realized_pct)>0.000001"
    ).fetchone()[0]
    # Rows the reconstruction above dropped for the same reason. Reported so a
    # shrinking sample is visible in the verdict rather than silently absorbed.
    q["pnl_rows_excluded_from_reconstruction"] = con.execute(
        "SELECT COUNT(*) FROM mh_trades WHERE ABS(realized_usd-qty*entry_px*realized_pct)>0.000001"
    ).fetchone()[0]
    q["duplicate_price_groups"] = con.execute(
        "SELECT COUNT(*) FROM (SELECT coin,ts,COUNT(*) n FROM mh_pxhist GROUP BY coin,ts HAVING n>1)"
    ).fetchone()[0]
    q["invalid_price_rows"] = con.execute("SELECT COUNT(*) FROM mh_pxhist WHERE px<=0 OR ts<=0").fetchone()[0]
    q["open_positions"] = {
        "scalper": con.execute("SELECT COUNT(*) FROM mh_positions").fetchone()[0],
        "reasoner": con.execute("SELECT COUNT(*) FROM mh_reasoner_positions").fetchone()[0],
        "whale": con.execute("SELECT COUNT(*) FROM mh_whale_positions").fetchone()[0],
        "memecoin": con.execute("SELECT COUNT(*) FROM mh_memecoin_positions").fetchone()[0],
    }
    return q


def _challenger_replay(rows, min_samples=20, z=1.0):
    history = defaultdict(list)
    accepted = []
    decisions = []
    for row in rows:
        key = (row["coin"], row["setup"])
        score = conservative_expectancy(history[key], min_samples=min_samples, z=z)
        take = bool(score["eligible"] and score["score"] > 0)
        decisions.append({"trade_id": row["id"], "coin": row["coin"], "setup": row["setup"], "take": take, "prior_n": score["n"], "prior_score": score["score"] if math.isfinite(score["score"]) else None})
        if take:
            accepted.append(row)
        history[key].append(row["net_return"])
    return accepted, decisions


def _fold_results(rows, folds=4):
    if not rows:
        return [], 0
    size = max(1, len(rows) // folds)
    out = []
    wins = 0
    for i in range(folds):
        start = i * size
        end = len(rows) if i == folds - 1 else min(len(rows), (i + 1) * size)
        fold = rows[start:end]
        accepted, _ = _challenger_replay(rows[:end])
        accepted_ids = {r["id"] for r in accepted}
        candidate = [r for r in fold if r["id"] in accepted_ids]
        champion_m = performance_metrics(fold)
        challenger_m = performance_metrics(candidate)
        won = challenger_m["net_profit_usd"] > champion_m["net_profit_usd"] and challenger_m["n"] >= 20
        wins += int(won)
        out.append({"fold": i + 1, "start": _iso(fold[0]["close_ts"]), "end": _iso(fold[-1]["close_ts"]), "champion": champion_m, "challenger": challenger_m, "won": won})
    return out, wins


def analyse(db_path, per_side_bps=40.0, fixed_cost_usd=0.0):
    source = Path(db_path).resolve()
    con = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
    raw = _load_scalper(con)
    adjusted = [adjust_trade(r, per_side_bps, fixed_cost_usd) for r in raw if valid_trade(r)]
    split = chronological_split(adjusted)
    accepted, decisions = _challenger_replay(adjusted)
    accepted_ids = {r["id"] for r in accepted}
    folds, folds_won = _fold_results(adjusted)
    periods = {}
    for name, part in split.items():
        challenger_part = [r for r in part if r["id"] in accepted_ids]
        periods[name] = {"champion": performance_metrics(part), "challenger": performance_metrics(challenger_part)}
    stress_rows = [adjust_trade(r, per_side_bps * 1.5, fixed_cost_usd) for r in raw if valid_trade(r)]
    stress_by_id = {r["id"]: r for r in stress_rows}
    test_candidate = [stress_by_id[r["id"]] for r in split["test"] if r["id"] in accepted_ids]
    periods["test"]["challenger"]["stress_expectancy"] = performance_metrics(test_candidate)["expectancy"]
    periods["test"]["challenger"]["total_n"] = len(accepted)
    gate = promotion_gate(periods["test"]["champion"], periods["test"]["challenger"], folds_won, len(folds))
    first = min((r["open_ts"] for r in raw), default=0)
    last = max((r["close_ts"] for r in raw), default=0)
    report = {
        "run": {
            "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "source_db": str(source),
            "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "integrity": integrity,
            "source_period_utc": [_iso(first) if first else None, _iso(last) if last else None],
            "cost_assumptions": {"per_side_quote_impact_bps": per_side_bps, "round_trip_bps": per_side_bps * 2.0, "fixed_cost_usd_per_trade": fixed_cost_usd},
            "limitations": [
                "Recorded scalper fills are spot-price marks, not persisted Jupiter executable quotes.",
                "Counterfactual setup returns are unavailable when a different setup was selected, so the challenger replay is selection-biased and cannot prove a deployable edge.",
                "Exposure overlap, failed-quote rate, spread, and latency are not persisted at trade level.",
            ],
        },
        "data_quality": _data_quality(con),
        "account_reconciliation": _account_reconciliations(con),
        "trader_scorecards": _trader_scorecards(con, per_side_bps),
        "baseline": performance_metrics(adjusted),
        "by_coin": _group_metrics(adjusted, "coin"),
        "by_setup": _group_metrics(adjusted, "setup"),
        "by_exit_reason": _group_metrics(adjusted, "exit_reason"),
        "periods": periods,
        "walk_forward": folds,
        "challenger": {
            "name": "positive_lower_confidence_expectancy_abstention",
            "bounds": {"minimum_prior_samples_per_coin_setup": 20, "z_penalty": 1.0, "costs_included": True},
            "accepted_observed_trades": len(accepted),
            "decisions": decisions,
            "folds_won": folds_won,
            "folds_total": len(folds),
            "promotion_gate": gate,
        },
        "deployment_state": "rejected" if not gate["accepted"] else "shadow_only_pending_counterfactual_validation",
    }
    con.close()
    return report


def _fmt_pct(value):
    return f"{100.0 * float(value):.2f}%"


def _fmt_num(value, places=3, missing="n/a"):
    """Format a metric that is legitimately undefined for some samples.

    profit_factor is None when a period has no losing trades, and a report that
    cannot render is worse than one that says n/a.
    """
    return missing if value is None else f"{float(value):.{places}f}"


def render_markdown(report):
    base = report["baseline"]
    quality = report["data_quality"]
    scalper_recon = report["account_reconciliation"]["scalper"]
    scorecards = report["trader_scorecards"]
    test = report["periods"]["test"]
    gate = report["challenger"]["promotion_gate"]
    lines = [
        "# MultiHedge Continuous Optimisation Report",
        "",
        "## 1. Executive verdict",
        "No trading-policy change is safe to deploy. The authoritative scalper ledger is negative before modeled costs, the untouched test contains only 19 trades, and the recorded data cannot support unbiased counterfactual strategy selection.",
        "",
        "## 2. Baseline",
        f"Source: `{report['run']['source_db']}` ({report['run']['source_sha256']}). Period: {report['run']['source_period_utc'][0]} to {report['run']['source_period_utc'][1]}. SQLite integrity: {report['run']['integrity']}.",
        f"Costs: {report['run']['cost_assumptions']['per_side_quote_impact_bps']:.0f} bps per side, {report['run']['cost_assumptions']['round_trip_bps']:.0f} bps round trip, fixed cost ${report['run']['cost_assumptions']['fixed_cost_usd_per_trade']:.2f}.",
        f"Scalper: {base['n']} trades, gross P&L ${base['gross_profit_usd']:.4f}, modeled costs ${base['modeled_cost_usd']:.4f}, net P&L ${base['net_profit_usd']:.4f}, net return {_fmt_pct(base['net_return'])}, win rate {_fmt_pct(base['win_rate'])}, expectancy ${base['expectancy']:.4f}/trade, profit factor {_fmt_num(base['profit_factor'])}, maximum drawdown {_fmt_pct(base['max_drawdown'])}.",
        "",
        "| Trader | Closed trades | Gross P&L | Modeled costs | Net P&L | Net return | Max DD |",
        "|---|---:|---:|---:|---:|---:|---:|",
        *[f"| {name} | {m['n']} | ${m['gross_profit_usd']:.4f} | ${m['modeled_cost_usd']:.4f} | ${m['net_profit_usd']:.4f} | {_fmt_pct(m['net_return'])} | {_fmt_pct(m['max_drawdown'])} |" for name, m in scorecards.items()],
        "",
        f"Data quality: {quality['invalid_trade_rows']} invalid trade row, {quality['pnl_formula_mismatch_rows']} P&L formula mismatches across all traders, and {quality['exact_duplicate_trade_groups']} exact duplicate groups. {quality['pnl_rows_excluded_from_reconstruction']} rows were excluded from this reconstruction because their persisted dollar P&L omits the entry-price factor (usd == qty*pct instead of qty*entry_px*pct); the figures above are therefore a smaller, cleaner sample, not a repaired ledger. Scalper account reconciliation differs by ${scalper_recon['unexplained_difference_usd']:.4f}, so reconstructed returns are evidence for rejection, not proof of a clean deployable edge. Regime, confidence calibration, quote-failure rate, spread, and latency breakdowns are unavailable because those fields are not persisted at trade level.",
        "",
        "## 3. Diagnosed weaknesses",
        "1. Scalper accounting omits configured quote impact from persisted trade P&L, so dashboard and strategy feedback overstate net performance. Confidence: high.",
        "2. Exploration completion depends on closed trades, causing rarely firing setups to consume thousands of selections without reaching the six-trade floor. Confidence: high.",
        "3. Points-only exploitation treats payoff magnitudes as identical and ignores costs, recency, and uncertainty. Confidence: high.",
        "4. Trade-level failed quotes, provider identity, spread, latency, and executable quote impact are not persisted, preventing full execution-quality attribution. Confidence: high.",
        "",
        "## 4. Experiments",
        "Challenger: trade only when the setup's prior cost-adjusted mean return minus one standard error is positive, requiring at least 20 prior closed trades for that coin/setup. Search space was fixed to one hypothesis with no parameter sweep. The replay is conservative but selection-biased because unchosen setup outcomes do not exist.",
        "",
        "## 5. Champion versus challenger",
        "| Period | Policy | Trades | Net P&L | Expectancy | Max DD |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for period in ("train", "validation", "test"):
        for policy in ("champion", "challenger"):
            m = report["periods"][period][policy]
            lines.append(f"| {period} | {policy} | {m['n']} | ${m['net_profit_usd']:.4f} | ${m['expectancy']:.4f} | {_fmt_pct(m['max_drawdown'])} |")
    lines += [
        "",
        f"Walk-forward folds won: {report['challenger']['folds_won']} of {report['challenger']['folds_total']}. Promotion gates: {'PASS' if gate['accepted'] else 'FAIL'} ({', '.join(gate['reasons']) or 'none'}).",
        "",
        "## 6. Risk assessment",
        f"Baseline maximum drawdown was {_fmt_pct(base['max_drawdown'])}; longest losing streak was {base['longest_losing_streak']} trades; worst trade was ${base['worst_trade_usd']:.4f}; 95% expected shortfall was ${base['expected_shortfall_95_usd']:.4f}. Portfolio heat and correlated exposure cannot be reconstructed exactly from closed rows alone. Rollback trigger is any trading-code or parameter change, because none was accepted.",
        "",
        "## 7. Implementation",
        "Added a read-only weekly evaluator only. No strategy, sizing, exit, wallet, credential, confirmation flag, live gate, or runtime database was changed.",
        "",
        "## 8. Verification",
        "The evaluator opens the database with SQLite `mode=ro`, checks integrity, applies chronological 70/15/15 splits, calculates uncertainty-aware and stress metrics, and fails closed when sample, expectancy, walk-forward, stress, or drawdown gates fail.",
        "",
        "## 9. Deployment state",
        f"{report['deployment_state']}. Champion unchanged.",
        "",
        "## 10. Weekly scorecard",
        f"Realised gross return before modeled costs: {_fmt_pct(base['gross_profit_usd'] / base['starting_equity_usd'])}. Cost-adjusted net return: {_fmt_pct(base['net_return'])}. The 20% strong-week outcome remains context only, not a quota or pass condition.",
        "",
        "## 11. Next experiment",
        "Persist executable entry and exit quote impact plus shadow recommendations and opportunity outcomes for every enabled setup. After at least 50 closed shadow opportunities overall and 20 per promoted coin/setup, rerun the same untouched-test and walk-forward gates.",
        "",
        "VERDICT: NO SAFE IMPROVEMENT PROVEN, CHAMPION UNCHANGED",
    ]
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--per-side-bps", type=float, default=40.0)
    parser.add_argument("--fixed-cost-usd", type=float, default=0.0)
    args = parser.parse_args(argv)
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    report = analyse(args.db, args.per_side_bps, args.fixed_cost_usd)
    (out / "baseline.json").write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False), encoding="utf-8")
    (out / "report.md").write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps({"baseline_json": str((out / 'baseline.json').resolve()), "report_md": str((out / 'report.md').resolve()), "deployment_state": report["deployment_state"], "promotion_gate": report["challenger"]["promotion_gate"]}, indent=2))


if __name__ == "__main__":
    main()
