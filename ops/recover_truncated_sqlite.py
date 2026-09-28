#!/usr/bin/env python3
"""Salvage a truncated SQLite file by reconciling the header page count with the file.

Root cause this addresses: a torn/short write leaves sqlite_master.page_count larger than
the number of pages actually present. SQLite refuses to open such a file ("database disk
image is malformed") even though every page on disk is a valid b-tree page. Reconciling
the header to the real page count restores readability without touching page content.

Safe by construction:
  - the source file is never opened for writing
  - the patched copy is written to a separate path
  - a byte-level diff proves only the 4-byte page_count field changed
  - every table is row-counted after recovery so losses are explicit, never assumed

Usage:
    python3 ops/recover_truncated_sqlite.py <corrupt.db> <recovered.db> [--report out.json]
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import struct
import sys
from pathlib import Path

PAGE_COUNT_OFFSET = 28


def reconcile(src: str, dst: str) -> dict:
    size = os.path.getsize(src)
    with open(src, "rb") as fh:
        header = fh.read(100)
    if not header.startswith(b"SQLite format 3\x00"):
        raise SystemExit("not a SQLite database")
    page_size = struct.unpack(">H", header[16:18])[0]
    page_size = 65536 if page_size == 1 else page_size
    declared = struct.unpack(">I", header[PAGE_COUNT_OFFSET: PAGE_COUNT_OFFSET + 4])[0]
    actual = size // page_size

    shutil.copy2(src, dst)
    patched = bytearray(header)
    struct.pack_into(">I", patched, PAGE_COUNT_OFFSET, actual)
    with open(dst, "r+b") as fh:
        fh.seek(0)
        fh.write(patched)

    return {
        "source": src,
        "output": dst,
        "page_size": page_size,
        "declared_pages": declared,
        "actual_pages": actual,
        "missing_pages": declared - actual,
        "bytes_missing": (declared - actual) * page_size,
        "truncated": declared > actual,
    }


def verify_only_page_count_changed(src: str, dst: str) -> dict:
    """Prove the patch touched nothing but the page_count field.

    The 4-byte big-endian page_count can legitimately change in only some of its bytes
    (e.g. 10561 -> 10552 differs only in the low byte), so the correct assertion is that
    every differing offset falls INSIDE the page_count field, not that all four differ.
    """
    size = os.path.getsize(src)
    differing = []
    with open(src, "rb") as a, open(dst, "rb") as b:
        offset = 0
        while offset < size:
            chunk = 4096
            ca, cb = a.read(chunk), b.read(chunk)
            if ca != cb:
                for i in range(min(len(ca), len(cb))):
                    if ca[i] != cb[i]:
                        differing.append(offset + i)
            offset += chunk
    field = set(range(PAGE_COUNT_OFFSET, PAGE_COUNT_OFFSET + 4))
    outside = [o for o in differing if o not in field]
    with open(src, "rb") as fh:
        before = struct.unpack(">I", fh.read(100)[PAGE_COUNT_OFFSET:PAGE_COUNT_OFFSET + 4])[0]
    with open(dst, "rb") as fh:
        after = struct.unpack(">I", fh.read(100)[PAGE_COUNT_OFFSET:PAGE_COUNT_OFFSET + 4])[0]
    return {
        "differing_byte_offsets": differing,
        "differing_bytes_are_subset_of_page_count": not outside,
        "unexpected_offsets": outside,
        "page_count_before": before,
        "page_count_after": after,
        "page_count_corrected": after == os.path.getsize(src) // (
            struct.unpack(">H", open(src, "rb").read(100)[16:18])[0] or 4096
        ),
    }


def inventory(db: str) -> dict:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=15)
    info: dict = {"integrity": None, "quick_check": None, "tables": {}, "errors": []}
    try:
        try:
            info["integrity"] = con.execute("PRAGMA integrity_check(20)").fetchall()
        except sqlite3.Error as exc:
            info["errors"].append(f"integrity_check: {exc}")
        try:
            info["quick_check"] = con.execute("PRAGMA quick_check").fetchall()[:5]
        except sqlite3.Error as exc:
            info["errors"].append(f"quick_check: {exc}")

        names = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        for name in names:
            try:
                n = con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
                info["tables"][name] = {"rows": n, "readable": True}
            except sqlite3.Error as exc:
                info["tables"][name] = {"rows": None, "readable": False, "error": str(exc)}
    finally:
        con.close()
    return info


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    src, dst = sys.argv[1], sys.argv[2]
    report_path = None
    if "--report" in sys.argv:
        report_path = sys.argv[sys.argv.index("--report") + 1]

    patch_info = reconcile(src, dst)
    print("RECONCILE")
    for k, v in patch_info.items():
        print(f"  {k:20} {v}")

    diff = verify_only_page_count_changed(src, dst)
    print("\nPATCH SAFETY")
    print(f"  differing bytes            {diff['differing_byte_offsets']}")
    print(f"  all inside page_count fld  {diff['differing_bytes_are_subset_of_page_count']}")
    print(f"  unexpected offsets         {diff['unexpected_offsets']}")
    print(f"  page_count {diff['page_count_before']} -> {diff['page_count_after']} "
          f"(corrected={diff['page_count_corrected']})")
    if not diff["differing_bytes_are_subset_of_page_count"] or not diff["page_count_corrected"]:
        print("  ABORT: patch touched unexpected bytes or did not correct page_count")
        return 1

    inv = inventory(dst)
    print("\nINVENTORY")
    print(f"  quick_check : {inv['quick_check']}")
    print(f"  integrity   : {inv['integrity']}")
    unreadable = [t for t, v in inv["tables"].items() if not v["readable"]]
    total = sum(v["rows"] for v in inv["tables"].values() if v["readable"])
    print(f"  tables      : {len(inv['tables'])} total, {len(unreadable)} unreadable")
    print(f"  rows        : {total:,}")
    for name, meta in sorted(inv["tables"].items(), key=lambda kv: -(kv[1]["rows"] or 0))[:20]:
        r = meta["rows"]
        print(f"    {name:44} {r if r is not None else 'UNREADABLE':>10}")

    if report_path:
        Path(report_path).write_text(json.dumps(
            {"patch": patch_info, "safety": diff, "inventory": inv}, indent=2))
        print(f"\nreport: {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
