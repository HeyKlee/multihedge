"""
AutoHedge Solana chain integration (NOVA).

Unified wrapper around Solana RPC + Jupiter swap used by BOTH the devnet
harness (real on-chain, test coins) and the future live-trading pipeline.

Kaupapa:
  - Network modes: "devnet" (test SOL, safe) and "mainnet" (real money).
  - Keypair loading: from SOLANA_PRIVATE_KEY / WALLET_PRIVATE_KEY in .env
    (JSON array or base58) OR freshly generated (ephemeral devnet harness).
    In mainnet+sign mode we NEVER log or store the private key beyond .env.
  - Safety rail: `live_mode` config flag must equal "1" for mainnet writes.
    Devnet writes never require it. Zero/empty balance never sends anything.

Layout versions: solana-py 0.30.2 + solders. The Client uses the positional
object API (.value). Transaction types come from solders.
"""

import os
import sys
import time
import json
import base64
import sqlite3

import httpx
from pathlib import Path

from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.system_program import transfer, TransferParams
from solders.transaction import VersionedTransaction, Transaction

try:
    from solana.rpc.api import Client
    from solana.rpc.types import TxOpts
except Exception:  # pragma: no cover
    Client = None

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------
DEFAULT_DEVNET = "https://api.devnet.solana.com"
DEFAULT_MAINNET = "https://api.mainnet-beta.solana.com"

NATIVE_SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MAINNET = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_DEVNET = "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU"

_JUP_QUOTE = "https://api.jup.ag/swap/v2/quote"
_JUP_SWAP = "https://api.jup.ag/swap/v2/swap"

DB_PATH = Path(__file__).parent / "autohedge.db"


# --------------------------------------------------------------------------
# env helpers
# --------------------------------------------------------------------------

def _env() -> dict:
    out = {}
    # The .env can live next to this module (docker bind-mount at /app/dashboard/.env)
    # OR one level up (/app/.env) depending on deployment. Check both, preferring
    # the nearest to this file so the real keypair is always resolved.
    candidates = [
        Path(__file__).resolve().parent / ".env",
        Path(__file__).resolve().parent.parent / ".env",
    ]
    env_file = next((p for p in candidates if p.exists()), None)
    if env_file is not None:
        for line in env_file.read_text(errors="ignore").splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    for k in ("SOLANA_PRIVATE_KEY", "WALLET_PRIVATE_KEY", "JUPITER_API_KEY",
              "SOLANA_RPC_URL", "SOLANA_NETWORK"):
        if os.getenv(k):
            out[k] = os.getenv(k)
    return out


def _load_env() -> dict:
    return _env()


def get_keypair(env: dict | None = None) -> tuple[Keypair | None, str]:
    """Resolve a Keypair from .env, or generate a fresh ephemeral one."""
    src = env or _load_env()
    secret = src.get("SOLANA_PRIVATE_KEY") or src.get("WALLET_PRIVATE_KEY") or ""
    if secret:
        # JSON array of 64 bytes
        try:
            raw = bytes(json.loads(secret))
            if len(raw) == 64:
                return Keypair.from_bytes(raw), "env"
        except (ValueError, json.JSONDecodeError):
            pass
        # base64 64-byte secret
        try:
            raw = base64.b64decode(secret)
            if len(raw) == 64:
                return Keypair.from_bytes(raw), "env"
        except Exception:
            pass
    kp = Keypair()
    return kp, "ephemeral"


def _resolve_rpc(network: str, url: str | None = None) -> str:
    if url:
        return url
    net = (network or "devnet").lower()
    if net.startswith("mainnet"):
        return DEFAULT_MAINNET
    return DEFAULT_DEVNET


def client(network: str | None = None, rpc_url: str | None = None):
    if Client is None:
        raise RuntimeError("solana-py not installed")
    return Client(rpc_url or _resolve_rpc(network))


# --------------------------------------------------------------------------
# config flags
# --------------------------------------------------------------------------

def _get_cfg(key: str):
    con = sqlite3.connect(DB_PATH)
    try:
        row = con.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
        return row[0] if row else None
    finally:
        con.close()


def _set_cfg(key: str, value: str):
    con = sqlite3.connect(DB_PATH)
    try:
        con.execute(
            "INSERT INTO config(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value))
        con.commit()
    finally:
        con.close()


def set_network(network: str):
    _set_cfg("solana_network", network.lower())


def current_network() -> str:
    """Return only an explicitly supported Solana network.

    Environment configuration takes precedence because the copied chain module
    may not have an AutoHedge config table in the MultiHedge database.
    """
    env_network = (_load_env().get("SOLANA_NETWORK") or "").strip().lower()
    configured = env_network
    if not configured:
        try:
            configured = (_get_cfg("solana_network") or "devnet").strip().lower()
        except sqlite3.Error:
            configured = "devnet"
    aliases = {"mainnet": "mainnet-beta", "mainnet-beta": "mainnet-beta", "devnet": "devnet"}
    if configured not in aliases:
        raise RuntimeError(f"unsupported Solana network: {configured}")
    return aliases[configured]


def live_mode_enabled() -> bool:
    """Mainnet writes require live_mode=1 explicitly set by the user."""
    return (_get_cfg("live_mode") or "0") == "1"


# --------------------------------------------------------------------------
# balance
# --------------------------------------------------------------------------

def get_balance(kp: Keypair | None = None, network: str | None = None,
                pubkey_str: str | None = None, rpc_url: str | None = None) -> float:
    c = client(network, rpc_url)
    pk = Pubkey.from_string(pubkey_str) if pubkey_str else kp.pubkey()
    return c.get_balance(pk).value / 1e9


def get_token_balance(mint: str, kp: Keypair | None = None, pubkey_str: str | None = None,
                      network: str | None = None, rpc_url: str | None = None) -> float:
    c = client(network, rpc_url)
    pk = Pubkey.from_string(pubkey_str) if pubkey_str else kp.pubkey()
    try:
        from solders.token.associated import get_associated_token_address
        ata = get_associated_token_address(pk, Pubkey.from_string(mint))
        resp = c.get_token_account_balance(ata)
        return float(resp.value.amount) / (10 ** resp.value.decimals)
    except Exception:
        return 0.0


# --------------------------------------------------------------------------
# devnet funding (test SOL)
# --------------------------------------------------------------------------

def request_devnet_airdrop(kp: Keypair, sol: float = 2.0, attempts: int = 4,
                           wait: float = 8.0, rpc_url: str | None = None) -> bool:
    """Best-effort devnet SOL faucet. Returns True if balance > 0.

    Devnet airdrops are rate-limited per IP/day; the shared public node can
    run dry. Retries with backoff and reports honestly (no fake success).
    """
    lamports = int(sol * 1e9)
    c = client("devnet", rpc_url)
    for i in range(attempts):
        try:
            res = c.request_airdrop(kp.pubkey(), lamports)
            sig = res.value if hasattr(res, "value") else None
            if sig:
                time.sleep(wait)
                if c.get_balance(kp.pubkey()).value > 0:
                    return True
        except Exception as e:
            print(f"  [devnet faucet] attempt {i}: {type(e).__name__}: {str(e)[:90]}")
            time.sleep(wait)
    return False


# --------------------------------------------------------------------------
# Jupiter swap
# --------------------------------------------------------------------------

def _jup_headers(network: str) -> dict:
    env = _load_env()
    key = env.get("JUPITER_API_KEY") or ""
    out = {"Content-Type": "application/json"}
    if network.startswith("mainnet") and key:
        out["x-api-key"] = key
    return out


def usdc_mint(network: str) -> str:
    net = (network or "devnet").lower()
    return USDC_MAINNET if net.startswith("mainnet") else USDC_DEVNET


def jupiter_quote(amount_lamports: int, input_mint: str, output_mint: str,
                  network: str = "devnet", slippage_bps: int = 100) -> dict | None:
    """Ask Jupiter for a swap route. Returns the quote dict or None."""
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": int(amount_lamports),
        "slippageBps": slippage_bps,
    }
    try:
        r = httpx.get(_JUP_QUOTE, params=params, headers=_jup_headers(network), timeout=20)
        if r.status_code == 200:
            return r.json()
        print("  [jup quote] HTTP", r.status_code, str(r.text)[:140])
    except Exception as e:
        print("  [jup quote] ERR", type(e).__name__, str(e)[:140])
    return None


def build_swap_transaction(keypair: Keypair, quote: dict, network: str = "devnet"):
    """Build a signable VersionedTransaction from a Jupiter quote.

    POSTs to Jupiter /swap (v6), which returns a ready-to-sign tx (base64).
    Returns {"tx_factory", "raw", "recentBlockhash", "lastValidBlockHeight"}.
    """
    payload = {
        "quoteResponse": quote,
        "taker": str(keypair.pubkey()),
        "userPublicKey": str(keypair.pubkey()),
        "wrapAndUnwrapSol": True,
        "dynamicComputeUnitLimit": True,
        "prioritizationFeeLamports": "auto",
    }
    r = httpx.post(_JUP_SWAP, json=payload, headers=_jup_headers(network), timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"jup swap HTTP {r.status_code}: {r.text[:300]}")
    data = r.json()

    tx_b64 = None
    for key in ("swapTransaction", "swapTransactionBase64", "tx", "transaction"):
        v = data.get(key)
        if isinstance(v, str) and v:
            tx_b64 = v
            break
    if tx_b64 is None:
        raise RuntimeError("no tx field in jup swap response")

    raw = base64.b64decode(tx_b64)

    built = None
    try:
        built = VersionedTransaction.from_bytes(raw)
    except Exception:
        try:
            built = Transaction.from_bytes(raw)
        except Exception:
            built = None
    if built is None:
        raise RuntimeError("could not deserialize swap tx")

    # blockhash name differs between legacy (blockhash) and v0 (recent_blockhash)
    _bh_attr = "recent_blockhash" if hasattr(built.message, "recent_blockhash") else "blockhash"
    _bh = getattr(built.message, _bh_attr, None)
    return {
        "transaction": built,
        "raw": raw,
        "recentBlockhash": data.get("recentBlockhash") or str(_bh),
        "lastValidBlockHeight": data.get("lastValidBlockHeight"),
    }


def send_swap(keypair: Keypair, quote: dict, network: str = "devnet",
              skip_preflight: bool = True, rpc_url: str | None = None) -> dict:
    """Quote + build + sign + send a Jupiter swap atomically (devnet or mainnet)."""
    # Mainnet safety rail
    net = (network or "devnet").lower()
    if net.startswith("mainnet") and not live_mode_enabled():
        raise RuntimeError(
            "LIVE MODE OFF. To allow real-money swaps set live_mode=1 in config. "
            "Refusing to spend real SOL.")

    # Zero/empty balance guard
    bal = get_balance(keypair, net, rpc_url=rpc_url)
    if bal <= 0:
        raise RuntimeError(f"wallet balance {bal} SOL; nothing to trade. Fund the wallet first.")

    built = build_swap_transaction(keypair, quote, net)
    tx = built["transaction"]

    if isinstance(tx, VersionedTransaction):
        tx = VersionedTransaction(tx.message, [keypair])
    else:
        tx.sign(keypair)

    c = client(net, rpc_url)
    resp = c.send_raw_transaction(bytes(tx), TxOpts(skip_preflight=skip_preflight))
    sig = resp.value if hasattr(resp, "value") else resp
    return {
        "signature": str(sig),
        "send_ok": True,
        "network": net,
        "in_amount_lam": quote.get("inAmount"),
        "out_amount_lam": quote.get("outAmount"),
        "slippage_bps": quote.get("slippageBps"),
    }


def sol_transfer(keypair: Keypair, to_pubkey: str | Pubkey, sol_amount: float,
                 network: str = "devnet", rpc_url: str | None = None) -> str:
    """Send native SOL. Returns signature string."""
    net = (network or "devnet").lower()
    if net.startswith("mainnet") and not live_mode_enabled():
        raise RuntimeError("LIVE MODE OFF. Set live_mode=1 to send real SOL.")
    if get_balance(keypair, net, rpc_url=rpc_url) < sol_amount:
        raise RuntimeError("insufficient balance for transfer")
    to_pk = Pubkey.from_string(to_pubkey) if isinstance(to_pubkey, str) else to_pubkey
    c = client(net, rpc_url)
    ix = transfer(TransferParams(from_pubkey=keypair.pubkey(), to_pubkey=to_pk,
                                 lamports=int(sol_amount * 1e9)))
    blockhash = c.get_latest_blockhash().value.blockhash
    tx = Transaction.new_with_payer([ix], keypair.pubkey(), blockhash)
    tx.sign(keypair)
    resp = c.send_raw_transaction(bytes(tx))
    return str(resp.value)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "status"
    kp, src = get_keypair()
    net = (sys.argv[3] if len(sys.argv) > 3 else (env := _load_env()).get("SOLANA_NETWORK") or "devnet").lower()
    print(f"network   : {net}")
    print(f"keypair   : {src}  ({kp.pubkey()})")
    if mode == "air":
        amt = float(sys.argv[2]) if len(sys.argv) > 2 else 2.0
        ok = request_devnet_airdrop(kp, amt)
        print("airdrop   :", "OK" if ok else "rate-limited/dry")
        print("balance   :", get_balance(kp, net), "SOL")
    else:
        try:
            print(f"balance   : {get_balance(kp, net)} SOL")
        except Exception as e:
            print("balance   : err", type(e).__name__, str(e)[:80])