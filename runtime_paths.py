"""Single runtime-path authority (Project ATLAS Rule A, plan section 9).

Why this module exists
----------------------
Before this, 15+ places independently decided where the production database lives:

    Path(__file__).parent / "multihedge.db"      # relative, resolves by CWD
    os.environ.get("MULTIHEDGE_DB", ...)         # env with a relative default
    os.environ.get("MULTIHEDGE_EVIDENCE_DB", ...)
    os.environ.get("MULTIHEDGE_LEGACY_DB", ...)

Those defaults are RELATIVE, so the same source resolves to different databases depending
on the working directory of whoever imported it. Verified:

    container (cwd=/app)           -> /app/multihedge.db                49 tables, 2218 trades
    host     (cwd=~/multihedge)    -> ~/multihedge/multihedge.db       27 tables, 0 trades

That silent divergence is FINDING 001. It is also what made the 2026-09-28 torn write
possible: `deploy/docker-compose.yml` bind-mounts a single FILE, so SQLite placed the
container's -wal/-shm in the container's writable layer while host tools created their own
beside the host file, giving one database file two independent write-ahead logs.

Design constraints, in priority order
--------------------------------------
1. **Behaviour-preserving migration.** Every path this resolver returns must be identical
   to what the call site returned before, or the migration is wrong. The env default is
   therefore derived from this file's own location, exactly as `Path(__file__).parent`
   was, so an unconfigured caller resolves the same file it always did.

2. **Absolute paths only.** A returned path is always absolute, so a stray `os.chdir`
   cannot silently repoint a module at a different database. This is a behaviour change
   and is the point of the exercise.

3. **Fails loudly, not silently.** An env var that names a non-existent production
   database is a configuration error. It raises rather than falling back, because falling
   back is how an empty database gets created and mistaken for real history.

4. **No I/O at import time.** Resolution is a function call, so tests can point it at a
   temporary database without importing side effects.

Env contract
------------
    MULTIHEDGE_DB            production database (default: <this dir>/multihedge.db)
    MULTIHEDGE_EVIDENCE_DB   evidence database (default: production database)
    MULTIHEDGE_LEGACY_DB     read-only legacy/root copy, for comparison only
"""
from __future__ import annotations

import os
from pathlib import Path

# This file lives in the repository root, which is /app inside the container. Deriving the
# default from here reproduces the historical `Path(__file__).parent` default exactly, so
# the migration changes no resolution until an operator opts in via the environment.
_APP_DIR = Path(__file__).resolve().parent

DEFAULT_DB_NAME = "multihedge.db"

#: Set by tests to redirect resolution at a temporary database.
_override: dict[str, Path] = {}


def _env_path(name: str) -> Path | None:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return None
    return Path(str(raw).strip()).expanduser()


def _resolve(env_name: str, default: Path) -> Path:
    if env_name in _override:
        return Path(_override[env_name]).expanduser().resolve()
    candidate = _env_path(env_name)
    if candidate is None:
        candidate = default
    # A relative value is resolved against the application directory, NEVER the caller's
    # working directory. Path(...).resolve() alone would anchor a relative env value to the
    # cwd, which is the exact defect class this module exists to remove (FINDING 001).
    if not candidate.is_absolute():
        candidate = _APP_DIR / candidate
    return Path(candidate).expanduser().resolve()


def production_db() -> Path:
    """Absolute path to the single production database.

    This is the only sanctioned way to locate it. Callers must not rebuild the path.
    """
    return _resolve("MULTIHEDGE_DB", _APP_DIR / DEFAULT_DB_NAME)


def evidence_db() -> Path:
    """Absolute path to the evidence database.

    Defaults to the production database because every current caller pointed them at the
    same file. Kept as a separate function so the two can diverge deliberately later,
    with one place to change.
    """
    return _resolve("MULTIHEDGE_EVIDENCE_DB", production_db())


def legacy_db() -> Path:
    """Absolute path to the read-only legacy copy, used only for comparison.

    Never a write target. The dashboard reads it to show historical divergence.
    """
    return _resolve("MULTIHEDGE_LEGACY_DB", _APP_DIR / DEFAULT_DB_NAME)


def connect_readonly(path: Path | None = None, timeout: float = 5.0):
    """Open the production database READ-ONLY.

    Mandatory helper: a host-side read-write open is what races the container's WAL. Every
    host-side tool, report and analysis must use this rather than sqlite3.connect(path).
    """
    import sqlite3

    target = Path(path) if path is not None else production_db()
    return sqlite3.connect(f"file:{target}?mode=ro", uri=True, timeout=timeout)


def connect(path: Path | None = None, timeout: float = 15.0):
    """Open a database read-write. For in-container runtime writers ONLY.

    Guarded so that a host-side caller is visible: a host process must not write the
    production database while the container owns the write-ahead log.
    """
    import sqlite3

    target = Path(path) if path is not None else production_db()
    app_dir = str(_APP_DIR)
    if not str(target).startswith(app_dir) and not os.environ.get("MULTIHEDGE_ALLOW_HOST_WRITE"):
        raise PermissionError(
            f"refusing read-write open of {target}: writers must run inside the application "
            f"directory ({app_dir}) or set MULTIHEDGE_ALLOW_HOST_WRITE explicitly. A "
            "host-side write races the container's -wal and caused the 2026-09-28 torn write."
        )
    return sqlite3.connect(target, timeout=timeout)


# --- test support -------------------------------------------------------------------
# Tests may redirect resolution without touching the real environment or the real ledger.

def set_override(env_name: str, path: Path | str | None) -> None:
    """Point an env key at a specific path. `None` clears it. Test use only."""
    if path is None:
        _override.pop(env_name, None)
    else:
        _override[env_name] = Path(path)


def clear_overrides() -> None:
    _override.clear()


def describe() -> dict:
    """Human-readable resolution report, for the runtime manifest and for debugging."""
    return {
        "app_dir": str(_APP_DIR),
        "production_db": str(production_db()),
        "production_db_env": os.environ.get("MULTIHEDGE_DB"),
        "evidence_db": str(evidence_db()),
        "evidence_db_env": os.environ.get("MULTIHEDGE_EVIDENCE_DB"),
        "legacy_db": str(legacy_db()),
        "overrides_active": dict(_override),
    }
