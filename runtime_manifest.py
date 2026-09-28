"""Runtime identity manifest (Project ATLAS, plan section 13).

Answers the question that a health check cannot: *which code, config, schema and policy
are actually running right now?*

A container that is up, whose processes are RUNNING, and whose endpoints answer 200 is
compatible with running stale code. Those are health signals, not identity signals. This
module produces the identity half.

Design rules, in order of importance:
  1. Never raise. A manifest endpoint that 500s during an incident is worse than useless,
     so every probe degrades to an explicit "unavailable" value with a reason.
  2. No secrets. Only names, hashes, counts and versions.
  3. Read-only. Every database access is mode=ro. This module must never open a
     production database read-write; a host-side write racing the container's WAL is
     exactly what destroyed 9 pages on 2026-09-28.
  4. Cheap. It is called by humans during incidents and by deploy gates, so it does not
     scan table contents or hash large files on every call.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
from pathlib import Path

import runtime_paths  # ATLAS Rule A: sole runtime path authority
APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.yaml"
DB_PATH = runtime_paths.production_db()  # ATLAS Rule A: do not resolve independently


def _sha256_file(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _git_sha() -> str | None:
    """Prefer a build-stamped SHA; fall back to git only if present and safe."""
    for env in ("MULTIHEDGE_GIT_SHA", "GIT_SHA", "BUILD_SHA"):
        value = os.environ.get(env)
        if value:
            return value.strip()
    try:
        proc = subprocess.run(
            ["git", "-C", str(APP_DIR), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _image_id() -> str | None:
    # /etc/hostname is the container id; /proc/self/cgroup may carry the image digest.
    try:
        cid = Path("/etc/hostname").read_text(encoding="utf-8").strip()
        return cid or None
    except OSError:
        return None


def _db_identity() -> dict:
    """Read-only identity for the runtime database. Never opens read-write."""
    out: dict = {"path": str(DB_PATH), "exists": DB_PATH.exists()}
    if not out["exists"]:
        out["error"] = "database file not present at the runtime path"
        return out
    try:
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error as exc:
        out["error"] = f"cannot open read-only: {exc}"
        return out
    try:
        out["user_version"] = con.execute("PRAGMA user_version").fetchone()[0]
        out["table_count"] = con.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
        out["integrity"] = con.execute("PRAGMA quick_check").fetchone()[0]
        # Canonical trade count: the single most load-bearing fact in the system.
        try:
            out["mh_trades_rows"] = con.execute("SELECT COUNT(*) FROM mh_trades").fetchone()[0]
        except sqlite3.Error as exc:
            out["mh_trades_rows"] = None
            out["mh_trades_error"] = str(exc)
        # Effective policy override, so the manifest reports what is IN FORCE, not what
        # any source file claims (ATLAS FINDING 004).
        try:
            out["reasoner_params"] = dict(con.execute("SELECT key, value FROM mh_reasoner_params"))
        except sqlite3.Error:
            out["reasoner_params"] = None
    except sqlite3.Error as exc:
        out["error"] = str(exc)
    finally:
        con.close()
    return out


def _declared_exit_policy() -> dict:
    """The exit thresholds as written in the canonical source module.

    Reported ALONGSIDE the live override so a mismatch is visible rather than hidden.
    Import is done defensively; absence must not break the manifest.
    """
    try:
        import live_inventory as li
    except Exception as exc:  # noqa: BLE001 - manifest must never raise
        return {"error": f"{type(exc).__name__}: {exc}"}
    return {
        "owner_module": "live_inventory.py",
        "MEME_take_profit_pct": getattr(li, "MEME_TAKE_PROFIT_PCT", None),
        "MEME_stop_loss_pct": getattr(li, "MEME_STOP_LOSS_PCT", None),
        "MEME_max_hold_seconds": getattr(li, "MEME_MAX_HOLD_SECONDS", None),
        "SERIOUS_take_profit_pct": getattr(li, "SERIOUS_TAKE_PROFIT_PCT", None),
        "SERIOUS_stop_loss_pct": getattr(li, "SERIOUS_STOP_LOSS_PCT", None),
        "SERIOUS_max_hold_seconds": getattr(li, "SERIOUS_MAX_HOLD_SECONDS", None),
    }


def _friction_limit() -> dict:
    """The entry-cost gate threshold actually in force, read from config.

    Deliberately does NOT re-derive precedence: it reports the declared value and names
    its source, so a reader can see the number AND where it came from.
    """
    try:
        import yaml
        cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
        dyn = cfg.get("live", {}).get("autonomous", {}).get("dynamic_universe", {})
        return {
            "maximum_friction_cost_pct": dyn.get("maximum_friction_cost_pct"),
            "source": "config.yaml:live.autonomous.dynamic_universe",
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def build_manifest() -> dict:
    """Assemble the manifest. Every section is individually failure-tolerant."""
    manifest: dict = {
        "generated_at": None,
        "identity": {
            "git_sha": _git_sha(),
            "container_id": _image_id(),
            "app_dir": str(APP_DIR),
            "hostname": os.uname().nodename if hasattr(os, "uname") else None,
        },
        "config": {
            "path": str(CONFIG_PATH),
            "exists": CONFIG_PATH.exists(),
            "sha256": _sha256_file(CONFIG_PATH) if CONFIG_PATH.exists() else None,
        },
        "database": _db_identity(),
        "declared_exit_policy": _declared_exit_policy(),
        "entry_friction_gate": _friction_limit(),
    }
    try:
        from datetime import datetime, timezone
        manifest["generated_at"] = datetime.now(timezone.utc).isoformat()
    except Exception:  # noqa: BLE001
        manifest["generated_at"] = "unavailable"

    # The one thing a human checks first during an incident: does source match runtime?
    manifest["deploy_freshness"] = _deploy_freshness()
    return manifest


def _deploy_freshness() -> dict:
    """Compare the deployed image's own source against the git checkout, if present.

    This is the check whose absence produced FINDING 005 (a fix committed but not live).
    It compares the running file's hash to the working-tree file at the same path.
    """
    try:
        from pathlib import Path as _P
        import subprocess as _sp
        root = _P(__file__).resolve().parent
        proc = _sp.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                       capture_output=True, text=True, timeout=5, check=False)
        if proc.returncode != 0:
            return {"status": "unavailable", "reason": "not a git checkout (expected in the image)"}
        head = proc.stdout.strip()
        return {
            "status": "checked",
            "git_head": head,
            "note": "container images bake source at build time; compare with the host "
                    "working tree to detect drift, since git metadata is absent in the image",
        }
    except Exception as exc:  # noqa: BLE001
        return {"status": "unavailable", "reason": f"{type(exc).__name__}: {exc}"}
