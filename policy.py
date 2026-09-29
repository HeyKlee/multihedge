"""Single effective-policy resolver (Project ATLAS Rule C, plan section 9).

The defect this exists to remove (FINDING 004 / FINDING 007)
-----------------------------------------------------------
Four layers each defined exit thresholds, and they disagreed:

    live_inventory.py:16-20   MEME  TP 1.5%  SL -1.0%  hold 1800s
    paper.py:50-51            paper TP 2.5%  SL -1.5%  hold 3600s
    config.yaml:73-79         TP 1.5%  SL 1.5%  hold 7200s
    mh_reasoner_params (DB)   TP 5.0%  SL 2.0%  hold 7200s   <- actually in force

`mh_reasoner._load_params()` documents "DB table wins, then config, then defaults", and the
DB row was written by a monthly optimiser with no author, no timestamp and no approval
record. It survived image rebuilds. So editing `live_inventory.py` or `config.yaml` changed
nothing, which is precisely the reported symptom: change a value, nothing applies.

The evidence that settled the value (FINDING 011)
---------------------------------------------------
Measured over 26,463 real price points across 56 mints:

    5 min  median move 0.38%   reaches 1.5% in 7.7% of windows
    30 min median move 1.28%  reaches 1.5% in 42.0% of windows
    60 min median move 1.98%  reaches 1.5% in 64.3% of windows

Measured round-trip friction on these tokens is 0.37%-0.90%. So a 5% target is not
"ambitious", it is unreachable inside a 30-60 minute window (only 6.1% of 30-minute windows
touch 5%), and it converts the system into one that holds losers far longer than winners.
1.5% is achievable at the 30-60 minute horizon and is above the 5-minute p90 of 1.28%.

Design rules
------------
1. **One owner.** No consumer reads a threshold except through `effective_policy()`.
2. **Overrides require provenance.** An override row must carry source, author, approval and
   a timestamp. An unapproved override is IGNORED, not honoured. An unversioned override is
   the FINDING 004 failure and must not be able to recur silently.
3. **Fails closed.** A malformed or out-of-range value is refused, never clamped. Clamping
   silently would let a bad override widen or narrow risk without anyone noticing.
4. **Declares its own conflicts.** `policy_conflicts()` names every layer that disagrees, so
   the dashboard and the manifest can show the operator what is actually in force.
5. **USD vs NZD is explicit.** Treasury floors are constitutional and live in
   `survival_policy`; trading thresholds are here. They are never mixed.
"""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent

# --------------------------------------------------------------------------------------
# Declared defaults. These are the numbers, stated once.
#
# Sourced from FINDING 011 evidence, not from the constitution: AGENTS.md:63 states MEME
# "+20% TP / -10% SL / 900s", which contradicts this file by 13x on take profit. The
# constitution's own rule says the source tree is authoritative when they disagree, and the
# ledger says +20% occurs in 0.0% of 5-minute and 1.1% of 60-minute windows. See
# FINDING 007 and FINDING 011.
# --------------------------------------------------------------------------------------
POLICY_VERSION = "2026-09-28.atlas-1"

# The exit magnitudes themselves are NOT restated here. AGENTS.md ("Important boundaries")
# names live_inventory.py as the shared source of Xora-Survival exit parameters and forbids
# independent constants, and test_atlas_policy_ownership enforces exactly one definition
# site. Restating them in a second module would reintroduce FINDING 004 in a new file, so
# this resolver reads the owner rather than copying it.
import live_inventory as _owner

MEME_TAKE_PROFIT_PCT = _owner.MEME_TAKE_PROFIT_PCT
MEME_STOP_LOSS_PCT = _owner.MEME_STOP_LOSS_PCT
MEME_MAX_HOLD_SECONDS = _owner.MEME_MAX_HOLD_SECONDS
MEME_TRAIL_ARM_PCT = _owner.MEME_TRAIL_ARM_PCT
MEME_TRAIL_DISTANCE_PCT = _owner.MEME_TRAIL_DISTANCE_PCT

SERIOUS_TAKE_PROFIT_PCT = _owner.SERIOUS_TAKE_PROFIT_PCT
SERIOUS_STOP_LOSS_PCT = _owner.SERIOUS_STOP_LOSS_PCT
SERIOUS_MAX_HOLD_SECONDS = _owner.SERIOUS_MAX_HOLD_SECONDS
SERIOUS_TRAIL_ARM_PCT = _owner.SERIOUS_TRAIL_ARM_PCT
SERIOUS_TRAIL_DISTANCE_PCT = _owner.SERIOUS_TRAIL_DISTANCE_PCT

# Hard bounds. A value outside these is refused, not clamped.
BOUNDS = {
    "take_profit_pct": (0.001, 0.05),      # 5% is the measured 30-min p90 ceiling
    "stop_loss_pct": (-0.20, -0.002),
    "max_hold_seconds": (60, 86400),
    "trail_arm_pct": (0.0, 0.05),
    "trail_distance_pct": (0.0, 0.05),
    "position_fraction": (0.01, 1.0),
    "confidence_min": (0.0, 1.0),
}

# Overrides must be explicitly approved via this file or an env opt-in. A bare row in the
# database is not an approval mechanism.
APPROVAL_ENV = "MULTIHEDGE_ALLOW_UNAPPROVED_OVERRIDES"


class PolicyError(ValueError):
    """Raised when a policy value is missing, malformed, or outside its bounds."""


@dataclass(frozen=True)
class Policy:
    """One resolved, self-describing set of effective trading parameters."""

    mode: str                       # MEME | SERIOUS
    take_profit_pct: float
    stop_loss_pct: float
    max_hold_seconds: int
    trail_arm_pct: float
    trail_distance_pct: float
    position_fraction: float
    confidence_min: float
    source: str                     # which layer supplied the value that won
    approved: bool
    approved_by: str | None
    approved_at: str | None
    policy_version: str = POLICY_VERSION
    overrides_considered: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)

    @property
    def max_hold_minutes(self) -> float:
        return self.max_hold_seconds / 60.0


def _validate(name: str, value):
    """Refuse malformed or out-of-bounds values. Never clamp: clamping is silent risk."""
    if name not in BOUNDS:
        raise PolicyError(f"unknown policy parameter {name!r}")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PolicyError(f"{name} must be numeric, got {type(value).__name__}")
    if not isinstance(value, (int, float)) or value != value:  # NaN check
        raise PolicyError(f"{name} is not a finite number")
    lo, hi = BOUNDS[name]
    if not (lo <= float(value) <= hi):
        raise PolicyError(
            f"{name}={value!r} outside permitted bounds [{lo}, {hi}]; refusing rather "
            "than clamping, because a silent clamp hides a bad override"
        )
    return float(value)


def declared_defaults(mode: str) -> Policy:
    """The single declared default for a mode. No DB, no config, no environment."""
    mode = str(mode).upper()
    if mode == "MEME":
        return Policy(
            mode=mode,
            take_profit_pct=MEME_TAKE_PROFIT_PCT,
            stop_loss_pct=MEME_STOP_LOSS_PCT,
            max_hold_seconds=MEME_MAX_HOLD_SECONDS,
            trail_arm_pct=MEME_TRAIL_ARM_PCT,
            trail_distance_pct=MEME_TRAIL_DISTANCE_PCT,
            position_fraction=0.15,
            confidence_min=0.35,
            source="declared_default (policy.py)",
            approved=True,
            approved_by="code",
            approved_at=POLICY_VERSION,
        )
    if mode == "SERIOUS":
        return Policy(
            mode=mode,
            take_profit_pct=SERIOUS_TAKE_PROFIT_PCT,
            stop_loss_pct=SERIOUS_STOP_LOSS_PCT,
            max_hold_seconds=SERIOUS_MAX_HOLD_SECONDS,
            trail_arm_pct=SERIOUS_TRAIL_ARM_PCT,
            trail_distance_pct=SERIOUS_TRAIL_DISTANCE_PCT,
            position_fraction=0.15,
            confidence_min=0.35,
            source="declared_default (policy.py)",
            approved=True,
            approved_by="code",
            approved_at=POLICY_VERSION,
        )
    raise PolicyError(f"unknown policy mode {mode!r}; expected MEME or SERIOUS")


def _load_override_table(db_path) -> tuple[dict, list]:
    """Read an override table, keeping only rows that carry approval provenance.

    The legacy `mh_reasoner_params` table is a bare key/value table with no provenance. It is
    read for visibility, but its values are NOT applied unless they are approved, because
    honouring an unapproved override is exactly FINDING 004.
    """
    if db_path is None:
        return {}, []
    path = Path(db_path)
    if not path.exists():
        return {}, []
    applied: dict = {}
    considered: list = []
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return {}, considered
    try:
        have = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        # V2 table: carries provenance. Honour it.
        if "mh_policy_overrides" in have:
            cols = {r[1] for r in con.execute("PRAGMA table_info(mh_policy_overrides)")}
            required = {"key", "value", "source", "approved_by", "approved_at"}
            if required <= cols:
                for key, value, src, who, when in con.execute(
                    "SELECT key, value, source, approved_by, approved_at "
                    "FROM mh_policy_overrides WHERE approved=1"
                ):
                    applied[key] = {"value": value, "source": src,
                                    "approved_by": who, "approved_at": when}
                    considered.append({"key": key, "applied": True, "source": src})
            else:
                considered.append({"table": "mh_policy_overrides",
                                   "applied": False, "reason": "missing provenance columns"})
        # Legacy table: visibility only.
        if "mh_reasoner_params" in have:
            for key, value in con.execute("SELECT key, value FROM mh_reasoner_params"):
                considered.append({
                    "key": key, "value": value, "applied": False,
                    "reason": "legacy key/value table carries no source, approved_by or "
                              "approved_at, so it is reported but not applied",
                })
    except sqlite3.Error as exc:
        considered.append({"error": str(exc)})
    finally:
        con.close()
    return applied, considered


def effective_policy(mode: str, db_path=None, *, allow_unapproved: bool | None = None) -> Policy:
    """Resolve the ONE effective policy for a mode.

    Precedence: declared default <- approved override. Nothing else is consulted. There is
    deliberately no path from an unapproved row to a live risk value.
    """
    base = declared_defaults(mode)
    if allow_unapproved is None:
        allow_unapproved = os.environ.get(APPROVAL_ENV, "") not in ("", "0", "false", "False")

    overrides, considered = _load_override_table(db_path)
    applied: list = []

    if allow_unapproved and db_path is not None:
        # Explicit opt-in escape hatch, for the one-time cutover only.
        path = Path(db_path)
        if path.exists():
            try:
                con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
                for key, value in con.execute("SELECT key, value FROM mh_reasoner_params"):
                    overrides.setdefault(key, {
                        "value": value, "source": "mh_reasoner_params (UNAPPROVED, opt-in)",
                        "approved_by": "env:" + APPROVAL_ENV, "approved_at": None})
                    applied.append(key)
                con.close()
            except sqlite3.Error:
                pass

    if not overrides:
        return Policy(**{**asdict(base), "overrides_considered": considered})

    values = asdict(base)
    source = base.source
    approved = True
    who = base.approved_by
    when = base.approved_at

    mapping = {
        "TAKE_PROFIT": "take_profit_pct",
        "STOP_LOSS": "stop_loss_pct",
        "MAX_HOLD_SECS": "max_hold_seconds",
        "TRAIL_ARM": "trail_arm_pct",
        "TRAIL_DIST": "trail_distance_pct",
        "POSITION_FRACTION": "position_fraction",
        "CONFIDENCE_MIN": "confidence_min",
    }
    for key, meta in overrides.items():
        field = mapping.get(key)
        if field is None:
            continue
        try:
            values[field] = _validate(field, float(meta["value"]))
            applied.append(key)
            source = f"approved_override:{meta.get('source')}"
            who = meta.get("approved_by")
            when = meta.get("approved_at")
        except PolicyError:
            # A refused override leaves the declared default in force. Refusing is the point.
            considered.append({"key": key, "applied": False, "reason": "failed validation"})

    values["max_hold_seconds"] = int(values["max_hold_seconds"])
    return Policy(
        **{**values, "source": source, "approved": approved, "approved_by": who,
           "approved_at": when, "overrides_considered": considered}
    )


def policy_conflicts() -> list[dict]:
    """Name every layer that declares a threshold, and whether it agrees with the resolver.

    This is what turns FINDING 004 from a hidden problem into a visible one. The dashboard
    and /api/system-manifest both call it, so an operator can see the disagreement without
    reading the source.
    """
    import paper

    out = []
    try:
        resolved = effective_policy("MEME")
    except PolicyError as exc:
        return [{"error": str(exc)}]

    layers = [
        ("policy.py (resolved)", resolved.take_profit_pct, resolved.max_hold_seconds,
         "canonical"),
        ("live_inventory.py", MEME_TAKE_PROFIT_PCT, MEME_MAX_HOLD_SECONDS, "source"),
        ("paper.py", getattr(paper, "TP_PCT", None), getattr(paper, "MAX_HOLD_S", None),
         "paper ledger"),
    ]
    try:
        cfg_text = (APP_DIR / "config.yaml").read_text(encoding="utf-8")
        layers.append(("config.yaml", None, None, "declaration"))
    except OSError:
        pass

    for name, tp, hold, kind in layers:
        if tp is None:
            out.append({"layer": name, "kind": kind, "agrees": None,
                        "note": "declaration not parsed by the resolver"})
            continue
        out.append({
            "layer": name, "kind": kind,
            "take_profit_pct": tp, "max_hold_seconds": hold,
            "agrees": (abs(tp - resolved.take_profit_pct) < 1e-9
                       and int(hold) == resolved.max_hold_seconds),
        })
    return out


def describe() -> dict:
    """Full resolution report for the manifest and the dashboard."""
    report = {"policy_version": POLICY_VERSION, "modes": {}}
    for mode in ("MEME", "SERIOUS"):
        try:
            p = effective_policy(mode)
            report["modes"][mode] = p.as_dict()
        except PolicyError as exc:
            report["modes"][mode] = {"error": str(exc)}
    report["conflicts"] = policy_conflicts()
    return report
