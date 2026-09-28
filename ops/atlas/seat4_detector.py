#!/usr/bin/env python3
"""SEAT 4 (ATLAS) duplication detector — READ-ONLY.

Scans the MultiHedge source tree for behaviour implemented more than once,
in the five domains that are NOT already covered by
docs/atlas/FINDINGS/001..007 (which own DB paths, config readers, risk
thresholds, cost/PnL and image drift).

This tool creates no files outside ops/atlas/ and never writes to any
database. It parses source text only.

Domains:
  D1 exit_policy     take-profit / stop-loss / max-hold / trailing decisions
  D2 evidence_gate   live-promotion eligibility (n + win rate + net)
  D3 position_sizing fraction-of-wallet sizing
  D4 model_client    OpenRouter / OpenAI-compatible chat transport + JSON salvage
  D5 asset_registry  mint<->symbol maps hardcoded outside config.yaml

Usage:
    python3 ops/atlas/seat4_detector.py            # human report
    python3 ops/atlas/seat4_detector.py --json     # machine readable
    python3 ops/atlas/seat4_detector.py --quiet    # violations only, exit 1
"""
from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent

# Findings 001-007 already own these; excluded so this tool reports only NEW duplication.
OWNED_BY_PRIOR_FINDINGS = {
    "db_path_construction",      # 001, 003
    "config_yaml_reader",        # 002
    "risk_threshold_literals",   # 004, 007
    "cost_pnl_formula",          # 006
}

SKIP_PREFIXES = (
    "legacy/", "tests/", "reports/", "backtest-results/", "audit/",
    "dataset/", "autotuner-logs/", "entry-quality-results/", "price_history/",
    "deploy/price_history/", "docs/", "local-ai-voice-web/", "node_modules/",
)
SKIP_NAMES = {"mh_collect.py"}  # untracked per FINDING 005
# Tests assert behaviour against a value; they are not a second implementation of it.
SKIP_STEMS = ("test_", "test")
SKIP_SUFFIXES = ("_test.py",)

TP_RE = re.compile(r"\b[A-Z0-9_]*(TAKE_PROFIT|TP_PCT|TP_PCT)\b")
SL_RE = re.compile(r"\b[A-Z0-9_]*(STOP_LOSS|SL_PCT)\b")
MH_RE = re.compile(r"\b[A-Z0-9_]*(MAX_HOLD)\b")
TAIL_RE = re.compile(r"\b[A-Z0-9_]*(TRAIL_ARM|TRAIL_DIST)\b")
POSFRAC_RE = re.compile(r"\b[A-Z0-9_]*(POSITION_FRACTION|position_fraction)\b")
GATE_RE = re.compile(r"(?i)\b(win_rate|profitable_rate|eligible|eligibility|gate_min_|"
                     r"min_closed_trades|min_aggregate|gate_enabled)\b")
# A gate is a function that returns an eligibility verdict, not a keyword.
GATE_FN_RE = re.compile(
    r"def\s+(\w*(?:gate|eligible|eligibility|evidence|qualified)\w*)\s*\(", re.I)
TRADE_SOURCE_RE = re.compile(
    r"(mh_trades|grid_trades|excursions|realized_pct|realized_usd|policy_cohort)")
MINT_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")
CHAT_RE = re.compile(r"chat/completions")


def tracked_python() -> list[Path]:
    out = subprocess.run(("git", "ls-files", "*.py"), cwd=REPO,
                         capture_output=True, text=True, check=False)
    files = []
    for r in out.stdout.split():
        if r.startswith(SKIP_PREFIXES) or Path(r).name in SKIP_NAMES:
            continue
        name = Path(r).name
        if name.startswith(SKIP_STEMS) or name.endswith(SKIP_SUFFIXES):
            continue
        p = REPO / r
        if p.is_file():
            files.append(p)
    return files


def rel(p: Path) -> str:
    return str(p.relative_to(REPO))


def src(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


# ----------------------------------------------------------------- D1 exit policy
DECIDER_NAMES = ("close_checks", "_eval_exit", "_exit_reason", "check_exit",
                 "should_exit", "evaluate_exit", "exit_reason", "risk_params")

# A param-driven engine passes policy in as a dict; a constant-driven one reads
# module globals. Both decide an exit. Both must be found, or the map is wrong.
PARAM_KEY_RE = re.compile(
    r"""["'](take_profit_pct|stop_loss_pct|max_hold_seconds|trail_arm_pct|trail_distance_pct)["']""")


def d1_exit_policy(files: list[Path]) -> list[dict]:
    """Each module that decides an exit is a rival exit engine.

    Two shapes qualify: constant-driven (paper.close_checks) and
    param-driven (dynamic_shadow_scalper._exit_reason).
    """
    out = []
    for p in files:
        r = rel(p)
        text = src(p)
        deciders = [n for n in DECIDER_NAMES if re.search(rf"def\s+{n}\s*\(", text)]
        if not deciders:
            continue
        # only count a function that actually returns an exit verdict
        verdicts = len(re.findall(
            r'return\s+"(take_profit|stop_loss|max_hold|trail_stop)"', text))
        if not verdicts:
            continue
        consts = []
        for i, line in enumerate(text.splitlines(), 1):
            if re.match(r"^\s*[A-Z][A-Z0-9_]*\s*=\s*-?\d", line) and (
                    TP_RE.search(line) or SL_RE.search(line) or MH_RE.search(line)
                    or TAIL_RE.search(line)):
                consts.append({"line": i, "text": line.strip()[:90]})
        params = [i for i, line in enumerate(text.splitlines(), 1) if PARAM_KEY_RE.search(line)]
        driver = "constants" if consts else ("params_dict" if params else "unknown")
        out.append({
            "domain": "D1_exit_policy",
            "file": r,
            "deciders": deciders,
            "verdict_returns": verdicts,
            "driver": driver,
            "constants": consts,
            "param_key_lines": params[:8],
        })
    return out


# ---------------------------------------------------------------- D2 evidence gate
def d2_evidence_gate(files: list[Path]) -> list[dict]:
    """Live-promotion eligibility recomputed from trade history, more than once.

    Structural test: the module defines a gate-shaped function AND derives its
    verdict from closed-trade rows. Keyword matching alone missed
    autonomous_live.strategy_evidence and grid_trader.grid_gate_status.
    """
    out = []
    for p in files:
        r = rel(p)
        text = src(p)
        gate_fns = sorted(set(GATE_FN_RE.findall(text)))
        if not gate_fns or not TRADE_SOURCE_RE.search(text):
            continue
        # a real gate compares a computed statistic against a threshold
        compares = bool(re.search(
            r"(>=|<=|>|<)\s*\w*(min|MIN|minimum|threshold|gate)\w*", text))
        if not compares:
            continue
        thr = []
        for i, line in enumerate(text.splitlines(), 1):
            if re.search(r"(?i)(min_win_rate|min_closed_trades|min_aggregate|"
                         r"MIN_REPLAY_WIN_RATE|gate_min_|minimum_strategy_|"
                         r"minimum_aggregate)", line):
                thr.append({"line": i, "text": line.strip()[:100]})
        out.append({"domain": "D2_evidence_gate", "file": r,
                    "gate_functions": gate_fns, "thresholds": thr})
    return out


# ------------------------------------------------------------- D3 position sizing
def d3_position_sizing(files: list[Path]) -> list[dict]:
    """Each module deciding its own fraction-of-wallet position size."""
    out = []
    for p in files:
        r = rel(p)
        text = src(p)
        if not POSFRAC_RE.search(text):
            continue
        vals = []
        for i, line in enumerate(text.splitlines(), 1):
            m = re.search(r"(?i)(POSITION_FRACTION|position_fraction)[\"']?\s*[:=]\s*([0-9.]+)", line)
            if m:
                vals.append({"line": i, "value": m.group(2), "text": line.strip()[:90]})
        if vals:
            out.append({"domain": "D3_position_sizing", "file": r, "values": vals})
    return out


# ----------------------------------------------------------------- D4 model client
def d4_model_client(files: list[Path]) -> list[dict]:
    """Independent chat-completions transports and JSON-salvage parsers."""
    transport, salvage = [], []
    for p in files:
        r = rel(p)
        text = src(p)
        if CHAT_RE.search(text):
            base = None
            m = re.search(r"(OPENROUTER_URL|OPENROUTER_BASE|_JUP_QUOTE|BASE_URL)\s*=\s*[\"']([^\"']+)", text)
            if m:
                base = m.group(2)
            transport.append({"file": r, "endpoint": base,
                              "line": next(i for i, l in enumerate(text.splitlines(), 1)
                                           if CHAT_RE.search(l))})
        if "raw_decode" in text:
            salvage.append({
                "file": r,
                "line": next(i for i, l in enumerate(text.splitlines(), 1) if "raw_decode" in l),
            })
    return ([{"domain": "D4_model_client", "kind": "chat_transport", "sites": transport}]
            + ([{"domain": "D4_model_client", "kind": "json_salvage", "sites": salvage}]
               if len(salvage) > 1 else []))


# ------------------------------------------------------------- D5 asset registry
def d5_asset_registry(files: list[Path]) -> list[dict]:
    """Mint<->symbol maps hardcoded outside config.yaml (which owns the universe).

    A single incidental address (a pool, USDC, a token account) is not a
    registry. Only a named mapping with 2+ mints is a second universe.
    """
    out = []
    for p in files:
        r = rel(p)
        if r == "config.py":
            continue
        text = src(p)
        mints = sorted(set(MINT_RE.findall(text)))
        if len(mints) < 2:
            continue
        named = sorted(set(re.findall(
            r"^\s*([A-Z][A-Z0-9_]{2,})\s*=\s*\{", text, re.M)))
        # a registry is a dict literal assigned to a descriptive uppercase name
        registries = [n for n in named if re.search(
            r"MINT|SYMBOL|COIN|UNIVERSE|ASSET|TOKEN", n)]
        if not registries:
            continue
        out.append({"domain": "D5_asset_registry", "file": r,
                    "registry_names": registries, "mint_count": len(mints),
                    "mints": mints})
    return out


DOMAINS = {
    "D1_exit_policy": d1_exit_policy,
    "D2_evidence_gate": d2_evidence_gate,
    "D3_position_sizing": d3_position_sizing,
    "D4_model_client": d4_model_client,
    "D5_asset_registry": d5_asset_registry,
}


def main() -> int:
    quiet = "--quiet" in sys.argv
    as_json = "--json" in sys.argv
    files = tracked_python()
    results = {name: fn(files) for name, fn in DOMAINS.items()}

    findings = []
    for name, groups in results.items():
        for g in groups:
            findings.append(g)
    total = len(findings)

    if as_json:
        print(json.dumps({
            "baseline_head": subprocess.run(
                ("git", "rev-parse", "HEAD"), cwd=REPO,
                capture_output=True, text=True, check=False).stdout.strip(),
            "scanned_python_files": len(files),
            "excluded_prior_finding_domains": sorted(OWNED_BY_PRIOR_FINDINGS),
            "duplicate_domains_total": total,
            "domains": results,
        }, indent=2))
        return 0

    if quiet:
        for g in findings:
            print(f"{g['domain']} {g.get('file') or g.get('kind')} "
                  f"{len(g.get('constants') or g.get('values') or g.get('thresholds') or g.get('sites') or g.get('mints') or [])}")
        return 1 if total else 0

    print("ATLAS SEAT 4 — DUPLICATION DETECTOR (read-only)")
    print(f"  scanned {len(files)} git-tracked python modules")
    print(f"  domains: {', '.join(DOMAINS)}")
    print(f"  excluded (owned by FINDINGS 001-007): "
          f"{', '.join(sorted(OWNED_BY_PRIOR_FINDINGS))}\n")
    for name, groups in results.items():
        print(f"  [{'DUP' if groups else ' - '}] {name:20} {len(groups)} modules")
    print()
    for g in findings:
        print(f"--- {g['domain']}")
        if g.get("file"):
            print(f"    {g['file']}")
        else:
            print(f"    kind: {g.get('kind')}")
        for item in (g.get("constants") or g.get("values") or g.get("thresholds")
                     or g.get("sites") or g.get("mints") or []):
            if isinstance(item, dict) and "line" in item:
                owner = item.get("file", g.get("file", "?"))
                print(f"      {owner}:{item['line']}  {item.get('text','')}")
            elif isinstance(item, dict) and "file" in item:
                print(f"      {item['file']}:{item['line']}")
            else:
                print(f"      {item}")
        if g.get("deciders"):
            print(f"      deciders: {', '.join(g['deciders'])}")
        print()
    return 0 if not total else 1


if __name__ == "__main__":
    sys.exit(main())
