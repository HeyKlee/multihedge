#!/usr/bin/env python3
"""Where does SQLite put -wal/-shm when the database is a SYMLINK?

This decides the cheapest safe fix for the torn-write defect (FINDING 008).

The defect: docker-compose bind-mounts a single FILE (./data/multihedge.db ->
/app/multihedge.db). SQLite derives the -wal and -shm names from the path it was GIVEN,
so the container writes /app/multihedge.db-wal (writable layer) while any host tool
writing deploy/data/multihedge.db writes deploy/data/multihedge.db-wal. Two independent
WALs, one database file -> torn write.

If SQLite follows the symlink when placing sidecars, the fix is two lines: mount the
directory and symlink the path every module already uses. If it does NOT, the only correct
fix is to mount the directory AND move every resolution site to one resolver.

This script answers that empirically rather than by assumption. It is self-contained,
creates only /tmp paths, and deletes them on exit.
"""
import os
import shutil
import sqlite3
import sys

BASE = "/tmp/wal_placement_probe"


def sidecars_near(db_path: str) -> tuple[str, str]:
    return db_path + "-wal", db_path + "-shm"


def run_case(label: str, open_path: str, target_dir: str) -> dict:
    for suffix in ("", "-wal", "-shm"):
        p = open_path + suffix
        if os.path.exists(p):
            os.remove(p)

    con = sqlite3.connect(open_path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE IF NOT EXISTS t(x)")
    con.execute("INSERT INTO t VALUES (1)")
    con.commit()
    # Hold a second connection so the WAL is not checkpointed away on close.
    holder = sqlite3.connect(open_path)
    holder.execute("SELECT COUNT(*) FROM t").fetchone()

    own_wal = os.path.exists(open_path + "-wal")
    own_shm = os.path.exists(open_path + "-shm")
    tgt_wal = os.path.exists(os.path.join(target_dir, os.path.basename(open_path) + "-wal"))
    tgt_shm = os.path.exists(os.path.join(target_dir, os.path.basename(open_path) + "-shm"))

    holder.close()
    con.close()
    for suffix in ("", "-wal", "-shm"):
        p = open_path + suffix
        if os.path.exists(p):
            os.remove(p)

    return {
        "case": label,
        "opened_as": open_path,
        "sidecars_beside_opened_path": own_wal and own_shm,
        "sidecars_beside_target": tgt_wal and tgt_shm,
        "verdict": "FOLLOWS_SYMLINK" if (tgt_wal or tgt_shm) else "USES_GIVEN_PATH",
    }


def main() -> int:
    shutil.rmtree(BASE, ignore_errors=True)
    os.makedirs(os.path.join(BASE, "data"), exist_ok=True)

    # Seed a real database inside the "shared directory".
    real = os.path.join(BASE, "data", "multihedge.db")
    seed = sqlite3.connect(real)
    seed.execute("PRAGMA journal_mode=WAL")
    seed.execute("CREATE TABLE t(x)")
    seed.commit()
    seed.close()

    results = []
    # Case 1: plain file opened directly inside the shared dir.
    shutil.copy2(real, os.path.join(BASE, "data", "direct.db"))
    results.append(run_case("direct file in shared dir",
                            os.path.join(BASE, "data", "direct.db"),
                            os.path.join(BASE, "data")))

    # Case 2: symlink in a DIFFERENT directory pointing at the shared dir file.
    link = os.path.join(BASE, "linked.db")
    os.symlink(real, link)
    results.append(run_case("symlink in another dir", link, os.path.join(BASE, "data")))

    print("SQLite sidecar placement probe")
    print(f"  sqlite library: {sqlite3.sqlite_version}\n")
    for r in results:
        print(f"  {r['case']}")
        print(f"    opened as            : {r['opened_as']}")
        print(f"    beside opened path   : {r['sidecars_beside_opened_path']}")
        print(f"    beside symlink target: {r['sidecars_beside_target']}")
        print(f"    verdict              : {r['verdict']}\n")

    follows = any(r["verdict"] == "FOLLOWS_SYMLINK" for r in results)
    print("CONCLUSION")
    # Only the symlink case is evidence. In the "direct file" case the opened path and the
    # target directory are the SAME directory, so both checks see the same two files and
    # the result is degenerate — it must not be counted as evidence of following a symlink.
    symlink_case = results[-1]
    follows = symlink_case["verdict"] == "FOLLOWS_SYMLINK"
    if follows:
        print("  SQLite DOES place -wal/-shm beside the symlink target.")
        print("  => a directory mount plus a symlink would share sidecars, and every")
        print("     existing resolution site keeps working unchanged.")
    else:
        print("  SQLite does NOT follow symlinks for sidecar placement: it derives the")
        print("  -wal/-shm names from the path it was GIVEN, not from the resolved target.")
        print("  => a symlink shim would REPRODUCE the split and is not a fix. The only")
        print("     correct fix is to bind-mount the shared DIRECTORY and move every")
        print("     resolution site onto a single resolver (runtime/paths.py, ATLAS Rule A).")

    shutil.rmtree(BASE, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
