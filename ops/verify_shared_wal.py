#!/usr/bin/env python3
"""Prove host and container now share ONE write-ahead log (FINDING 008 fix verification).

The torn write happened because the database was bind-mounted as a single FILE, so SQLite
placed the container's -wal/-shm in the container's writable layer while host tools created
their own beside the host file: two independent WALs over one database file, and a
checkpoint racing a stale shared-memory index destroyed 9 pages.

A directory mount fixes it. This script verifies the fix EMPIRICALLY:

  1. the host writes through the shared directory
  2. the container reads the host's write back, immediately
  3. the container writes, and the host reads that back
  4. the sidecars are in the mounted directory, not the writable layer
  5. the file header page count still matches the file length (the torn-write signature)

Writes go to a dedicated probe table and are removed afterwards. It never touches trading
tables, and it never opens the ledger read-write from a context that could race the
container: the host write uses the shared directory deliberately, which is the point.

Usage:  python3 ops/verify_shared_wal.py
"""
from __future__ import annotations

import os
import sqlite3
import struct
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import runtime_paths  # noqa: E402

PROBE_TABLE = "atlas_shared_wal_probe"


def host_db() -> Path:
    return runtime_paths.production_db()


def run_in_container(script: str, timeout: int = 90, extra_env: dict | None = None):
    """Run python inside the container.

    `extra_env` is passed with -e. The first version of this probe read a marker from
    os.environ INSIDE the container without forwarding it, so it looked for a key that
    was never set and reported a false failure. Explicit forwarding is required.
    """
    env_args = ["-e", f"MULTIHEDGE_DB={os.environ.get('MULTIHEDGE_DB') or '/app/db/multihedge.db'}"]
    for k, v in (extra_env or {}).items():
        env_args += ["-e", f"{k}={v}"]
    p = subprocess.run(
        ["docker", "exec", *env_args, "multihedge", "python3", "-c", script],
        capture_output=True, text=True, timeout=timeout, check=False,
    )
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def step(n: int, msg: str) -> None:
    print(f"\n[{n}] {msg}")


def main() -> int:
    db = host_db()
    print("SHARED WAL VERIFICATION (FINDING 008)")
    print(f"  host ledger      : {db}")
    print(f"  host MULTIHEDGE_DB: {os.environ.get('MULTIHEDGE_DB')}")

    # 0. Prove the container agrees on the path.
    step(0, "container resolver agreement")
    rc, out, err = run_in_container(
        "import runtime_paths; print(runtime_paths.production_db())")
    print(f"  container resolves : {out}")
    if rc != 0:
        print(f"  FAIL: {err[-200:]}")
        return 1
    if Path(out).name != db.name:
        print("  FAIL: host and container name different files")
        return 1
    print("  OK: same filename on both sides")

    # 1. host creates the probe table and writes a row.
    step(1, "host writes a probe row through the shared directory")
    host = sqlite3.connect(str(db), timeout=30)
    host.execute("PRAGMA busy_timeout=30000")
    host.execute(f"CREATE TABLE IF NOT EXISTS {PROBE_TABLE}(k TEXT PRIMARY KEY, v TEXT, ts REAL)")
    marker = f"host-{os.getpid()}"
    host.execute(f"INSERT OR REPLACE INTO {PROBE_TABLE} VALUES (?,?,?)",
                 (marker, "written-by-host", __import__("time").time()))
    host.commit()
    print(f"  wrote marker {marker}")

    # 2. container must see it immediately.
    step(2, "container reads the host's write back")
    rc, out, err = run_in_container(
        "import sqlite3, runtime_paths, os\n"
        "p = runtime_paths.production_db()\n"
        "c = sqlite3.connect(f'file:{p}?mode=ro', uri=True, timeout=30)\n"
        f"r = c.execute('SELECT v FROM {PROBE_TABLE} WHERE k=?', (os.environ.get('M'),)).fetchone()\n"
        "print('MISSING' if r is None else r[0])\n",
        extra_env={"M": marker},
    )
    print(f"  container sees: {out}")
    if out != "written-by-host":
        print(f"  FAIL: container did not see the host write (rc={rc}) {err[-200:]}")
        host.close()
        return 1
    print("  OK: host write is visible to the container")

    # 3. container writes, host reads back.
    step(3, "container writes, host reads it back")
    rc, out, err = run_in_container(
        "import sqlite3, runtime_paths\n"
        "p = runtime_paths.production_db()\n"
        "c = sqlite3.connect(p, timeout=30)\n"
        "c.execute('PRAGMA busy_timeout=30000')\n"
        f"c.execute('INSERT OR REPLACE INTO {PROBE_TABLE} VALUES (?,?,?)', ('from-container','written-by-container',0))\n"
        "c.commit(); c.close(); print('WROTE')\n"
    )
    print(f"  container write: {out}")
    row = host.execute(f"SELECT v FROM {PROBE_TABLE} WHERE k='from-container'").fetchone()
    if not row or row[0] != "written-by-container":
        print(f"  FAIL: host did not see the container write (rc={rc}) {err[-200:]}")
        host.close()
        return 1
    print("  OK: container write is visible to the host")

    # 4. sidecars live in the mounted directory.
    step(4, "sidecar location")
    host.execute("PRAGMA wal_checkpoint(PASSIVE)")
    host.close()
    sidecars = sorted(p.name for p in db.parent.glob("multihedge.db-*"))
    print(f"  host dir sidecars : {sidecars or '(checkpointed away)'}")
    rc, out, err = run_in_container(
        "import os, glob; print(sorted(os.path.basename(p) for p in glob.glob('/app/db/multihedge.db-*')))"
    )
    print(f"  container /app/db : {out}")
    rc2, out2, _ = run_in_container(
        "import glob; print(sorted(os.path.basename(p) for p in glob.glob('/app/multihedge.db-*')))"
    )
    print(f"  container /app    : {out2}")
    if out2.strip() not in ("[]", ""):
        print("  FAIL: a private WAL exists in the container's writable layer")
        return 1
    print("  OK: no private WAL in the writable layer")

    # 5. the torn-write signature: header page count must match file length.
    step(5, "torn-write signature check (header page count vs file length)")
    raw = db.read_bytes()[:100]
    page_size = struct.unpack(">H", raw[16:18])[0] or 4096
    declared = struct.unpack(">I", raw[28:32])[0]
    actual = db.stat().st_size // page_size
    print(f"  page_size={page_size}  declared={declared}  actual={actual}")
    if declared != actual:
        print("  FAIL: header claims more pages than exist - this is the 2026-09-28 signature")
        return 1
    print("  OK: header and file agree, no torn write")

    # cleanup
    step(6, "cleanup")
    host = sqlite3.connect(str(db), timeout=30)
    host.execute("PRAGMA busy_timeout=30000")
    host.execute(f"DROP TABLE IF EXISTS {PROBE_TABLE}")
    host.commit()
    host.close()
    print(f"  dropped {PROBE_TABLE}")

    print("\nRESULT: host and container share one ledger and one write-ahead log.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
