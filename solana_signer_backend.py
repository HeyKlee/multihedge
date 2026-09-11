"""Network backend used only inside the isolated signer service."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import time

import httpx
from solders.address_lookup_table_account import AddressLookupTableAccount
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.message import MessageV0, to_bytes_versioned
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction
from solders.token.associated import get_associated_token_address
from solana.rpc.api import Client
from solana.rpc.commitment import Confirmed, Finalized
from solana.rpc.types import TxOpts

from execution_policy import TradeIntent

NATIVE_SOL_MINT = "So11111111111111111111111111111111111111112"
BUILD_URL = "https://api.jup.ag/swap/v2/build"
MAX_RECONCILIATION_FEE_LAMPORTS = 2_000_000


class SolanaSignerBackend:
    def __init__(self, *, wallet_pubkey: str, rpc_url: str, api_key: str, keypair_loader):
        if not api_key:
            raise RuntimeError("Jupiter API key unavailable to signer")
        self.wallet_pubkey = wallet_pubkey
        self.wallet = Pubkey.from_string(wallet_pubkey)
        self.rpc_url = rpc_url
        self.api_key = api_key
        self.keypair_loader = keypair_loader
        self.rpc = Client(rpc_url)
        self._pre_balances: dict[str, int] | None = None

    def _headers(self):
        return {"x-api-key": self.api_key}

    def _read_atomic(self, mint: str) -> int:
        if mint == NATIVE_SOL_MINT:
            response = self.rpc.get_balance(self.wallet, commitment=Confirmed)
            if response.value is None:
                raise RuntimeError("SOL balance unavailable")
            return int(response.value)
        ata = get_associated_token_address(self.wallet, Pubkey.from_string(mint))
        account = self.rpc.get_account_info(ata, commitment=Confirmed)
        if account.value is None:
            return 0
        response = self.rpc.get_token_account_balance(ata, commitment=Confirmed)
        if response.value is None:
            raise RuntimeError("token balance unavailable")
        return int(response.value.amount)

    def prepare(self, intent: TradeIntent) -> None:
        self._pre_balances = {
            intent.input_mint: self._read_atomic(intent.input_mint),
            intent.output_mint: self._read_atomic(intent.output_mint),
        }

    def current_block_height(self) -> int:
        response = self.rpc.get_block_height(commitment=Confirmed)
        if response.value is None:
            raise RuntimeError("block height unavailable")
        return int(response.value)

    def build(self, intent: TradeIntent) -> dict:
        params = {
            "inputMint": intent.input_mint,
            "outputMint": intent.output_mint,
            "amount": str(intent.amount_atomic),
            "taker": self.wallet_pubkey,
            "slippageBps": str(intent.slippage_bps),
            "maxAccounts": "32",
            "blockhashSlotsToExpiry": "100",
        }
        response = httpx.get(BUILD_URL, params=params, headers=self._headers(), timeout=30)
        if response.status_code != 200:
            raise RuntimeError(f"Jupiter build failed with HTTP {response.status_code}")
        result = response.json()
        if not isinstance(result, dict):
            raise RuntimeError("Jupiter build returned invalid JSON")
        return result

    @staticmethod
    def _instruction(raw: dict) -> Instruction:
        accounts = [
            AccountMeta(
                Pubkey.from_string(account["pubkey"]),
                bool(account.get("isSigner")),
                bool(account.get("isWritable")),
            )
            for account in raw.get("accounts") or []
        ]
        return Instruction(
            Pubkey.from_string(raw["programId"]),
            base64.b64decode(raw["data"], validate=True),
            accounts,
        )

    @staticmethod
    def _raw_instructions(build: dict):
        yield from build.get("computeBudgetInstructions") or []
        yield from build.get("setupInstructions") or []
        if build.get("swapInstruction"):
            yield build["swapInstruction"]
        if build.get("cleanupInstruction"):
            yield build["cleanupInstruction"]
        yield from build.get("otherInstructions") or []

    def compile(self, build: dict) -> dict:
        instructions = [self._instruction(raw) for raw in self._raw_instructions(build)]
        lookups = [
            AddressLookupTableAccount(
                key=Pubkey.from_string(table),
                addresses=[Pubkey.from_string(address) for address in addresses],
            )
            for table, addresses in (build.get("addressesByLookupTableAddress") or {}).items()
        ]
        metadata = build["blockhashWithMetadata"]
        blockhash_raw = metadata["blockhash"]
        blockhash = Hash.from_bytes(bytes(blockhash_raw)) if isinstance(blockhash_raw, list) else Hash.from_string(blockhash_raw)
        message = MessageV0.try_compile(self.wallet, instructions, lookups, blockhash)
        message_bytes = to_bytes_versioned(message)
        message_hash = hashlib.sha256(message_bytes).hexdigest()
        signatures = [Signature.default() for _ in range(message.header.num_required_signatures)]
        unsigned = VersionedTransaction.populate(message, signatures)
        canonical = json.dumps(build, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        return {
            "message": message,
            "message_hash": message_hash,
            "build_hash": hashlib.sha256(canonical).hexdigest(),
            "unsigned": unsigned,
        }

    def simulate(self, compiled: dict) -> dict:
        response = self.rpc.simulate_transaction(compiled["unsigned"], sig_verify=False, commitment=Confirmed)
        value = response.value
        return {
            "ok": value is not None and value.err is None,
            "message_hash": compiled["message_hash"],
            "units": getattr(value, "units_consumed", None) if value is not None else None,
            "error": None if value is None else str(value.err)[:200],
        }

    def sign(self, compiled: dict) -> dict:
        keypair = self.keypair_loader()
        if keypair is None or str(keypair.pubkey()) != self.wallet_pubkey:
            raise RuntimeError("signer wallet identity mismatch")
        transaction = VersionedTransaction(compiled["message"], [keypair])
        signed_hash = hashlib.sha256(to_bytes_versioned(transaction.message)).hexdigest()
        if signed_hash != compiled["message_hash"]:
            raise RuntimeError("signed message differs from simulated message")
        return {"message_hash": signed_hash, "transaction": transaction}

    def send(self, signed: dict) -> str:
        response = self.rpc.send_raw_transaction(
            bytes(signed["transaction"]),
            TxOpts(skip_preflight=False, preflight_commitment=Confirmed, max_retries=3),
        )
        if response.value is None:
            raise RuntimeError("RPC returned no transaction signature")
        return str(response.value)

    def confirm(self, signature: str, last_valid_block_height: int) -> bool:
        try:
            response = self.rpc.confirm_transaction(
                Signature.from_string(signature),
                commitment=Finalized,
                last_valid_block_height=last_valid_block_height,
            )
            return response.value is not None and response.value[0].err is None
        except Exception:
            return False

    def reconcile(self, signature: str, intent: TradeIntent, min_output_atomic: int) -> dict:
        if self._pre_balances is None:
            return {"verified": False, "reason": "missing pre-trade balance snapshot"}
        post_input = self._read_atomic(intent.input_mint)
        post_output = self._read_atomic(intent.output_mint)
        used = self._pre_balances[intent.input_mint] - post_input
        received = post_output - self._pre_balances[intent.output_mint]
        input_ok = used >= intent.amount_atomic
        if intent.input_mint != NATIVE_SOL_MINT:
            input_ok = used == intent.amount_atomic
        elif used > intent.amount_atomic + MAX_RECONCILIATION_FEE_LAMPORTS:
            input_ok = False
        output_floor = min_output_atomic
        if intent.output_mint == NATIVE_SOL_MINT:
            output_floor = max(0, min_output_atomic - MAX_RECONCILIATION_FEE_LAMPORTS)
        verified = input_ok and received >= output_floor
        return {
            "verified": verified,
            "signature": signature,
            "input_atomic": used,
            "output_atomic": received,
            "minimum_output_atomic": min_output_atomic,
            "reason": "balances_reconciled" if verified else "balance_delta_mismatch",
        }
