"""Deterministic policy primitives for isolated Solana execution.

This module owns no private key and performs no network calls. It validates a
small trade intent and Jupiter Swap V2 /build response before a signer may act.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import sqlite3
import time

SYSTEM_PROGRAM = "11111111111111111111111111111111"
COMPUTE_BUDGET_PROGRAM = "ComputeBudget111111111111111111111111111111"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
ASSOCIATED_TOKEN_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
JUPITER_V6_PROGRAM = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
USDC_MAINNET_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
ALLOWED_PROGRAMS = frozenset(
    {SYSTEM_PROGRAM, COMPUTE_BUDGET_PROGRAM, TOKEN_PROGRAM, ASSOCIATED_TOKEN_PROGRAM, JUPITER_V6_PROGRAM}
)
PROTECTED_FLOOR_NZD = Decimal("60.00")
MAX_SLIPPAGE_BPS = 50
MAX_PRICE_IMPACT = Decimal("0.0075")
MAX_EXPIRY_BLOCKS = 300
MIN_FEE_RESERVE_SOL = Decimal("0.01")


def calculate_fee_reserve_sol(
    *,
    priority_micro_lamports_per_cu,
    transactions: int = 10,
    compute_units_per_transaction: int = 200_000,
    token_account_rent_lamports: int = 1_855_569,
    token_accounts: int = 2,
    contingency_multiplier="2",
) -> Decimal:
    """Return a conservative SOL fee reserve, floored at 0.01 SOL.

    The priority-rate floor is 50,000 micro-lamports per compute unit and the
    base/signature allowance is 10,000 lamports per transaction.
    """
    if transactions < 1 or compute_units_per_transaction < 1 or token_accounts < 0:
        raise ValueError("invalid fee-reserve assumptions")
    priority_rate = max(Decimal("50000"), _decimal(
        priority_micro_lamports_per_cu, "priority fee rate"
    ))
    per_transaction = Decimal("10000") + (
        priority_rate * Decimal(compute_units_per_transaction) / Decimal("1000000")
    )
    total_lamports = (
        per_transaction * Decimal(transactions)
        + Decimal(token_account_rent_lamports) * Decimal(token_accounts)
    ) * _decimal(contingency_multiplier, "contingency multiplier")
    calculated = total_lamports / Decimal("1000000000")
    return max(MIN_FEE_RESERVE_SOL, calculated).quantize(Decimal("0.000000001"))


class PolicyDenied(RuntimeError):
    pass


@dataclass(frozen=True)
class TradeIntent:
    order_id: str
    side: str
    input_mint: str
    output_mint: str
    amount_atomic: int
    slippage_bps: int
    strategy: str
    expected_reward_nzd: str
    expected_loss_nzd: str


@dataclass(frozen=True)
class ApprovedBuild:
    min_output_atomic: int
    last_valid_block_height: int
    program_ids: tuple[str, ...]
    build_hash: str


def _decimal(value, label: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise PolicyDenied(f"invalid {label}") from exc
    if not result.is_finite():
        raise PolicyDenied(f"invalid {label}")
    return result


def validate_intent(intent: TradeIntent, approved_mints: set[str] | frozenset[str]) -> None:
    if not intent.order_id or len(intent.order_id) > 160:
        raise PolicyDenied("invalid order id")
    if intent.side not in {"BUY", "SELL"}:
        raise PolicyDenied("spot side must be BUY or SELL")
    if intent.input_mint == intent.output_mint:
        raise PolicyDenied("input and output mints must differ")
    if intent.input_mint not in approved_mints or intent.output_mint not in approved_mints:
        raise PolicyDenied("mint is not approved")
    if intent.side == "BUY" and intent.input_mint != USDC_MAINNET_MINT:
        raise PolicyDenied("BUY input must be mainnet USDC")
    if intent.side == "SELL" and intent.output_mint != USDC_MAINNET_MINT:
        raise PolicyDenied("SELL output must be mainnet USDC")
    if intent.input_mint == USDC_MAINNET_MINT and intent.output_mint == USDC_MAINNET_MINT:
        raise PolicyDenied("USDC cannot trade to itself")
    if isinstance(intent.amount_atomic, bool) or not isinstance(intent.amount_atomic, int) or intent.amount_atomic <= 0:
        raise PolicyDenied("amount must be a positive atomic integer")
    if not 0 < intent.slippage_bps <= MAX_SLIPPAGE_BPS:
        raise PolicyDenied("slippage exceeds 50 bps")
    reward = _decimal(intent.expected_reward_nzd, "expected reward")
    loss = _decimal(intent.expected_loss_nzd, "expected loss")
    if loss <= 0 or reward < loss * 2:
        raise PolicyDenied("expected net reward must be at least twice expected loss")
    if not intent.strategy.strip():
        raise PolicyDenied("strategy identity required")


def _instructions(build: dict):
    for item in build.get("computeBudgetInstructions") or []:
        yield item
    for item in build.get("setupInstructions") or []:
        yield item
    swap = build.get("swapInstruction")
    if swap:
        yield swap
    cleanup = build.get("cleanupInstruction")
    if cleanup:
        yield cleanup
    for item in build.get("otherInstructions") or []:
        yield item


def validate_build(
    intent: TradeIntent,
    build: dict,
    wallet_pubkey: str,
    current_block_height: int,
) -> ApprovedBuild:
    if build.get("tipInstruction") is not None:
        raise PolicyDenied("tip instruction is prohibited")
    if build.get("inputMint") != intent.input_mint or build.get("outputMint") != intent.output_mint:
        raise PolicyDenied("build mint mismatch")
    try:
        in_amount = int(build.get("inAmount"))
        out_amount = int(build.get("outAmount"))
        min_output = int(build.get("otherAmountThreshold"))
        slippage = int(build.get("slippageBps"))
    except (TypeError, ValueError) as exc:
        raise PolicyDenied("invalid build amounts") from exc
    if in_amount != intent.amount_atomic or out_amount <= 0 or min_output <= 0 or min_output > out_amount:
        raise PolicyDenied("build amount mismatch")
    if slippage != intent.slippage_bps or slippage > MAX_SLIPPAGE_BPS:
        raise PolicyDenied("build slippage mismatch")
    impact = _decimal(build.get("priceImpactPct"), "price impact")
    if impact < 0 or impact > MAX_PRICE_IMPACT:
        raise PolicyDenied("price impact exceeds 0.75%")

    metadata = build.get("blockhashWithMetadata") or {}
    try:
        last_valid = int(metadata.get("lastValidBlockHeight"))
    except (TypeError, ValueError) as exc:
        raise PolicyDenied("missing transaction expiry") from exc
    blocks_left = last_valid - int(current_block_height)
    if blocks_left <= 0:
        raise PolicyDenied("transaction expired")
    if blocks_left > MAX_EXPIRY_BLOCKS:
        raise PolicyDenied("transaction expiry exceeds policy")

    program_ids: list[str] = []
    instruction_count = 0
    for instruction in _instructions(build):
        instruction_count += 1
        if not isinstance(instruction, dict):
            raise PolicyDenied("opaque instruction")
        program_id = instruction.get("programId")
        if program_id not in ALLOWED_PROGRAMS:
            raise PolicyDenied(f"program is not allowlisted: {program_id}")
        program_ids.append(program_id)
        for account in instruction.get("accounts") or []:
            if account.get("isSigner") and account.get("pubkey") != wallet_pubkey:
                raise PolicyDenied("unexpected signer account")
    if instruction_count == 0 or not build.get("swapInstruction"):
        raise PolicyDenied("missing swap instruction")
    if JUPITER_V6_PROGRAM not in program_ids:
        raise PolicyDenied("swap program is not Jupiter V6")
    swap_accounts = (build.get("swapInstruction") or {}).get("accounts") or []
    wallet_is_signer = any(a.get("isSigner") and a.get("pubkey") == wallet_pubkey for a in swap_accounts)
    if not wallet_is_signer:
        raise PolicyDenied("wallet signer required in swap instruction")
    if build.get("otherInstructions"):
        raise PolicyDenied("other instructions are prohibited")

    canonical = json.dumps(build, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return ApprovedBuild(
        min_output_atomic=min_output,
        last_valid_block_height=last_valid,
        program_ids=tuple(program_ids),
        build_hash=hashlib.sha256(canonical).hexdigest(),
    )


def validate_reserves(
    pre_treasury_nzd,
    projected_post_nzd,
    fee_reserve_sol,
    required_fee_reserve_sol=MIN_FEE_RESERVE_SOL,
) -> None:
    pre = _decimal(pre_treasury_nzd, "pre treasury")
    post = _decimal(projected_post_nzd, "post treasury")
    reserve = _decimal(fee_reserve_sol, "fee reserve")
    required = max(
        MIN_FEE_RESERVE_SOL,
        _decimal(required_fee_reserve_sol, "required fee reserve"),
    )
    if pre < PROTECTED_FLOOR_NZD or post < PROTECTED_FLOOR_NZD:
        raise PolicyDenied("protected NZ$60 floor would be breached")
    if reserve < required:
        raise PolicyDenied("SOL fee reserve is insufficient")


class OrderStore:
    """Persistent idempotency ledger. Existing order IDs are never reserved twice."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            con.execute(
                "CREATE TABLE IF NOT EXISTS live_orders ("
                "order_id TEXT PRIMARY KEY, created_ts REAL NOT NULL, updated_ts REAL NOT NULL, "
                "state TEXT NOT NULL, intent_json TEXT NOT NULL, signature TEXT, reconciliation_json TEXT)"
            )

    def _connect(self):
        return sqlite3.connect(self.path, timeout=30)

    def reserve(self, intent: TradeIntent) -> bool:
        payload = json.dumps(asdict(intent), sort_keys=True, separators=(",", ":"), allow_nan=False)
        now = time.time()
        try:
            with self._connect() as con:
                con.execute("BEGIN IMMEDIATE")
                con.execute(
                    "INSERT INTO live_orders(order_id,created_ts,updated_ts,state,intent_json) VALUES(?,?,?,?,?)",
                    (intent.order_id, now, now, "PENDING", payload),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def _transition(self, order_id: str, from_states: tuple[str, ...], state: str, **fields) -> None:
        assignments = ["state=?", "updated_ts=?"]
        values: list[object] = [state, time.time()]
        for key, value in fields.items():
            if key not in {"signature", "reconciliation_json"}:
                raise ValueError("unsupported order field")
            assignments.append(f"{key}=?")
            values.append(value)
        values.extend([order_id, *from_states])
        placeholders = ",".join("?" for _ in from_states)
        with self._connect() as con:
            cur = con.execute(
                f"UPDATE live_orders SET {','.join(assignments)} WHERE order_id=? AND state IN ({placeholders})",
                values,
            )
            if cur.rowcount != 1:
                raise PolicyDenied("invalid or ambiguous order transition")

    def mark_submitted(self, order_id: str, signature: str) -> None:
        if not signature:
            raise PolicyDenied("signature required")
        self._transition(order_id, ("PENDING",), "SUBMITTED", signature=signature)

    def mark_reconciled(self, order_id: str, reconciliation: dict) -> None:
        payload = json.dumps(reconciliation, sort_keys=True, separators=(",", ":"), allow_nan=False)
        self._transition(order_id, ("SUBMITTED",), "RECONCILED", reconciliation_json=payload)

    def mark_ambiguous(self, order_id: str) -> None:
        self._transition(order_id, ("PENDING", "SUBMITTED"), "REQUIRES_HUMAN")

    def get(self, order_id: str) -> dict:
        with self._connect() as con:
            con.row_factory = sqlite3.Row
            row = con.execute("SELECT * FROM live_orders WHERE order_id=?", (order_id,)).fetchone()
        if row is None:
            raise KeyError(order_id)
        return dict(row)

    def recover_orphans(self) -> int:
        """Promote PENDING and SUBMITTED orders to REQUIRES_HUMAN on restart."""
        with self._connect() as con:
            count = con.execute(
                "UPDATE live_orders SET state='REQUIRES_HUMAN',updated_ts=? "
                "WHERE state IN ('PENDING','SUBMITTED')",
                (time.time(),),
            ).rowcount
        return count or 0
