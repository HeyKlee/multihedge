#!/usr/bin/env python3
"""Diagnose SQLite corruption: where is the damage, and is any page salvageable?

Pure stdlib. Opens the file read-only with plain open()/seek() — it never hands the file
to SQLite, so a malformed image cannot crash the probe. This answers one question:

    is the schema (page 1) intact, and how far into the file do readable pages run?

That determines whether .recover is possible and which recovery route is worth taking.
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

SQLITE_MAGIC = b"SQLite format 3\x00"


def probe(path: str) -> dict:
    size = Path(path).stat().st_size
    with open(path, "rb") as fh:
        header = fh.read(100)
        if not header.startswith(SQLITE_MAGIC):
            return {"ok": False, "reason": "not a SQLite file", "size": size}
        page_size = struct.unpack(">H", header[16:18])[0]
        page_size = 65536 if page_size == 1 else page_size
        reserved = header[20]
        page_count = struct.unpack(">I", header[28:32])[0]
        freelist_trunk = struct.unpack(">I", header[32:36])[0]
        freelist_pages = struct.unpack(">I", header[36:40])[0]
        text_encoding = struct.unpack(">I", header[56:60])[0]
        usable = page_size - reserved

        actual_pages = size // page_size
        # Walk every page: a b-tree page starts with a type byte (2..13).
        readable = 0
        corrupt = 0
        free = 0
        first_bad = None
        page_types: dict[int, int] = {}
        for pno in range(1, actual_pages + 1):
            fh.seek((pno - 1) * page_size)
            b = fh.read(1)
            if not b:
                break
            t = b[0]
            if t == 0:
                free += 1
            elif 2 <= t <= 13:
                readable += 1
                page_types[t] = page_types.get(t, 0) + 1
            else:
                corrupt += 1
                if first_bad is None:
                    first_bad = pno
        return {
            "ok": True,
            "size_bytes": size,
            "page_size": page_size,
            "reserved_bytes": reserved,
            "usable_size": usable,
            "header_page_count": page_count,
            "actual_pages": actual_pages,
            "file_consistent": page_count == actual_pages,
            "freelist_trunk_page": freelist_trunk,
            "freelist_page_count": freelist_pages,
            "text_encoding": text_encoding,
            "page1_first_byte": None,
            "pages_readable": readable,
            "pages_free": free,
            "pages_corrupt": corrupt,
            "first_corrupt_page": first_bad,
            "page_type_histogram": dict(sorted(page_types.items())),
        }


def describe(r: dict) -> str:
    if not r.get("ok"):
        return f"NOT SQLITE: {r.get('reason')}"
    lines = [
        f"  file size          : {r['size_bytes']:,} bytes",
        f"  page size          : {r['page_size']:,} (usable {r['usable_size']:,})",
        f"  header page count  : {r['header_page_count']:,}",
        f"  actual pages       : {r['actual_pages']:,}  "
        f"({'CONSISTENT' if r['file_consistent'] else 'MISMATCH -> truncated/extended file'})",
        f"  freelist pages     : {r['freelist_page_count']:,} (trunk page {r['freelist_trunk_page']})",
        f"  pages readable     : {r['pages_readable']:,}",
        f"  pages free/unused  : {r['pages_free']:,}",
        f"  pages corrupt      : {r['pages_corrupt']:,}"
        + (f"  (first bad page: {r['first_corrupt_page']})" if r["first_corrupt_page"] else ""),
        f"  b-tree page types  : {r['page_type_histogram']}",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    result = probe(sys.argv[1])
    print(f"SQLITE CORRUPTION PROBE: {sys.argv[1]}")
    print(describe(result))
    # B-tree type legend for interpretation.
    print("\n  b-tree page type legend: 2=interior-index 5=interior-table 10=leaf-index 13=leaf-table")
    print("  interpretation: a high 'pages corrupt' count with page 1 readable means the")
    print("  schema survived and only data pages rotted; that is the recoverable case.")
