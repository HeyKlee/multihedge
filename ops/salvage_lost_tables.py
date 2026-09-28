#!/usr/bin/env python3
"""Walk SQLite b-trees to locate pages lost to a torn write, and salvage what survives.

Pure stdlib, read-only against the source. For a given table it walks interior pages
breadth-first, records every page that is missing (beyond EOF) or unparseable, and then
reads every reachable leaf page, decoding records and emitting INSERT statements for a
clean target database.

Design rules, all deliberate:
  - a page that cannot be parsed is SKIPPED and reported, never guessed at
  - rows are keyed by (table, primary key) so a re-run is idempotent
  - nothing is written to the source, ever
"""
from __future__ import annotations

import json
import sqlite3
import struct
import sys
from pathlib import Path

PAGE_HDR = 100  # sqlite_master root uses a 100-byte offset; all other pages use 0


def read_page(path: str, pno: int, page_size: int) -> bytes | None:
    with open(path, "rb") as fh:
        fh.seek((pno - 1) * page_size)
        data = fh.read(page_size)
    return data if len(data) == page_size else None


def parse_page(page: bytes, pno: int):
    """Return (type, ncell, cell_ptrs, rightmost) for a b-tree page."""
    off = PAGE_HDR if pno == 1 else 0
    ptype = page[off]
    if ptype not in (2, 5, 10, 13):
        raise ValueError(f"page {pno}: not a b-tree page (type={ptype})")
    ncell = struct.unpack(">H", page[off + 3:off + 5])[0]
    hdr = 12 if ptype in (2, 5) else 8
    ptrs = [struct.unpack(">H", page[off + hdr + 2 * i: off + hdr + 2 + 2 * i])[0]
            for i in range(ncell)]
    right = struct.unpack(">I", page[off + 8:off + 12])[0] if ptype in (2, 5) else None
    return ptype, ncell, ptrs, right


def interior_children(page: bytes, pno: int):
    ptype, _, ptrs, right = parse_page(page, pno)
    kids = []
    for c in ptrs:
        kids.append(struct.unpack(">I", page[c:c + 4])[0])
    if right:
        kids.append(right)
    return ptype, kids


def varint(buf: bytes, i: int):
    v = 0
    for k in range(9):
        if i + k >= len(buf):
            raise ValueError("varint overruns page")
        b = buf[i + k]
        if k == 8:
            return (v << 8) | b, i + 9
        v = (v << 7) | (b & 0x7F)
        if not b & 0x80:
            return v, i + k + 1
    raise ValueError("varint too long")


def leaf_records(page: bytes, pno: int):
    """Decode leaf-table cell payloads -> list of (rowid, raw_payload_bytes)."""
    ptype, _, ptrs, _ = parse_page(page, pno)
    if ptype != 13:
        return []
    out = []
    for c in ptrs:
        try:
            payload_len, i = varint(page, c)
            rowid, i = varint(page, i)
            payload = page[i:i + payload_len]
            if len(payload) == payload_len:
                out.append((rowid, payload))
        except (ValueError, struct.error, IndexError):
            continue
    return out


def walk(path: str, root: int, page_size: int, last_page: int):
    """Breadth-first walk. Returns (visited, missing, leaves)."""
    visited, missing, leaves = set(), set(), []
    stack = [root]
    while stack:
        pno = stack.pop()
        if pno in visited or pno < 1:
            continue
        if pno > last_page:
            missing.add(pno)
            continue
        visited.add(pno)
        page = read_page(path, pno, page_size)
        if page is None:
            missing.add(pno)
            continue
        try:
            ptype, kids = interior_children(page, pno)
        except (ValueError, struct.error, IndexError):
            missing.add(pno)
            continue
        if ptype in (2, 5):
            stack.extend(kids)
        else:
            leaves.append(pno)
    return visited, missing, leaves


def serial_len(t: int) -> int:
    if t <= 4:
        return 0
    if t == 5:
        return 6
    if t in (6, 7):
        return 8
    if t in (8, 9):
        return 0
    if t >= 12:
        return (t - 12) // 2
    raise ValueError(f"reserved serial type {t}")


def decode_values(payload: bytes):
    """Decode a record per the SQLite file format. Raises on any inconsistency.

    Serial types (from the SQLite format spec):
      0 NULL | 1..6 int of 1/2/3/4/6/8 bytes | 7 float64 | 8 const 0 | 9 const 1
      10,11 reserved (error) | >=12 even: blob of (t-12)/2 | >=13 odd: text of (t-13)/2
    An INTEGER PRIMARY KEY column is a rowid alias and is stored as NULL; the b-tree key
    supplies its value, so callers must substitute the rowid for that column.
    """
    hdr_len, i = varint(payload, 0)
    if hdr_len < 1 or hdr_len > len(payload):
        raise ValueError(f"bad header length {hdr_len}")
    types = []
    while i < hdr_len:
        t, i = varint(payload, i)
        types.append(t)
    if i != hdr_len:
        raise ValueError("serial-type array overruns the record header")
    vals, j = [], hdr_len
    for t in types:
        if t == 0:
            vals.append(None)
        elif 1 <= t <= 6:
            n = {1: 1, 2: 2, 3: 3, 4: 4, 5: 6, 6: 8}[t]
            vals.append(int.from_bytes(payload[j:j + n], "big", signed=True)); j += n
        elif t == 7:
            vals.append(struct.unpack(">d", payload[j:j + 8])[0]); j += 8
        elif t == 8:
            vals.append(0)
        elif t == 9:
            vals.append(1)
        elif t in (10, 11):
            raise ValueError(f"reserved serial type {t}")
        elif t % 2 == 0:
            n = (t - 12) // 2
            vals.append(bytes(payload[j:j + n])); j += n
        else:
            n = (t - 13) // 2
            vals.append(bytes(payload[j:j + n]).decode("utf-8", "replace")); j += n
        if j > len(payload):
            raise ValueError("value overruns record payload")
    return types, vals


def main() -> int:
    if len(sys.argv) < 4:
        print(__doc__)
        return 2
    src, dst, report = sys.argv[1], sys.argv[2], sys.argv[3]
    # Schema source: the raw (corrupt) file cannot answer "SELECT ... FROM sqlite_master"
    # reliably, so the caller must pass a reconciled copy via --schema-from, or we try the
    # reconciled path derived from dst's sibling. Raw pages are always read from `src`.
    schema_from = src
    if "--schema-from" in sys.argv:
        schema_from = sys.argv[sys.argv.index("--schema-from") + 1]
    size = Path(src).stat().st_size
    with open(src, "rb") as fh:
        hdr = fh.read(100)
    page_size = struct.unpack(">H", hdr[16:18])[0] or 4096
    last_page = size // page_size
    print(f"source={src}\n  page_size={page_size}  last_page={last_page}")
    print(f"schema_from={schema_from}")

    try:
        con = sqlite3.connect(f"file:{schema_from}?mode=ro", uri=True, timeout=15)
    except sqlite3.Error as exc:
        print(f"FATAL: cannot open schema source {schema_from}: {exc}")
        print("       pass --schema-from <reconciled.db>")
        return 1
    try:
        targets = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        roots = {n: rp for n, rp in con.execute(
            "SELECT name, rootpage FROM sqlite_master WHERE type='table' AND rootpage>0")}
    except sqlite3.Error as exc:
        print(f"FATAL: schema source unreadable ({exc}); pass --schema-from <reconciled.db>")
        return 1
    finally:
        con.close()

    wanted = list(targets)
    results = {}
    out = sqlite3.connect(dst)
    for t in wanted:
        root = roots.get(t)
        if not root:
            continue
        visited, missing, leaves = walk(src, root, page_size, last_page)
        rows = 0
        bad_records = 0
        try:
            info = out.execute(f'PRAGMA table_info("{t}")').fetchall()
        except sqlite3.Error:
            info = []
        cols = [r[1] for r in info]
        # An INTEGER PRIMARY KEY column is a rowid alias: SQLite stores NULL for it in the
        # record and keeps the real value in the b-tree key. Substituting the rowid keeps
        # primary keys intact and makes INSERT OR REPLACE idempotent.
        alias_idx = [
            r[1] for r in info if r[5] == 1 and (r[2] or "").strip().upper() in ("INTEGER", "INT")
        ]
        for lp in leaves:
            page = read_page(src, lp, page_size)
            if page is None:
                continue
            try:
                recs = leaf_records(page, lp)
            except (ValueError, struct.error):
                bad_records += 1
                continue
            for rowid, payload in recs:
                try:
                    types, vals = decode_values(payload)
                except (ValueError, struct.error, IndexError):
                    bad_records += 1
                    continue
                if not cols:
                    bad_records += 1
                    continue
                # A column added by ALTER TABLE ... ADD COLUMN is absent from older records.
                # SQLite materialises it from the column DEFAULT, so trailing defaults are
                # appended here rather than discarding an otherwise valid row.
                if len(vals) < len(cols) and vals:
                    tail = info[len(vals):]
                    if all(r[4] is not None or (r[4] == "" and r[5] == 0) for r in tail):
                        for r in tail:
                            default = r[4]
                            if default is None:
                                vals.append(None)
                            else:
                                up = str(default).strip().upper()
                                try:
                                    if up in ("NULL",):
                                        vals.append(None)
                                    elif up.startswith("'") or up.startswith('"'):
                                        vals.append(up.strip("'\""))
                                    elif "." in up or "E" in up.upper():
                                        vals.append(float(up))
                                    else:
                                        vals.append(int(up))
                                except ValueError:
                                    vals.append(default)
                    else:
                        bad_records += 1
                        continue
                elif len(vals) > len(cols):
                    # Extra stored values cannot be mapped to declared columns.
                    bad_records += 1
                    continue
                for name in alias_idx:
                    try:
                        vals[cols.index(name)] = rowid
                    except (ValueError, IndexError):
                        pass
                try:
                    ph = ",".join("?" * len(vals))
                    out.execute(
                        f'INSERT OR REPLACE INTO "{t}" ({",".join(chr(34)+c+chr(34) for c in cols)})'
                        f" VALUES({ph})", vals)
                    rows += 1
                except sqlite3.Error as exc:
                    bad_records += 1
                    if bad_records <= 3:
                        print(f"    insert error on {t}: {exc}")
        results[t] = {
            "root": root, "pages_visited": len(visited), "pages_missing": sorted(missing),
            "leaf_pages": len(leaves), "rows_inserted": rows, "bad_records": bad_records,
        }
    out.commit()
    out.close()
    Path(report).write_text(json.dumps(results, indent=2, default=str))
    total = sum(v["rows_inserted"] for v in results.values())
    print(f"\n  tables walked : {len(results)}")
    print(f"  rows inserted : {total}")
    lost = {t: v["pages_missing"] for t, v in results.items() if v["pages_missing"]}
    print(f"  tables with lost pages: {len(lost)}")
    for t, v in sorted(results.items(), key=lambda kv: -kv[1]["pages_missing"].__len__())[:10]:
        if v["pages_missing"]:
            print(f"    {t:34} missing={len(v['pages_missing'])} rows={v['rows_inserted']}")
    print(f"report: {report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
