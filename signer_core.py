"""Signer orchestration with injected backend and mandatory reconciliation."""

from __future__ import annotations

from execution_policy import (
    OrderStore,
    PolicyDenied,
    TradeIntent,
    validate_build,
    validate_intent,
    validate_reserves,
)


class SignerCore:
    def __init__(self, *, wallet_pubkey: str, approved_mints: set[str], order_store: OrderStore, backend):
        self.wallet_pubkey = wallet_pubkey
        self.approved_mints = frozenset(approved_mints)
        self.order_store = order_store
        self.backend = backend

    def execute(
        self,
        intent: TradeIntent,
        pre_treasury_nzd,
        projected_post_nzd,
        fee_reserve_sol,
        required_fee_reserve_sol,
        *,
        treasury_verified: bool,
        treasury_age_seconds: float,
    ) -> dict:
        if treasury_verified is not True or treasury_age_seconds < 0 or treasury_age_seconds > 300:
            raise PolicyDenied("treasury evidence is stale or unverified")
        validate_intent(intent, self.approved_mints)
        validate_reserves(
            pre_treasury_nzd,
            projected_post_nzd,
            fee_reserve_sol,
            required_fee_reserve_sol,
        )
        if not self.order_store.reserve(intent):
            raise PolicyDenied("duplicate or previously attempted order")

        try:
            self.backend.prepare(intent)
            current_height = self.backend.current_block_height()
            build = self.backend.build(intent)
            approved = validate_build(intent, build, self.wallet_pubkey, current_height)
            compiled = self.backend.compile(build)
            if compiled.get("build_hash") != approved.build_hash:
                raise PolicyDenied("compiled payload is not bound to approved build")
            message_hash = compiled.get("message_hash")
            if not message_hash:
                raise PolicyDenied("compiled payload has no message hash")

            simulation = self.backend.simulate(compiled)
            if not simulation.get("ok"):
                raise PolicyDenied("transaction simulation failed")
            if simulation.get("message_hash") != message_hash:
                raise PolicyDenied("simulation payload differs from compiled payload")

            signed = self.backend.sign(compiled)
            if signed.get("message_hash") != message_hash:
                raise PolicyDenied("signed payload differs from simulated payload")
            signature = self.backend.send(signed)
            self.order_store.mark_submitted(intent.order_id, signature)

            if not self.backend.confirm(signature, approved.last_valid_block_height):
                self.order_store.mark_ambiguous(intent.order_id)
                raise PolicyDenied("transaction confirmation failed or expired")

            reconciliation = self.backend.reconcile(
                signature, intent, approved.min_output_atomic
            )
            if not reconciliation.get("verified"):
                self.order_store.mark_ambiguous(intent.order_id)
                raise PolicyDenied("post-finality reconciliation failed")
            self.order_store.mark_reconciled(intent.order_id, reconciliation)
            return {
                "state": "RECONCILED",
                "order_id": intent.order_id,
                "signature": signature,
                "min_output_atomic": approved.min_output_atomic,
                "program_ids": list(approved.program_ids),
                "simulation_units": simulation.get("units"),
                "reconciliation": reconciliation,
            }
        except Exception:
            try:
                row = self.order_store.get(intent.order_id)
                if row["state"] in {"PENDING", "SUBMITTED"}:
                    self.order_store.mark_ambiguous(intent.order_id)
            except Exception:
                pass
            raise
