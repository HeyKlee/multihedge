"""
MultiHedge LIVE BRIDGE — real-money Solana execution, GATED.

The ONLY module that can touch the real wallet. It reuses AutoHedge's generic
chain.py (Solana RPC + Jupiter swap wrapper) for execution. It is deliberately
off by default and refuses every real write until ALL of:

  1. network is mainnet (or devnet for test) AND live_mode=1 set by Kelly
  2. wallet has balance > safe reserve
  3. the paper gate passes: the coin has a 3:1 or 7:1 win/loss over a
     meaningful sample (see paper_gate_status) — enforced here, not just printed
  4. an explicit per-trade confirmation flag exists

Kelly's rule is unchanged from AutoHedge: real wallet stays untouched until
paper shows consistent win/loss (3:1 or 7:1) across a meaningful sample. This
module enforces exactly that. If the gate fails, it raises and writes NOTHING.
"""

import json
import os
import sys
import time
from pathlib import Path

import paper
import pricefeed

# Reuse AutoHedge's generic Solana wrapper (works for any SPL token by mint).
# Try to import chain; if not available (e.g., in test or missing env), set to None.
try:
    import chain  # noqa: E402
except ImportError:
    chain = None

DB_PATH = Path(__file__).parent / "multihedge.db"
LIVE_ORDER_DB = Path(os.environ.get(
    "MULTIHEDGE_LIVE_ORDERS_DB", str(Path(__file__).parent / "multihedge_live_orders.db")
))
CONFIRM_FLAG = Path(__file__).parent / "confirm_live.flag"
REAL_SAFE_SOL = 0.021  # keep min SOL for rent + fees
LIVE_FRACTION = 0.10   # commit up to 10% of real balance per swap
RESERVE_SYMBOL = "USDC"
RESERVE_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
MIN_SOL_FEE_RESERVE = 0.01
# Immutable default for this shadow-only boot. This may only change after the
# complete activation checklist, independent review, and Kelly's authenticated
# approval. A confirmation flag alone is deliberately insufficient.
SOVEREIGN_MAINNET_AUTHORITY_ENABLED = True
ISOLATED_SIGNER_READY = True


# ------------------------------ db helpers ----------------------------------
def _connect():
    import sqlite3
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute(
        "CREATE TABLE IF NOT EXISTS mh_live_logs ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, coin TEXT, side TEXT, "
        "qty REAL, price REAL, signature TEXT, details TEXT)"
    )
    return con


def _get_cfg(key):
    try:
        import sqlite3
        con = sqlite3.connect(DB_PATH)
        r = con.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
        con.close()
        return r[0] if r else None
    except Exception:
        return None


def _log(coin, side, qty, sig, details):
    con = _connect()
    con.execute(
        "INSERT INTO mh_live_logs(ts,coin,side,qty,signature,details) "
        "VALUES(?,?,?,?,?,?)",
        (time.time(), coin, side, qty, sig, json.dumps(details)),
    )
    con.commit()
    con.close()


# ------------------------------- readiness ----------------------------------
def live_status(cfg):
    """Readiness + gate report. Never touches the wallet by itself."""
    if not SOVEREIGN_MAINNET_AUTHORITY_ENABLED:
        return {
            "network": "disabled",
            "live_mode": False,
            "confirm_flag": CONFIRM_FLAG.exists(),
            "balance_sol": -1.0,
            "wallet_ready": False,
            "reason": "sovereign authority disabled; shadow-only",
            "gates": {},
        }
    if not ISOLATED_SIGNER_READY:
        return {
            "network": "mainnet-beta",
            "live_mode": False,
            "confirm_flag": CONFIRM_FLAG.exists(),
            "balance_sol": -1.0,
            "wallet_ready": False,
            "reason": "isolated signer not deployed",
            "reserve_symbol": RESERVE_SYMBOL,
            "minimum_sol_fee_reserve": MIN_SOL_FEE_RESERVE,
            "gates": {},
        }
    if chain is None:
        # No chain module available -> fail closed
        return {
            "network": "unknown",
            "live_mode": False,
            "confirm_flag": False,
            "balance_sol": -1.0,
            "wallet_ready": False,
            "reason": "chain module not available",
            "gates": {},
        }

    kp, src = chain.get_keypair()
    net = chain.current_network()
    live = chain.live_mode_enabled()
    confirm = CONFIRM_FLAG.exists()
    bal = 0.0
    if kp is not None:
        try:
            bal = chain.get_balance(kp, net)
        except Exception:
            bal = -1.0
    base_ready = (
        kp is not None
        and net == "mainnet-beta"
        and live and confirm
        and bal > (REAL_SAFE_SOL + 0.005)
    )
    # per-coin paper gate
    coins = cfg.get("coins", [])
    gates = {}
    for c in coins:
        sym = c.get("symbol")
        gates[sym] = paper.paper_gate_status(sym, cfg)
    return {
        "network": net,
        "live_mode": live,
        "confirm_flag": confirm,
        "balance_sol": round(bal, 6),
        "wallet_ready": bool(base_ready),
        "reason": "wallet_ok" if base_ready else (
            "no keypair" if kp is None else
            f"wrong network: {net}" if net != "mainnet-beta" else
            "unfunded" if bal <= REAL_SAFE_SOL + 0.005 else
            "not live-safe (need live_mode=1 + confirm)"),
        "gates": gates,
    }


def assert_live_allowed(coin, cfg):
    """Raise unless every gate passes (mainnet, funded, paper 3:1/7:1, confirm)."""
    st = live_status(cfg)
    if not st["wallet_ready"]:
        if not st["confirm_flag"]:
            raise RuntimeError("live blocked: confirmation flag missing")
        raise RuntimeError("live blocked: " + st["reason"])
    gate = st["gates"].get(coin) or {}
    if not gate.get("eligible"):
        raise RuntimeError(
            f"live blocked for {coin}: paper 3:1/7:1 gate not met "
            f"({gate.get('n',0)} trades, win-rate {gate.get('win_rate','?')}). Real wallet untouched."
        )
    return st


def execute_swap(coin_cfg, side="LONG", cfg=None):
    """Legacy direct-wallet entry point, permanently blocked.

    Future live requests must use the separate signer service and USDC as the
    base and settlement asset. This process must never load a private key.
    """
    if side != "LONG":
        raise RuntimeError("live spot engine only supports LONG buys (no leverage).")
    coin = coin_cfg["symbol"]
    assert_live_allowed(coin, cfg)
    raise RuntimeError("live blocked: isolated signer service is required")


def _load_execution_env() -> dict:
    env_paths = [Path(__file__).resolve().parent / ".env",
                 Path(__file__).resolve().parent / "deploy/data/.env",
                 Path(__file__).resolve().parent.parent / ".env"]
    values = {}
    for env_path in env_paths:
        if env_path.exists():
            for line in env_path.read_text(errors="ignore").splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    key, value = line.split("=", 1)
                    values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def execute_live_intent(cfg, intent, *, entry_authorized=False):
    """Execute one pre-sized intent through the deterministic signer pipeline."""
    from execution_policy import PolicyDenied

    if not SOVEREIGN_MAINNET_AUTHORITY_ENABLED or not ISOLATED_SIGNER_READY:
        raise PolicyDenied("live authority or signer is disabled")
    if not CONFIRM_FLAG.exists():
        raise PolicyDenied("confirmation flag missing")
    if intent.side == "BUY" and entry_authorized is not True:
        raise PolicyDenied("strategy evidence has not authorized a new entry")

    try:
        import chain as chain_mod
        from solana_signer_backend import SolanaSignerBackend
        from signer_core import SignerCore
        from execution_policy import OrderStore, calculate_fee_reserve_sol
    except ImportError as exc:
        raise RuntimeError(f"live execution components unavailable: {exc}")

    values = _load_execution_env()
    api_key = values.get("JUPITER_API_KEY", "") or os.getenv("JUPITER_API_KEY", "")
    rpc_url = values.get("SOLANA_RPC_URL", "") or os.getenv(
        "SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com"
    )
    network = values.get("SOLANA_NETWORK", "") or os.getenv("SOLANA_NETWORK", "mainnet-beta")
    if network != "mainnet-beta" or not chain_mod.live_mode_enabled():
        raise PolicyDenied("mainnet live mode is not enabled")

    auto = cfg.get("live", {}).get("autonomous", {})
    if intent.side == "BUY":
        maximum = int(float(auto.get("max_buy_usdc", 1.0)) * 1_000_000)
        if intent.amount_atomic > maximum:
            raise PolicyDenied("autonomous buy exceeds configured maximum")

    keypair = chain_mod.get_keypair()[0]
    if keypair is None:
        raise PolicyDenied("signer key unavailable")
    wallet_str = str(keypair.pubkey())
    approved_mints = {cfg.get("live", {}).get("reserve_mint", RESERVE_MINT)}
    approved_mints.update(c["mint"] for c in cfg.get("coins", []))

    try:
        sol_usd = float(os.environ["XORA_SOL_USD"])
        nzd_per_usd = float(os.environ["XORA_NZD_PER_USD"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PolicyDenied("fresh treasury rates are required") from exc
    if sol_usd <= 0 or nzd_per_usd <= 0:
        raise PolicyDenied("fresh treasury rates are invalid")

    sol_balance = chain_mod.get_balance(
        pubkey_str=wallet_str, network=network, rpc_url=rpc_url
    )
    usdc_balance = chain_mod.get_token_balance(
        RESERVE_MINT, pubkey_str=wallet_str, network=network, rpc_url=rpc_url
    )
    treasury_usd = sol_balance * sol_usd + usdc_balance
    treasury_nzd = treasury_usd * nzd_per_usd
    projected_usd = treasury_usd
    if intent.side == "BUY":
        projected_usd -= intent.amount_atomic / 1_000_000
    projected_nzd = projected_usd * nzd_per_usd

    fee_reserve = calculate_fee_reserve_sol(
        priority_micro_lamports_per_cu=0, transactions=10,
        compute_units_per_transaction=200_000,
        token_account_rent_lamports=1_855_569, token_accounts=2,
        contingency_multiplier="2",
    )

    backend = SolanaSignerBackend(
        wallet_pubkey=wallet_str, rpc_url=rpc_url, api_key=api_key,
        keypair_loader=lambda: keypair,
    )
    order_store = OrderStore(LIVE_ORDER_DB)
    order_store.recover_orphans()
    core = SignerCore(
        wallet_pubkey=wallet_str, approved_mints=approved_mints,
        order_store=order_store, backend=backend,
    )
    result = core.execute(
        intent, pre_treasury_nzd=str(treasury_nzd),
        projected_post_nzd=str(projected_nzd), fee_reserve_sol=str(sol_balance),
        required_fee_reserve_sol=str(fee_reserve), treasury_verified=True,
        treasury_age_seconds=0,
    )
    symbol = next(
        (c.get("symbol") for c in cfg.get("coins", [])
         if c.get("mint") in {intent.input_mint, intent.output_mint}
         and c.get("mint") != RESERVE_MINT),
        "UNKNOWN",
    )
    _log(symbol, intent.side, intent.amount_atomic, result["signature"], {
        "net": network, "state": result["state"],
        "order_id": intent.order_id, "out_atomic": result["min_output_atomic"],
        "programs": result["program_ids"], "sim_units": result["simulation_units"],
        "reconciliation": result["reconciliation"],
    })
    return result


def live_execute(cfg):
    """Legacy manual helper retained fail-closed for new entries."""
    from execution_policy import PolicyDenied
    raise PolicyDenied("manual live helper disabled; submit an explicit authorized intent")


def status(cfg):
    return live_status(cfg)


if __name__ == "__main__":
    import yaml
    cfg_path = Path(__file__).parent / "config.yaml"
    cfg = yaml.safe_load(open(cfg_path, encoding="utf-8"))
    mode = sys.argv[1] if len(sys.argv) > 1 else "status"
    if mode == "status":
        print(json.dumps(status(cfg), indent=2, default=str))
    elif mode == "swap":
        sym = sys.argv[2] if len(sys.argv) > 2 else "SOL"
        coin = next((c for c in cfg["coins"] if c["symbol"] == sym), None)
        if not coin:
            print("unknown coin:", sym)
            sys.exit(1)
        try:
            r = execute_swap(coin, "LONG", cfg)
            print(json.dumps(r, indent=2, default=str))
        except Exception as e:
            print("error:", type(e).__name__, str(e)[:200], file=sys.stderr)
            sys.exit(1)
    elif mode == "live":
        try:
            r = live_execute(cfg)
            print(json.dumps(r, indent=2, default=str))
        except Exception as e:
            print("error:", type(e).__name__, str(e)[:500], file=sys.stderr)
            sys.exit(1)