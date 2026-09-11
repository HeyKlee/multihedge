"""One-shot isolated signer queue worker.

This process receives no model credential. It revalidates every queued intent,
refreshes treasury rates, applies the strategy entry gate, and is the only
runtime allowed to receive the Solana private key.
"""

from __future__ import annotations

from dataclasses import fields
import hashlib
import json
import os
from pathlib import Path
import sys
import time

import yaml

from autonomous_live import fresh_rates, strategy_evidence, tradeable_universe
from execution_policy import PolicyDenied, TradeIntent, validate_intent

ROOT = Path(__file__).resolve().parent
TRADE_FIELDS = frozenset(field.name for field in fields(TradeIntent))


def _queue() -> Path:
    return Path(os.getenv("MULTIHEDGE_LIVE_QUEUE", str(ROOT / "deploy/data/live_queue")))


def _write_result(directory: Path, request: Path, payload: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / request.name
    temporary = directory / f".{request.name}.{os.getpid()}.tmp"
    temporary.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False),
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    temporary.replace(target)


def _load_intent(path: Path) -> TradeIntent:
    payload = path.read_text(encoding="utf-8")
    if path.stem != hashlib.sha256(payload.encode("utf-8")).hexdigest():
        raise PolicyDenied("queued intent hash mismatch")
    raw = json.loads(payload)
    if not isinstance(raw, dict) or set(raw) != TRADE_FIELDS:
        raise PolicyDenied("queued intent schema mismatch")
    return TradeIntent(**raw)


def validate_autonomous_order_id(cfg: dict, intent: TradeIntent, now: float) -> None:
    seconds = int(cfg.get("live", {}).get("autonomous", {}).get("cycle_seconds", 900))
    symbol = next(
        (coin["symbol"] for coin in tradeable_universe(cfg)
         if coin["mint"] in {intent.input_mint, intent.output_mint}),
        None,
    )
    bucket = int(now // seconds)
    allowed = {f"auto:{value}:{symbol}:{intent.side}" for value in (bucket, bucket - 1)}
    if symbol is None or intent.order_id not in allowed:
        raise PolicyDenied("autonomous order identity is invalid or stale")


def entry_authorized(cfg: dict, intent: TradeIntent, evidence: dict) -> bool:
    if intent.side == "SELL":
        return True
    symbol = next(
        (coin["symbol"] for coin in tradeable_universe(cfg)
         if coin["mint"] == intent.output_mint),
        None,
    )
    return bool(
        evidence.get("qualified") is True
        and symbol
        and evidence.get("by_symbol", {}).get(symbol, {}).get("qualified") is True
    )


def process_one(cfg: dict, request: Path, *, now: float | None = None) -> dict:
    intent = _load_intent(request)
    validate_autonomous_order_id(cfg, intent, time.time() if now is None else now)
    approved = {cfg.get("live", {}).get("reserve_mint")}
    approved.update(coin["mint"] for coin in tradeable_universe(cfg))
    validate_intent(intent, approved)
    evidence = strategy_evidence(cfg)
    authorized = entry_authorized(cfg, intent, evidence)
    if intent.side == "BUY" and not authorized:
        raise PolicyDenied("strategy evidence gate failed in signer")
    sol_usd, nzd_per_usd = fresh_rates()
    os.environ["XORA_SOL_USD"] = str(sol_usd)
    os.environ["XORA_NZD_PER_USD"] = str(nzd_per_usd)
    import live_bridge
    result = live_bridge.execute_live_intent(cfg, intent, entry_authorized=authorized)
    return {
        "state": result["state"], "order_id": intent.order_id,
        "signature": result["signature"], "reconciliation": result["reconciliation"],
    }


def main() -> int:
    queue = _queue()
    pending = queue / "pending"
    processing = queue / "processing"
    completed = queue / "completed"
    failed = queue / "failed"
    for directory in (pending, processing, completed, failed):
        directory.mkdir(parents=True, exist_ok=True)

    orphaned = 0
    for path in processing.glob("*.json"):
        _write_result(failed, path, {"state": "REQUIRES_HUMAN", "reason": "orphaned_request"})
        path.unlink(missing_ok=True)
        orphaned += 1

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    handled = []
    pending_paths = sorted(pending.glob("*.json"))
    if len(pending_paths) > 1:
        for pending_path in pending_paths:
            _write_result(failed, pending_path, {
                "state": "DENIED", "reason": "multiple_requests_in_cycle",
                "request": pending_path.name,
            })
            pending_path.unlink(missing_ok=True)
        print(json.dumps({"orphaned": orphaned, "handled": [],
                          "denied": len(pending_paths)}, sort_keys=True,
                         separators=(",", ":"), allow_nan=False))
        return 0
    for pending_path in pending_paths:
        active = processing / pending_path.name
        try:
            pending_path.replace(active)
        except FileNotFoundError:
            continue
        try:
            result = process_one(cfg, active)
            _write_result(completed, active, result)
            handled.append(result)
        except Exception as exc:
            result = {
                "state": "DENIED", "reason": type(exc).__name__,
                "request": active.name,
            }
            _write_result(failed, active, result)
            handled.append(result)
        finally:
            active.unlink(missing_ok=True)
    print(json.dumps({"orphaned": orphaned, "handled": handled}, sort_keys=True,
                     separators=(",", ":"), allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
