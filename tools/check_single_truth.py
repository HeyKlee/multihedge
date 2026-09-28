#!/usr/bin/env python3
"""Project ATLAS single-truth enforcement gate.

Fails when the codebase reintroduces a second source for a fact that must have
exactly one owner. This is the CI gate referenced by AGENTS rules A-H and by
docs/atlas/FINDINGS/001 and 002.

Each rule returns a list of violations with file:line evidence. Exit code 1 blocks.

Usage:
    python3 tools/check_single_truth.py            # full report
    python3 tools/check_single_truth.py --quiet    # violations only, exit code
"""
from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Once the canonical modules exist, these become the only legal owners.
CANONICAL_SETTINGS = {"config/settings.py", "src/multihedge/infrastructure/runtime/settings.py"}
CANONICAL_PATHS = {
    "runtime/paths.py",
    "runtime_paths.py",              # the shipped resolver: ATLAS Rule A owner
    "src/multihedge/infrastructure/runtime/paths.py",
}
CANONICAL_POLICY = {
    "domain/policy/resolver.py",
    "live_inventory.py",          # current shared owner of Xora-Survival exit params
    "src/multihedge/domain/risk/policy.py",
}
# Legacy/ops/test readers are reported separately: they are debt, not policy violations.
DEBT_PREFIXES = ("legacy/", "test_", "ops/", "tests/", "reports/", "tools/")

DB_DEFAULT_RE = re.compile(r"Path\([^)]*multihedge\.db|\"multihedge\.db\"|'multihedge\.db'")
DASHBOARD_FILES = {"mh_dash.py", "mh_ui.py", "dash_web.py"}
# Policy magnitudes a UI must never invent. Rendering an absent policy as a number
# is a projection lying about its source (FINDING 002).
UI_POLICY_LITERAL_RE = re.compile(
    r"(trail_distance_pct|take_profit_pct|stop_loss_pct|trail_arm_pct|max_hold_seconds)"
    r"[^,;{}\n]{0,80}?['\"](\d+\.?\d*)['\"]"
)


def tracked_python() -> list[Path]:
    out = subprocess.run(("git", "ls-files", "*.py"), cwd=REPO, capture_output=True, text=True)
    return [REPO / p for p in out.stdout.split() if (REPO / p).is_file()]


def rel(p: Path) -> str:
    try:
        return str(p.relative_to(REPO))
    except ValueError:
        return str(p)


def rule_direct_config_readers() -> list[dict]:
    """Rule B: config.yaml may only be parsed by the canonical settings module."""
    viol = []
    for p in tracked_python():
        r = rel(p)
        if r in CANONICAL_SETTINGS:
            continue
        if r.startswith(DEBT_PREFIXES):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if re.search(r"safe_load|yaml\.load", line) and re.search(r"config\.yaml|['\"]config['\"]", line):
                viol.append({
                    "rule": "B_direct_config_reader", "file": r, "line": i,
                    "detail": line.strip()[:140],
                    "canonical_owner": sorted(CANONICAL_SETTINGS),
                })
    return viol


def rule_db_path_construction() -> list[dict]:
    """Rule A: no module may independently decide where the database lives."""
    viol = []
    for p in tracked_python():
        r = rel(p)
        if r in CANONICAL_PATHS:
            continue
        if r.startswith(DEBT_PREFIXES):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if DB_DEFAULT_RE.search(line) and "multihedge.db" in line:
                viol.append({
                    "rule": "A_db_path_construction", "file": r, "line": i,
                    "detail": line.strip()[:140],
                    "canonical_owner": sorted(CANONICAL_PATHS),
                })
    return viol


def rule_cross_module_global_mutation() -> list[dict]:
    """Rule A: assigning another module's global (paper.DB_PATH = ...) hides the split."""
    viol = []
    for p in tracked_python():
        r = rel(p)
        if r.startswith(DEBT_PREFIXES):
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"), filename=r)
        except (SyntaxError, OSError):
            continue
        for node in ast.walk(tree):
            targets = []
            if isinstance(node, ast.Assign):
                targets = [t for t in node.targets if isinstance(t, ast.Attribute)]
            elif isinstance(node, ast.AugAssign):
                targets = [node.target] if isinstance(getattr(node, "target", None), ast.Attribute) else []
            for t in targets:
                if not isinstance(t, ast.Attribute):
                    continue
                if isinstance(t.value, ast.Name) and t.value.id not in ("os", "sys", "self"):
                    viol.append({
                        "rule": "A_cross_module_global_mutation", "file": r, "line": node.lineno,
                        "detail": f"assigns {t.value.id}.{t.attr} (module global of another module)",
                    })
    return viol


def rule_dashboard_policy_literals() -> list[dict]:
    """Rule G: the dashboard may not define or mirror trading policy."""
    viol = []
    for p in tracked_python():
        r = rel(p)
        if r not in DASHBOARD_FILES:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            m = UI_POLICY_LITERAL_RE.search(line)
            if m:
                viol.append({
                    "rule": "G_dashboard_policy_literal", "file": r, "line": i,
                    "detail": f"{m.group(1)}={m.group(2)} hardcoded in UI: {line.strip()[:120]}",
                    "canonical_owner": sorted(CANONICAL_POLICY),
                })
    return viol


def rule_unregistered_db_env_vars() -> list[dict]:
    """Rule A/B: only the resolver may read a database-location env var."""
    allowed = {"MULTIHEDGE_DB", "MULTIHEDGE_EVIDENCE_DB", "MULTIHEDGE_LEGACY_DB"}
    viol = []
    for p in tracked_python():
        r = rel(p)
        if r in CANONICAL_PATHS or r in CANONICAL_SETTINGS:
            continue
        if r.startswith(DEBT_PREFIXES):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            for var in allowed:
                if var in line and re.search(r"environ|getenv", line):
                    viol.append({
                        "rule": "A_db_env_var_outside_resolver", "file": r, "line": i,
                        "detail": f"{var} read outside runtime/paths.py: {line.strip()[:120]}",
                    })
    return viol


RULES = {
    "A_direct_db_path_construction": rule_db_path_construction,
    "A_cross_module_global_mutation": rule_cross_module_global_mutation,
    "A_db_env_var_outside_resolver": rule_unregistered_db_env_vars,
    "B_direct_config_reader": rule_direct_config_readers,
    "G_dashboard_policy_literal": rule_dashboard_policy_literals,
}


def main() -> int:
    quiet = "--quiet" in sys.argv
    results = {}
    for name, fn in RULES.items():
        results[name] = fn()

    total = sum(len(v) for v in results.values())
    if quiet:
        for name, viols in results.items():
            for v in viols:
                print(f"{v['rule']} {v['file']}:{v['line']} {v.get('detail','')}")
        return 1 if total else 0

    print("PROJECT ATLAS — SINGLE-TRUTH GATE")
    print(f"  rules={len(RULES)}  violations={total}\n")
    for name, viols in sorted(results.items()):
        files = len({v["file"] for v in viols})
        status = "PASS" if not viols else "FAIL"
        print(f"  [{status}] {name:38} {len(viols):4} violations across {files} files")
    if total:
        print("\n  Canonical modules that must exist to clear these:")
        for path in sorted(CANONICAL_SETTINGS | CANONICAL_PATHS | CANONICAL_POLICY):
            mark = "present" if (REPO / path).exists() else "MISSING"
            print(f"    {mark:8} {path}")
        print("\n  First 20 violations:")
        for name, viols in sorted(results.items()):
            for v in viols[:4]:
                print(f"    {v['file']}:{v['line']}  {v.get('detail','')[:110]}")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
