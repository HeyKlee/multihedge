#!/usr/bin/env bash

# Entrypoint for the MultiHedge container. Loads .env (JUPITER_API_KEY,
# SOLANA keys) before supervisord starts the daemons. Mirrors AutoHedge's.
set -e
if [ -f /app/.env ]; then
  set -a
  # shellcheck disable=SC1091
  . /app/.env
  set +a
fi

# --- ATLAS Rule A: fail closed on a missing or empty ledger ----------------
#
# The ledger is bind-mounted as a DIRECTORY at /app/db, so the single-file
# literal /app/multihedge.db is an orphan in the container layer. It has
# actually existed as a 0-byte file (observed 2026-09-29 14:02, created after
# the container was recreated, not by the image build).
#
# This matters because a 0-byte SQLite file is a valid, openable database with
# zero tables. Any process that opens it reports "no trades", "no evidence",
# "no history" as if that were a real measurement, and a read-only monitor will
# happily publish an empty history as fact. That is the same failure shape as
# FINDING 001 and FINDING 008, one layer up.
#
# sqlite3.connect() on a missing path CREATES a 0-byte file, so simply
# probing for the file can manufacture the very condition this guards. The
# checks below therefore use stat and the resolver, never a bare connect.
set -u

LEDGER_DIR="/app/db"
LEDGER="${LEDGER_DIR}/multihedge.db"
ORPHAN="/app/multihedge.db"

fail() {
  echo "FATAL: $1" >&2
  echo "Refusing to start. A missing or empty ledger would make every" >&2
  echo "downstream statistic read as zero rather than as a failure." >&2
  exit 1
}

if [ ! -d "$LEDGER_DIR" ]; then
  fail "ledger directory ${LEDGER_DIR} is not present. Is the bind mount missing?"
fi

if [ ! -f "$LEDGER" ]; then
  fail "ledger ${LEDGER} is missing."
fi

LEDGER_BYTES=$(stat -c%s "$LEDGER" 2>/dev/null || echo 0)
if [ "$LEDGER_BYTES" -lt 1024 ]; then
  fail "ledger ${LEDGER} is ${LEDGER_BYTES} bytes. That is not a database."
fi

# The orphan must not exist. If it does, something opened the old literal
# write-capably. Remove it so it cannot shadow anything, and warn loudly
# rather than fail, because the real ledger is already proven good above.
if [ -e "$ORPHAN" ]; then
  echo "WARNING: ${ORPHAN} exists (0-byte orphan at a retired path)." >&2
  echo "         Removing it. Something is still opening the old literal." >&2
  echo "         The real ledger is ${LEDGER} (${LEDGER_BYTES} bytes)." >&2
  rm -f "$ORPHAN"
fi

# Prove the ledger actually opens read-only AND has a table in it. A file can
# be non-empty and still be unusable, and an empty-but-valid database is the
# exact condition being guarded.
if ! python3 - <<'PYCHECK'
import sys, sqlite3
path = "/app/db/multihedge.db"
try:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    row = con.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()
    con.close()
except Exception as exc:
    print(f"ledger will not open read-only: {type(exc).__name__}: {exc}",
          file=sys.stderr)
    sys.exit(1)
if not row or row[0] < 1:
    print("ledger opens but contains no tables", file=sys.stderr)
    sys.exit(1)
print(f"ledger OK: {row[0]} tables at {path}")
PYCHECK
then
  fail "the ledger at ${LEDGER} is present and non-empty but not usable."
fi

exec "$@"
