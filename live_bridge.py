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
CONFIRM_FLAG = Path(__file__).parent / "confirm_live.flag"
REAL_SAFE_SOL = 0.021  # keep min SOL for rent + fees
LIVE_FRACTION = 0.10   # commit up to 10% of real balance per swap
RESERVE_SYMBOL = "USDC"
RESERVE_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
MIN_SOL_FEE_RESERVE = 0.01
# Immutable default for this shadow-only boot. This may only change after the
# complete activation checklist, independent review, and Kelly's authenticated
# approval. A confirmation flag alone is deliberately insufficient.
SOVEREIGN_MAINNET_AUTHORITY_ENABLED = False
ISOLATED_SIGNER_READY = False


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