#!/usr/bin/env python3
"""Migrate runtime DB-path call sites onto runtime_paths (Project ATLAS Rule A).

Behaviour-preserving by construction: each site's replacement returns the same file the
original expression resolved to, which test_runtime_paths asserts independently. This
script refuses to touch a file whose line does not match exactly what it expects, so an
upstream edit cannot be silently clobbered.

Idempotent: a site already migrated is skipped.

Usage:  python3 ops/migrate_db_paths.py [--apply] [--dry-run]
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# (file, exact original line, replacement) - exact match required
SITES: list[tuple[str, str, str]] = [
    ("paper.py",
     'DB_PATH = Path(__file__).parent / "multihedge.db"',
     'DB_PATH = runtime_paths.production_db()  # ATLAS Rule A: single path authority'),
    ("grid_trader.py",
     'DB_PATH = Path(__file__).parent / "multihedge.db"',
     'DB_PATH = runtime_paths.production_db()  # ATLAS Rule A: single path authority'),
    ("mh_memecoin_trader.py",
     'DB_PATH = Path(__file__).parent / "multihedge.db"',
     'DB_PATH = runtime_paths.production_db()  # ATLAS Rule A: single path authority'),
    ("mh_news.py",
     'DB_PATH = CUR_DIR / "multihedge.db"',
     'DB_PATH = runtime_paths.production_db()  # ATLAS Rule A: single path authority'),
    ("mh_reasoner.py",
     'DB_PATH = CUR_DIR / "multihedge.db"',
     'DB_PATH = runtime_paths.production_db()  # ATLAS Rule A: single path authority'),
    ("track_whales.py",
     'DB_PATH = Path(__file__).parent / "multihedge.db"',
     'DB_PATH = runtime_paths.production_db()  # ATLAS Rule A: single path authority'),
    ("mh_whale_trader.py",
     'DB_PATH = Path(__file__).parent / "multihedge.db"',
     'DB_PATH = runtime_paths.production_db()  # ATLAS Rule A: single path authority'),
    ("live_bridge.py",
     'DB_PATH = Path(__file__).parent / "multihedge.db"',
     'DB_PATH = runtime_paths.production_db()  # ATLAS Rule A: single path authority'),
    ("pricefeed.py",
     'DB_PATH = Path(__file__).parent / "multihedge.db"',
     'DB_PATH = runtime_paths.production_db()  # ATLAS Rule A: single path authority'),
    ("strategy.py",
     'DB_PATH = Path(__file__).parent / "multihedge.db"',
     'DB_PATH = runtime_paths.production_db()  # ATLAS Rule A: single path authority'),
    # pump_monitor inlines the path at three call sites
    ("pump_monitor.py",
     'con = sqlite3.connect(Path(__file__).parent / "multihedge.db", check_same_thread=False, timeout=30)',
     'con = sqlite3.connect(runtime_paths.production_db(), check_same_thread=False, timeout=30)'),
]

IMPORT_LINE = "import runtime_paths  # ATLAS Rule A: sole runtime path authority"

# Where to insert the import: after the last top-level import, before the first non-import.
IMPORT_RE = re.compile(r"^(import |from )")


def ensure_import(text: str) -> str:
    """Insert the runtime_paths import after the LAST top-level import.

    Uses AST end_lineno rather than a textual `^import` scan: a naive scan splits
    multi-line imports such as `from execution_costs import (\n ... \n)`, which is a
    syntax error. This was a real bug in the first version of this script.
    """
    if "import runtime_paths" in text:
        return text
    import ast

    tree = ast.parse(text)
    insert_at = 0
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            insert_at = max(insert_at, node.end_lineno or node.lineno)
    if insert_at == 0:
        # No top-level imports: place it after any module docstring.
        if tree.body and isinstance(tree.body[0], ast.Expr) and isinstance(
            getattr(tree.body[0], "value", None), ast.Constant
        ):
            insert_at = tree.body[0].end_lineno or 1
    lines = text.splitlines(keepends=True)
    # Skip a single blank line that already separates the import block.
    while insert_at < len(lines) and lines[insert_at].strip() == "":
        insert_at += 1
    lines.insert(insert_at, IMPORT_LINE + "\n")
    out = "".join(lines)
    ast.parse(out)  # fail loudly here rather than at deploy time
    return out


def migrate(apply: bool) -> int:
    changed = skipped = failed = 0
    for filename, original, replacement in SITES:
        path = REPO / filename
        if not path.is_file():
            print(f"  MISSING  {filename}")
            failed += 1
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if replacement in text:
            print(f"  already  {filename}")
            skipped += 1
            continue
        count = text.count(original)
        if count == 0:
            print(f"  NOMATCH  {filename}: {original[:60]!r} not found verbatim")
            failed += 1
            continue
        new = text.replace(original, replacement)
        new = ensure_import(new)
        print(f"  {'patched' if apply else 'would patch'} {filename} ({count} site(s))")
        if apply:
            path.write_text(new, encoding="utf-8")
        changed += 1
    print(f"\n  changed={changed} already={skipped} failed={failed}")
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes (default is a dry run)")
    args = ap.parse_args()
    print(f"DB path migration ({'APPLY' if args.apply else 'DRY RUN'})")
    return migrate(args.apply)


if __name__ == "__main__":
    sys.exit(main())
