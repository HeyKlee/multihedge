#!/usr/bin/env python3
"""ATLAS Seat 4 — single-exit-engine / single-gate gate (READ-ONLY, proposal).

This is a PROPOSAL for CI, not an applied change. It lives under ops/atlas/
per the ATLAS read-only constraint and modifies no existing module, config,
test or database. It exits 1 while the duplication in
ops/atlas/SEAT4_DUPLICATION_MAP.md exists, exactly like
tools/check_single_truth.py does.

Rules added on top of the existing 5:

  E1 single_exit_engine   exactly one module may decide an exit
  E2 constitution_max_hold  no engine may close a position on age alone
  E3 single_promotion_gate  the dashboard may not hardcode eligibility
  E4 single_position_sizer  one owner of fraction-of-wallet sizing
  E5 no_hardcoded_universe  mint maps belong to config.yaml

Usage:
    python3 ops/atlas/seat4_gate.py           # full report
    python3 ops/atlas/seat4_gate.py --quiet   # violations only, exit 1
    python3 ops/atlas/seat4_gate.py --json
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent

# The one module that is *allowed* to own each behaviour, once the refactor lands.
# Today these are aspirational: the refactor has not been applied, so the gate
# is expected to fail. That is the intent.
CANONICAL_EXIT_ENGINE = {"domain/risk/exit_policy.py", "ops/atlas/_unused.py"}
CANONICAL_SIZER = {"domain/risk/sizing.py", "ops/atlas/_unused.py"}
CANONICAL_UNIVERSE = {"config.py", "config/settings.py", "ops/atlas/_unused.py"}

SKIP_PREFIXES = (
    "legacy/", "tests/", "reports/", "backtest-results/", "audit/",
    "dataset/", "autotuner-logs/", "entry-quality-results/", "price_history/",
    "deploy/price_history/", "docs/", "local-ai-voice-web/", "node_modules/",
    "ops/",  # this proposal directory is analysis, not runtime
)
SKIP_NAMES = {"mh_collect.py"}

EXIT_DECIDER_RE = re.compile(
    r'def\s+(\w*(?:close_checks|_eval_exit|_exit_reason|check_exit|should_exit|'
    r'evaluate_exit|forced_exit)\w*)\s*\(')
VERDICT_RE = re.compile(r'return\s+"(take_profit|stop_loss|max_hold|trail_stop)"')
MAXHOLD_ALONE_RE = re.compile(
    r"(?m)^[^\n]*max_hold[^\n]*$\n?[^\n]*(?:return|reason\s*=)\s*[\"']?max_hold")
SIZER_RE = re.compile(r"(?i)\b(POSITION_FRACTION|position_fraction)\b")
DASH_FILES = {"mh_dash.py", "mh_ui.py", "dash_web.py"}
GATE_NUM_RE = re.compile(
    r"""(eligible|win_rate|profitable_rate)[^\n]{0,60}?(>=|<=)\s*([0-9.]+)""")
MINT_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")


def tracked_python() -> list[Path]:
    out = subprocess.run(("git", "ls-files", "*.py"), cwd=REPO,
                         capture_output=True, text=True, check=False)
    files = []
    for r in out.stdout.split():
        if r.startswith(SKIP_PREFIXES) or Path(r).name in SKIP_NAMES:
            continue
        if Path(r).name.startswith("test"):
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


def rule_single_exit_engine(files) -> list[dict]:
    """E1: only the canonical module may decide an exit."""
    viol = []
    for p in files:
        r = rel(p)
        if r in CANONICAL_EXIT_ENGINE:
            continue
        text = src(p)
        m = EXIT_DECIDER_RE.search(text)
        if m and VERDICT_RE.search(text):
            viol.append({
                "rule": "E1_single_exit_engine", "file": r,
                "detail": f"{m.group(1)}() is a second exit engine",
                "canonical_owner": sorted(CANONICAL_EXIT_ENGINE),
            })
    return viol


def rule_max_hold_not_age_alone(files) -> list[dict]:
    """E2: an exit engine must not close a position on age alone.

    Matches AGENTS.md:70-72. An engine is conformant when its max_hold branch is
    guarded by a peak-crossed-TP condition as well as the timer.
    """
    viol = []
    for p in files:
        r = rel(p)
        text = src(p)
        if not (EXIT_DECIDER_RE.search(text) and VERDICT_RE.search(text)):
            continue
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if "max_hold" not in line:
                continue
            # The guard may sit above the branch (DSS) or below it (live_inventory),
            # so search both directions before calling it a violation.
            lo = max(0, i - 3)
            window = "\n".join(lines[lo:i + 4])
            guarded = ("peak" in window and "take_profit" in window)
            if not guarded:
                viol.append({
                    "rule": "E2_max_hold_age_alone", "file": r, "line": i + 1,
                    "detail": "max_hold fires without a peak-crossed-TP guard "
                              "(violates AGENTS.md:70-72)",
                })
                break
    return viol


def rule_single_promotion_gate(files) -> list[dict]:
    """E3: the dashboard may not hardcode eligibility thresholds."""
    viol = []
    for p in files:
        r = rel(p)
        if r not in DASH_FILES:
            continue
        text = src(p)
        for i, line in enumerate(text.splitlines(), 1):
            m = GATE_NUM_RE.search(line)
            if m and m.group(3) not in ("0", "1"):
                viol.append({
                    "rule": "E3_dashboard_gate_literal", "file": r, "line": i,
                    "detail": f"eligibility hardcoded as {m.group(2)} {m.group(3)} "
                              f"in the dashboard: {line.strip()[:110]}",
                    "canonical_owner": "the single promotion-gate module",
                })
    return viol


def rule_single_position_sizer(files) -> list[dict]:
    """E4: one owner of fraction-of-wallet sizing."""
    viol = []
    for p in files:
        r = rel(p)
        if r in CANONICAL_SIZER:
            continue
        text = src(p)
        for i, line in enumerate(text.splitlines(), 1):
            m = re.search(
                r"(?i)(POSITION_FRACTION|position_fraction)[\"']?\s*[:=]\s*([0-9.]+)",
                line)
            if m:
                viol.append({
                    "rule": "E4_position_sizing_literal", "file": r, "line": i,
                    "detail": f"own position fraction {m.group(2)}: {line.strip()[:100]}",
                    "canonical_owner": sorted(CANONICAL_SIZER),
                })
    return viol


def rule_no_hardcoded_universe(files) -> list[dict]:
    """E5: mint maps belong to config.yaml, not to modules."""
    viol = []
    for p in files:
        r = rel(p)
        if r in CANONICAL_UNIVERSE:
            continue
        text = src(p)
        mints = sorted(set(MINT_RE.findall(text)))
        if len(mints) < 2:
            continue
        registries = [n for n in set(re.findall(r"^\s*([A-Z][A-Z0-9_]{2,})\s*=\s*\{", text, re.M))
                      if re.search(r"MINT|SYMBOL|COIN|UNIVERSE|ASSET|TOKEN", n)]
        for n in registries:
            viol.append({
                "rule": "E5_hardcoded_universe", "file": r,
                "detail": f"{n} re-declares the mint universe ({len(mints)} mints) "
                          f"outside config.yaml",
                "canonical_owner": sorted(CANONICAL_UNIVERSE),
            })
    return viol


RULES = {
    "E1_single_exit_engine": rule_single_exit_engine,
    "E2_max_hold_age_alone": rule_max_hold_not_age_alone,
    "E3_dashboard_gate_literal": rule_single_promotion_gate,
    "E4_position_sizing_literal": rule_single_position_sizer,
    "E5_hardcoded_universe": rule_no_hardcoded_universe,
}


def main() -> int:
    quiet = "--quiet" in sys.argv
    as_json = "--json" in sys.argv
    files = tracked_python()
    results = {name: fn(files) for name, fn in RULES.items()}
    total = sum(len(v) for v in results.values())

    if as_json:
        print(json.dumps({
            "baseline_head": subprocess.run(("git", "rev-parse", "HEAD"), cwd=REPO,
                                            capture_output=True, text=True,
                                            check=False).stdout.strip(),
            "status": "proposal_not_applied",
            "violations": total,
            "rules": results,
        }, indent=2))
        return 0

    if quiet:
        for name, viols in results.items():
            for v in viols:
                loc = f"{v['file']}:{v['line']}" if "line" in v else v["file"]
                print(f"{name} {loc} {v.get('detail','')}")
        return 1 if total else 0

    print("ATLAS SEAT 4 — EXIT/GATE/SIZING GATE (PROPOSAL, read-only)")
    print("  complements tools/check_single_truth.py; does not replace it\n")
    for name, viols in sorted(results.items()):
        status = "PASS" if not viols else "FAIL"
        print(f"  [{status}] {name:32} {len(viols):3} violations")
    print(f"\n  total: {total}")
    if total:
        print("\n  Canonical modules that must exist to clear these:")
        for path in sorted(CANONICAL_EXIT_ENGINE | CANONICAL_SIZER | CANONICAL_UNIVERSE):
            if path.endswith("_unused.py"):
                continue
            mark = "present" if (REPO / path).exists() else "MISSING"
            print(f"    {mark:8} {path}")
        print("\n  All violations:")
        for name, viols in sorted(results.items()):
            for v in viols:
                loc = f"{v['file']}:{v['line']}" if "line" in v else v["file"]
                print(f"    {name} {loc}")
                print(f"        {v.get('detail','')}")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
