#!/usr/bin/env python3
"""Project ATLAS repository scanner.

Generates the machine-readable inventory that "100% mapped" is measured against.
The denominator is generated from the exact Git commit under review, so coverage
cannot be inflated by hand-editing a document.

Read-only with respect to tracked source. Reads Git, the filesystem, and SQLite
schemas. Never writes to any database.

Usage:
    python3 tools/atlas_scan.py            # human summary + writes docs/atlas/ATLAS_SCAN.json
    python3 tools/atlas_scan.py --json     # JSON only
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCAN_OUT = REPO / "docs" / "atlas" / "ATLAS_SCAN.json"

# Files that are generated, cached, or third-party rather than authored source.
GENERATED_DIRS = {
    "backtest-results", "optimization-results", "autotuner-logs", "graphify-out",
    "reports", "dataset", "price_history", "trade_records", "__pycache__",
    ".pytest_cache", ".venv", "entry-quality-results", "audit", "local-ai-voice-web",
    "incident-20260925-walsplit", ".git",
}
GENERATED_SUFFIXES = {".pyc", ".log", ".db", ".db-wal", ".db-shm", ".bak"}
BACKUP_RE = re.compile(r"\.bak(\.\d+)?$|\.bak-|\.pyc$")

# Directories that hold authored vs generated python.
TEST_RE = re.compile(r"(^|/)test_[^/]*\.py$|(^|/)tests?/")
OPS_RE = re.compile(r"(^|/)ops/")
LEGACY_RE = re.compile(r"(^|/)legacy/")

DB_LITERAL_RE = re.compile(r"multihedge\.db")
DB_PATH_RE = re.compile(r"\b(DB_PATH|DB_FILE|DATABASE|EVIDENCE_DB|AUDIT_DB)\b")
SQLITE_RE = re.compile(r"sqlite3\.connect\s*\(")
ENV_RE = re.compile(r"os\.environ(?:\.get)?\s*[(\[]\s*[\"']?([A-Z0-9_]+)")
GETENV_RE = re.compile(r"os\.getenv\s*\(\s*[\"']([A-Z0-9_]+)[\"']")
HTTP_RE = re.compile(r"\b(httpx|requests|aiohttp|urllib\.request|websocket)\b")
MODEL_RE = re.compile(r"(openrouter|xkiro|gpt-|claude|deepseek|qwen|gemini|llama|mistral)[/\w.:-]*")
FALLBACK_RE = re.compile(r"or\s+[\"'][^\"']*\.db[\"']|getenv\([^)]*\)[\s,)]*or\b")


def git(*args: str) -> str:
    out = subprocess.run(("git",) + args, cwd=REPO, capture_output=True, text=True)
    return out.stdout.strip()


def tracked_files() -> list[str]:
    return [p for p in git("ls-files").splitlines() if p]


def sha256(path: Path) -> str | None:
    import hashlib
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def classify(rel: str) -> str:
    """Single authoritative classification per tracked file."""
    parts = rel.split("/")
    top = parts[0]
    name = parts[-1]
    if top in GENERATED_DIRS:
        return "generated_artifact"
    if BACKUP_RE.search(name):
        return "backup_copy"
    if LEGACY_RE.search(rel):
        return "legacy"
    if OPS_RE.search(rel):
        return "ops_script"
    if TEST_RE.search(rel):
        return "test"
    if name.endswith((".md", ".txt", ".rst")):
        return "docs"
    if name in ("config.yaml", "requirements.txt", "Dockerfile") or name.startswith("Dockerfile"):
        return "config"
    if name.endswith((".yml", ".yaml", ".json", ".ini", ".conf", ".toml", ".sh")):
        return "config_or_data"
    if name.endswith(".py"):
        return "source_module"
    if name.endswith((".csv", ".db", ".sqlite")):
        return "data"
    return "other"


def parse_python(rel: str, text: str) -> dict:
    """Static facts about one python file. Never executes the file."""
    out = {
        "imports_local": [], "broken_imports": [], "env_vars": [], "db_path_sites": [],
        "sqlite_connects": [], "config_reads": [], "http_calls": [], "model_ids": [],
        "fallback_db_defaults": [], "is_entrypoint": False, "entrypoint_reason": None,
        "sql_statements": 0, "insert_statements": 0,
    }
    try:
        tree = ast.parse(text, filename=rel)
    except SyntaxError as exc:
        out["parse_error"] = f"{exc.lineno}: {exc.msg}"
        return out

    for node in ast.walk(tree):
        # imports
        if isinstance(node, ast.Import):
            for a in node.names:
                out["imports_local"].append({"module": a.name, "line": node.lineno})
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            out["imports_local"].append({"module": mod, "line": node.lineno, "level": node.level})
        # os.environ / os.getenv
        elif isinstance(node, ast.Call):
            fn = node.func
            fname = getattr(fn, "attr", None) or getattr(fn, "id", None)
            base = getattr(getattr(fn, "value", None), "id", None)
            if fname in ("getenv", "get"):
                if base == "os" and fname == "getenv" and node.args:
                    if isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                        out["env_vars"].append({"name": node.args[0].value, "line": node.lineno})
            if fname == "connect" and base in ("sqlite3", "db"):
                out["sqlite_connects"].append({"line": node.lineno})
            if fname in ("get", "post", "request", "Client", "AsyncClient"):
                if base in ("httpx", "requests", "aiohttp", "ur") or fname in ("get", "post", "request"):
                    out["http_calls"].append({"line": node.lineno, "fn": fname})
        elif isinstance(node, ast.Subscript):
            base = getattr(node.value, "id", None)
            if base == "os":
                sl = node.slice
                if isinstance(sl, ast.Constant) and isinstance(sl.value, str):
                    out["env_vars"].append({"name": sl.value, "line": node.lineno})
        # entrypoint
        if isinstance(node, ast.If):
            test = ast.dump(node.test)
            if "__name__" in test and "__main__" in test:
                out["is_entrypoint"] = True
                out["entrypoint_reason"] = "__main__ guard"
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "main":
                out["is_entrypoint"] = True
                out["entrypoint_reason"] = out["entrypoint_reason"] or "main() defined"

    # regex facts (cheap, catches things AST misses: strings, f-strings, fallbacks)
    for i, line in enumerate(text.splitlines(), 1):
        if DB_LITERAL_RE.search(line) or DB_PATH_RE.search(line):
            if "sqlite3.connect" in line or "DB_PATH" in line or "multihedge.db" in line:
                out["db_path_sites"].append({"line": i, "text": line.strip()[:160]})
        if SQLITE_RE.search(line):
            pass  # already captured by AST
        if re.search(r"config\.yaml|['\"]config['\"]", line) and re.search(r"open\(|safe_load|load\(", line):
            out["config_reads"].append({"line": i, "text": line.strip()[:160]})
        for m in ENV_RE.finditer(line):
            out["env_vars"].append({"name": m.group(1), "line": i})
        for m in GETENV_RE.finditer(line):
            out["env_vars"].append({"name": m.group(1), "line": i})
        for m in MODEL_RE.finditer(line):
            out["model_ids"].append({"id": m.group(0), "line": i})
        if FALLBACK_RE.search(line) and DB_LITERAL_RE.search(line):
            out["fallback_db_defaults"].append({"line": i, "text": line.strip()[:160]})
        if re.search(r"\b(INSERT|REPLACE)\s+INTO\b", line, re.I):
            out["insert_statements"] += 1
        if re.search(r"\b(SELECT|UPDATE|DELETE|CREATE)\b", line, re.I):
            out["sql_statements"] += 1

    # de-duplicate env vars per (name, line)
    seen = set()
    dedup = []
    for e in out["env_vars"]:
        k = (e["name"], e["line"])
        if k not in seen:
            seen.add(k)
            dedup.append(e)
    out["env_vars"] = dedup
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    files = tracked_files()
    py_files = [f for f in files if f.endswith(".py")]

    classes = defaultdict(list)
    for f in files:
        classes[classify(f)].append(f)

    modules = {}
    for rel in py_files:
        p = REPO / rel
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            modules[rel] = {"read_error": str(exc)}
            continue
        modules[rel] = parse_python(rel, text)

    # resolve which imports are local modules
    module_names = {Path(f).stem for f in py_files}
    for rel, info in modules.items():
        local, broken = [], []
        for imp in info.get("imports_local", []):
            mod = imp["module"].split(".")[0]
            if not mod:
                continue
            if mod in module_names and Path(mod).resolve() != Path(rel).stem:
                local.append({"module": mod, "line": imp["line"]})
        info["imports_local"] = local
        info.pop("broken_imports", None)

    # aggregate facts
    db_path_sites, config_reads, env_vars, sqlite_connects = [], [], [], []
    entrypoints, http_calls, model_ids, fallback_defaults = [], [], [], []
    for rel, info in modules.items():
        if info.get("parse_error"):
            continue
        for d in info.get("db_path_sites", []):
            db_path_sites.append({"file": rel, **d})
        for d in info.get("config_reads", []):
            config_reads.append({"file": rel, **d})
        for d in info.get("env_vars", []):
            env_vars.append({"file": rel, **d})
        for d in info.get("sqlite_connects", []):
            sqlite_connects.append({"file": rel, "line": d["line"]})
        for d in info.get("http_calls", []):
            http_calls.append({"file": rel, **d})
        for d in info.get("model_ids", []):
            model_ids.append({"file": rel, **d})
        for d in info.get("fallback_db_defaults", []):
            fallback_defaults.append({"file": rel, **d})
        if info.get("is_entrypoint"):
            entrypoints.append({"file": rel, "reason": info["entrypoint_reason"]})

    env_names = sorted({e["name"] for e in env_vars})

    result = {
        "baseline": {
            "git_sha": git("rev-parse", "HEAD"),
            "branch": git("branch", "--show-current"),
            "git_status_porcelain_lines": len(git("status", "--porcelain").splitlines()),
            "tracked_file_count": len(files),
            "config_sha256": sha256(REPO / "config.yaml"),
        },
        "files": {
            "total": len(files),
            "by_classification": {k: len(v) for k, v in sorted(classes.items())},
            "classified_members": {k: v for k, v in sorted(classes.items())},
        },
        "entrypoints": entrypoints,
        "db_path_sites": db_path_sites,
        "sqlite_connects": sqlite_connects,
        "config_yaml_readers": config_reads,
        "env_var_names": env_names,
        "env_var_sites": env_vars,
        "http_call_sites": http_calls,
        "model_id_sites": model_ids,
        "fallback_db_default_sites": fallback_defaults,
        "python_modules": {rel: {k: v for k, v in info.items() if k != "imports_local"}
                           for rel, info in modules.items()},
    }

    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    SCAN_OUT.parent.mkdir(parents=True, exist_ok=True)
    SCAN_OUT.write_text(json.dumps(result, indent=2))
    b = result["baseline"]
    print("PROJECT ATLAS SCAN")
    print(f"  git_sha   {b['git_sha'][:12]}  branch={b['branch']}")
    print(f"  tracked   {b['tracked_file_count']} files, dirty_lines={b['git_status_porcelain_lines']}")
    print(f"  config    {b['config_sha256'][:16]}")
    print("\n  classification:")
    for k, v in result["files"]["by_classification"].items():
        print(f"    {k:22} {v}")
    print(f"\n  entrypoints            {len(entrypoints)}")
    print(f"  db path sites          {len(db_path_sites)}")
    print(f"  sqlite connects        {len(sqlite_connects)}")
    print(f"  config.yaml readers    {len(config_reads)}")
    print(f"  env var names          {len(env_names)}")
    print(f"  fallback db defaults   {len(fallback_defaults)}")
    print(f"\n  written: {SCAN_OUT.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
