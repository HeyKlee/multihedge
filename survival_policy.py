"""Deterministic, fail-closed controls for XORA-SURVIVAL.

This module never signs or submits transactions. It is the policy boundary used
by shadow-mode development until a separately reviewed signer is available.
"""

from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
import time

PROTECTED_FLOOR_NZD = Decimal("60.00")
DEATH_THRESHOLD_NZD = Decimal("40.00")
MAX_TOTAL_POSITION_FRACTION = Decimal("0.05")
MAX_RISK_POSITION_FRACTION = Decimal("0.20")
MAX_TOKEN_RISK_FRACTION = Decimal("0.15")
MAX_AGGREGATE_RISK_FRACTION = Decimal("0.40")


@dataclass(frozen=True)
class SurvivalState:
    state: str
    treasury_nzd: Decimal | None
    protected_reserve_nzd: Decimal
    risk_capital_nzd: Decimal


def classify_survival_state(
    treasury_nzd: Decimal | None,
    *,
    data_verified: bool,
    shadow_only: bool,
    independent_below_death_checks: int,
) -> SurvivalState:
    """Classify state without treating missing data as a zero balance."""
    if treasury_nzd is None or not data_verified:
        return SurvivalState("UNKNOWN", None, PROTECTED_FLOOR_NZD, Decimal("0.00"))
    if treasury_nzd < 0:
        raise ValueError("treasury_nzd cannot be negative")

    reserve = min(treasury_nzd, PROTECTED_FLOOR_NZD)
    risk_capital = max(Decimal("0.00"), treasury_nzd - PROTECTED_FLOOR_NZD)

    if shadow_only:
        state = "SHADOW_ONLY"
    elif treasury_nzd < DEATH_THRESHOLD_NZD:
        state = "DEAD" if independent_below_death_checks >= 2 else "UNKNOWN"
    elif treasury_nzd < PROTECTED_FLOOR_NZD:
        state = "DISTRESS"
    elif treasury_nzd < Decimal("70.00"):
        state = "CONSERVATION"
    elif treasury_nzd < Decimal("90.00"):
        state = "NORMAL"
    else:
        state = "GROWTH"

    return SurvivalState(state, treasury_nzd, reserve, risk_capital)


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str
    max_position_nzd: Decimal
    signing_authorized: bool = False


def max_new_position_nzd(
    state: SurvivalState,
    *,
    current_token_exposure_nzd: Decimal,
    aggregate_exposure_nzd: Decimal,
) -> Decimal:
    """Return the most restrictive constitutional position headroom."""
    if state.treasury_nzd is None or state.state not in {"GROWTH", "NORMAL"}:
        return Decimal("0.00")
    if current_token_exposure_nzd < 0 or aggregate_exposure_nzd < 0:
        raise ValueError("exposures cannot be negative")

    limits = (
        state.treasury_nzd * MAX_TOTAL_POSITION_FRACTION,
        state.risk_capital_nzd * MAX_RISK_POSITION_FRACTION,
        state.risk_capital_nzd * MAX_TOKEN_RISK_FRACTION - current_token_exposure_nzd,
        state.risk_capital_nzd * MAX_AGGREGATE_RISK_FRACTION - aggregate_exposure_nzd,
    )
    return max(Decimal("0.00"), min(limits)).quantize(Decimal("0.01"))


def evaluate_new_position(
    state: SurvivalState,
    *,
    proposed_position_nzd: Decimal,
    current_token_exposure_nzd: Decimal,
    aggregate_exposure_nzd: Decimal,
    expected_net_reward_nzd: Decimal,
    expected_loss_nzd: Decimal,
    daily_loss_nzd: Decimal,
    data_fresh: bool,
    controls_healthy: bool,
) -> PolicyDecision:
    """Evaluate a proposal while intentionally withholding signing authority."""
    cap = max_new_position_nzd(
        state,
        current_token_exposure_nzd=current_token_exposure_nzd,
        aggregate_exposure_nzd=aggregate_exposure_nzd,
    )
    if not data_fresh or not controls_healthy or state.treasury_nzd is None:
        return PolicyDecision(False, "STALE_OR_UNVERIFIED_STATE", cap)
    if state.state not in {"GROWTH", "NORMAL"}:
        return PolicyDecision(False, "SURVIVAL_STATE_BLOCKS_NEW_RISK", cap)
    if proposed_position_nzd <= 0 or proposed_position_nzd > cap:
        return PolicyDecision(False, "POSITION_LIMIT_EXCEEDED", cap)

    daily_loss_cap = min(state.treasury_nzd * Decimal("0.05"), Decimal("3.00"))
    if daily_loss_nzd >= daily_loss_cap:
        return PolicyDecision(False, "DAILY_LOSS_LIMIT_REACHED", cap)
    if expected_loss_nzd <= 0 or expected_net_reward_nzd < expected_loss_nzd * 2:
        return PolicyDecision(False, "INSUFFICIENT_NET_REWARD_TO_RISK", cap)
    return PolicyDecision(True, "POLICY_CHECKS_PASS_ADVISORY_ONLY", cap)


CYCLE_FIELDS = frozenset(
    {
        "state",
        "treasury_nzd",
        "protected_reserve_nzd",
        "risk_capital_nzd",
        "data_freshness",
        "highest_value_goal",
        "planned_actions",
        "policy_checks",
        "actions_completed",
        "external_readbacks",
        "cost_nzd",
        "revenue_nzd",
        "pnl_nzd",
        "risk_change",
        "n8n_changes",
        "messages_sent",
        "failures",
        "next_wake_reason",
        "verdict",
    }
)
CYCLE_VERDICTS = frozenset(
    {
        "CYCLE: VERIFIED VALUE CREATED",
        "CYCLE: SAFE PROGRESS, NO REVENUE YET",
        "CYCLE: NO SAFE ACTION, SLEEPING",
        "CYCLE: HUMAN ACTION REQUIRED",
        "CYCLE: CONSERVATION MODE",
        "CYCLE: DISTRESS MODE",
        "CYCLE: DEAD, SPEND LOCKED",
        "CYCLE: UNKNOWN STATE, FAIL CLOSED",
    }
)


class AuditStore:
    """Append-only, hash-chained SQLite storage for action-cycle records."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            con.execute(
                "CREATE TABLE IF NOT EXISTS survival_cycles ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, "
                "payload TEXT NOT NULL, previous_hash TEXT NOT NULL, "
                "record_hash TEXT NOT NULL UNIQUE)"
            )
            con.execute(
                "CREATE TRIGGER IF NOT EXISTS survival_cycles_no_update "
                "BEFORE UPDATE ON survival_cycles BEGIN "
                "SELECT RAISE(ABORT, 'append-only audit record'); END"
            )
            con.execute(
                "CREATE TRIGGER IF NOT EXISTS survival_cycles_no_delete "
                "BEFORE DELETE ON survival_cycles BEGIN "
                "SELECT RAISE(ABORT, 'append-only audit record'); END"
            )

    def _connect(self):
        return sqlite3.connect(self.path)

    def append_cycle(self, cycle: dict) -> int:
        missing = CYCLE_FIELDS.difference(cycle)
        extra = set(cycle).difference(CYCLE_FIELDS)
        if missing or extra:
            raise ValueError(f"invalid cycle fields: missing={sorted(missing)}, extra={sorted(extra)}")
        if cycle["verdict"] not in CYCLE_VERDICTS:
            raise ValueError("invalid cycle verdict")
        payload = json.dumps(cycle, sort_keys=True, separators=(",", ":"), allow_nan=False)
        timestamp = time.time()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            rows = con.execute(
                "SELECT id,ts,payload,previous_hash,record_hash "
                "FROM survival_cycles ORDER BY id"
            ).fetchall()
            if not self._rows_valid(rows):
                raise RuntimeError("audit chain verification failed; refusing append")
            previous_hash = rows[-1][4] if rows else "GENESIS"
            digest_input = f"{timestamp:.9f}|{previous_hash}|{payload}".encode("utf-8")
            record_hash = hashlib.sha256(digest_input).hexdigest()
            cursor = con.execute(
                "INSERT INTO survival_cycles(ts,payload,previous_hash,record_hash) "
                "VALUES(?,?,?,?)",
                (timestamp, payload, previous_hash, record_hash),
            )
            return int(cursor.lastrowid)

    def get_cycle(self, cycle_id: int) -> dict:
        with self._connect() as con:
            row = con.execute(
                "SELECT payload FROM survival_cycles WHERE id=?", (cycle_id,)
            ).fetchone()
        if row is None:
            raise KeyError(cycle_id)
        return json.loads(row[0])

    @staticmethod
    def _rows_valid(rows) -> bool:
        expected_id = 1
        previous_hash = "GENESIS"
        for cycle_id, timestamp, payload, stored_previous, stored_hash in rows:
            if cycle_id != expected_id or stored_previous != previous_hash:
                return False
            digest_input = f"{timestamp:.9f}|{previous_hash}|{payload}".encode("utf-8")
            calculated_hash = hashlib.sha256(digest_input).hexdigest()
            if calculated_hash != stored_hash:
                return False
            previous_hash = stored_hash
            expected_id += 1
        return True

    def verify_chain(self) -> bool:
        """Recompute the complete hash chain and reject gaps or rewrites."""
        try:
            with self._connect() as con:
                rows = con.execute(
                    "SELECT id,ts,payload,previous_hash,record_hash "
                    "FROM survival_cycles ORDER BY id"
                ).fetchall()
        except sqlite3.DatabaseError:
            return False
        return self._rows_valid(rows)

    def replace_cycle(self, cycle_id: int, cycle: dict) -> None:
        try:
            with self._connect() as con:
                con.execute(
                    "UPDATE survival_cycles SET payload=? WHERE id=?",
                    (json.dumps(cycle), cycle_id),
                )
        except sqlite3.DatabaseError as exc:
            raise PermissionError("audit records are append-only") from exc

    def delete_cycle(self, cycle_id: int) -> None:
        try:
            with self._connect() as con:
                con.execute("DELETE FROM survival_cycles WHERE id=?", (cycle_id,))
        except sqlite3.DatabaseError as exc:
            raise PermissionError("audit records are append-only") from exc
