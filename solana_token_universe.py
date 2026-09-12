"""Deterministic discovery and admission for Solana scalp candidates.

Token identity is always the mint address. Display tickers are untrusted metadata.
Only legacy SPL tokens with disabled authorities, adequate liquidity/activity, no
Jupiter Shield warnings, and a reversible USDC route may become live entries.
"""

from __future__ import annotations

from datetime import datetime, timezone
import math
import time
from typing import Callable

import httpx

TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL_MINT = "So11111111111111111111111111111111111111112"
TOKENS_URL = "https://api.jup.ag/tokens/v2"
SHIELD_URL = "https://api.jup.ag/ultra/v1/shield"
QUOTE_URL = "https://api.jup.ag/swap/v2/quote"
PRICE_URL = "https://api.jup.ag/price/v3"

# Jupiter returns 429 under burst load and transient 5xx during deploys. These
# are retryable upstream conditions, not token safety verdicts, so they get a
# bounded retry instead of aborting a whole autonomous cycle.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
MAX_ATTEMPTS = 3
RETRY_BASE_SECONDS = 1.5
RETRY_MAX_SECONDS = 5.0


class TokenDenied(RuntimeError):
    pass


class UpstreamUnavailable(TokenDenied):
    """Jupiter could not be reached or rate-limited every retry.

    Subclasses TokenDenied so every existing fail-closed handler still blocks
    entries, while cycle launchers can report an upstream outage instead of
    dying with a bare traceback.
    """


class FakeResponse:
    """Small response adapter useful for deterministic callers and tests."""

    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if not 200 <= self.status_code < 300:
            raise httpx.HTTPStatusError("request failed", request=None, response=None)


def _settings(cfg: dict) -> dict:
    return cfg.get("live", {}).get("autonomous", {}).get("dynamic_universe", {})


def _number(value, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TokenDenied(f"invalid {label}")
    result = float(value)
    if not math.isfinite(result):
        raise TokenDenied(f"invalid {label}")
    return result


def _timestamp(value: str) -> float:
    if not isinstance(value, str):
        raise TokenDenied("token age unavailable")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
    except ValueError as exc:
        raise TokenDenied("token age unavailable") from exc


def admit_token(raw: dict, cfg: dict, *, now: float, warnings: list) -> dict:
    """Return a compact mint-identified candidate or deny it fail-closed."""
    if not isinstance(raw, dict):
        raise TokenDenied("invalid token metadata")
    settings = _settings(cfg)
    mint = raw.get("id")
    if not isinstance(mint, str) or not 32 <= len(mint) <= 44 or mint in {USDC_MINT, SOL_MINT}:
        raise TokenDenied("invalid trade mint")
    if raw.get("tokenProgram") != TOKEN_PROGRAM:
        raise TokenDenied("unsupported token program")
    decimals = raw.get("decimals")
    if isinstance(decimals, bool) or not isinstance(decimals, int) or not 0 <= decimals <= 12:
        raise TokenDenied("invalid token decimals")
    audit = raw.get("audit")
    if not isinstance(audit, dict):
        raise TokenDenied("token audit unavailable")
    if audit.get("mintAuthorityDisabled") is not True or audit.get("freezeAuthorityDisabled") is not True:
        raise TokenDenied("token authorities remain enabled")
    if warnings:
        raise TokenDenied("Jupiter shield warning present")
    updated = _timestamp(raw.get("updatedAt"))
    if updated > now + 30 or now - updated > float(settings.get("maximum_metadata_age_seconds", 300)):
        raise TokenDenied("token metadata is stale")
    if now - _timestamp(raw.get("createdAt")) < float(settings.get("minimum_age_seconds", 3600)):
        raise TokenDenied("token is too new")
    liquidity = _number(raw.get("liquidity"), "liquidity")
    holders = _number(raw.get("holderCount"), "holder count")
    organic = _number(raw.get("organicScore"), "organic score")
    concentration = _number(audit.get("topHoldersPercentage"), "holder concentration")
    stats = raw.get("stats5m")
    if not isinstance(stats, dict):
        raise TokenDenied("5m market activity unavailable")
    traders = _number(stats.get("numTraders"), "5m traders")
    buy_volume = _number(stats.get("buyVolume"), "5m buy volume")
    sell_volume = _number(stats.get("sellVolume"), "5m sell volume")
    if liquidity < float(settings.get("minimum_liquidity_usd", 100_000)):
        raise TokenDenied("liquidity below minimum")
    if holders < float(settings.get("minimum_holders", 500)):
        raise TokenDenied("holder count below minimum")
    if concentration > float(settings.get("maximum_top_holders_pct", 35)):
        raise TokenDenied("holder concentration above maximum")
    if organic < float(settings.get("minimum_organic_score", 50)):
        raise TokenDenied("organic score below minimum")
    if traders < float(settings.get("minimum_5m_traders", 50)):
        raise TokenDenied("5m trader count below minimum")
    if buy_volume < float(settings.get("minimum_5m_buy_volume_usd", 5_000)):
        raise TokenDenied("5m buy volume below minimum")
    if sell_volume < float(settings.get("minimum_5m_sell_volume_usd", 5_000)):
        raise TokenDenied("5m sell volume below minimum")
    ticker = str(raw.get("symbol") or "UNKNOWN")[:24]
    price = _number(raw.get("usdPrice"), "USD price")
    if price <= 0:
        raise TokenDenied("USD price unavailable")
    return {
        "symbol": mint,
        "ticker": ticker,
        "name": str(raw.get("name") or ticker)[:80],
        "mint": mint,
        "decimals": decimals,
        "entry_eligible": True,
        "market": {
            "latest_usd": price,
            "latest_ts": updated,
            "samples": 2,
            "return_5m_pct": _number(stats.get("priceChange", 0), "5m price change"),
            "return_1h_pct": _number((raw.get("stats1h") or {}).get("priceChange", 0), "1h price change"),
            "liquidity_usd": liquidity,
            "holders": int(holders),
            "top_holders_pct": concentration,
            "organic_score": organic,
            "traders_5m": int(traders),
            "buy_volume_5m_usd": buy_volume,
            "sell_volume_5m_usd": sell_volume,
        },
    }


def _headers(api_key: str) -> dict:
    if not api_key:
        raise TokenDenied("Jupiter API key unavailable")
    return {"x-api-key": api_key}


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _retry_delay(attempt: int, response=None) -> float:
    """Backoff for one retry, honouring a capped Retry-After when present."""
    requested = 0.0
    try:
        requested = float(response.headers.get("Retry-After"))
    except (AttributeError, TypeError, ValueError):
        requested = 0.0
    if requested <= 0:
        requested = RETRY_BASE_SECONDS * (2 ** attempt)
    return max(0.0, min(requested, RETRY_MAX_SECONDS))


def _get_json(get: Callable, url: str, *, api_key: str, params: dict | None = None):
    headers = _headers(api_key)
    for attempt in range(MAX_ATTEMPTS):
        last = attempt + 1 >= MAX_ATTEMPTS
        try:
            response = get(url, headers=headers, params=params, timeout=30)
        except httpx.HTTPError as exc:
            if last:
                raise UpstreamUnavailable(
                    f"Jupiter metadata transport error: {type(exc).__name__}") from exc
            _sleep(_retry_delay(attempt))
            continue
        if response.status_code == 200:
            try:
                return response.json()
            except ValueError as exc:
                raise TokenDenied("Jupiter metadata returned invalid JSON") from exc
        if response.status_code not in RETRYABLE_STATUS:
            raise TokenDenied(f"Jupiter metadata HTTP {response.status_code}")
        if last:
            raise UpstreamUnavailable(f"Jupiter metadata HTTP {response.status_code}")
        _sleep(_retry_delay(attempt, response))
    raise UpstreamUnavailable("Jupiter metadata unavailable")


def discover_candidates(cfg: dict, *, api_key: str, now: float, get=httpx.get) -> list[dict]:
    settings = _settings(cfg)
    if settings.get("enabled") is not True:
        return []
    raw_rows = []
    for category in ("toptraded", "toptrending", "toporganicscore"):
        body = _get_json(
            get, f"{TOKENS_URL}/{category}/5m", api_key=api_key,
            params={"limit": int(settings.get("source_limit", 50))},
        )
        if not isinstance(body, list):
            raise TokenDenied("invalid Jupiter category response")
        raw_rows.extend(body)
    unique = {}
    for row in raw_rows:
        if isinstance(row, dict) and isinstance(row.get("id"), str):
            unique.setdefault(row["id"], row)
    mints = list(unique)
    warning_map = {}
    for start in range(0, len(mints), 100):
        batch = mints[start:start + 100]
        body = _get_json(get, SHIELD_URL, api_key=api_key, params={"mints": ",".join(batch)})
        if not isinstance(body, dict) or not isinstance(body.get("warnings"), dict):
            raise TokenDenied("invalid Jupiter shield response")
        warning_map.update(body["warnings"])
    candidates = []
    for mint, row in unique.items():
        try:
            candidates.append(admit_token(row, cfg, now=now, warnings=warning_map.get(mint, [])))
        except TokenDenied:
            continue
    candidates.sort(
        key=lambda row: (
            row["market"]["organic_score"], row["market"]["liquidity_usd"],
            row["market"]["traders_5m"],
        ), reverse=True,
    )
    return candidates[:int(settings.get("max_candidates", 12))]


def verify_token(mint: str, cfg: dict, *, api_key: str, now: float, get=httpx.get) -> dict:
    rows = _get_json(get, f"{TOKENS_URL}/search", api_key=api_key, params={"query": mint})
    exact = [row for row in rows if isinstance(row, dict) and row.get("id") == mint] if isinstance(rows, list) else []
    if len(exact) != 1:
        raise TokenDenied("exact token metadata unavailable")
    shield = _get_json(get, SHIELD_URL, api_key=api_key, params={"mints": mint})
    warnings = shield.get("warnings", {}).get(mint) if isinstance(shield, dict) else None
    if warnings is None:
        raise TokenDenied("shield result unavailable")
    return admit_token(exact[0], cfg, now=now, warnings=warnings)


def resolve_holdings(mints: list[str], *, api_key: str, now: float, get=httpx.get) -> list[dict]:
    """Resolve registered bot holdings for exits without applying entry gates."""
    unique = list(dict.fromkeys(mint for mint in mints if isinstance(mint, str)))
    if not unique:
        return []
    rows = _get_json(get, f"{TOKENS_URL}/search", api_key=api_key,
                     params={"query": ",".join(unique)})
    prices = _get_json(get, PRICE_URL, api_key=api_key, params={"ids": ",".join(unique)})
    if not isinstance(rows, list) or not isinstance(prices, dict):
        raise TokenDenied("holding metadata unavailable")
    by_mint = {row.get("id"): row for row in rows if isinstance(row, dict)}
    result = []
    for mint in unique:
        row = by_mint.get(mint)
        price_row = prices.get(mint)
        if not row or not isinstance(price_row, dict) or row.get("tokenProgram") != TOKEN_PROGRAM:
            continue
        decimals = row.get("decimals")
        try:
            price = float(price_row["usdPrice"])
        except (KeyError, TypeError, ValueError):
            continue
        if isinstance(decimals, bool) or not isinstance(decimals, int) or price <= 0:
            continue
        ticker = str(row.get("symbol") or "UNKNOWN")[:24]
        result.append({
            "symbol": mint, "ticker": ticker, "name": str(row.get("name") or ticker)[:80],
            "mint": mint, "decimals": decimals, "entry_eligible": False,
            "market": {"latest_usd": price, "latest_ts": now, "samples": 2,
                       "return_5m_pct": 0.0, "return_1h_pct": 0.0},
        })
    return result


def verify_round_trip(mint: str, cfg: dict, *, api_key: str, get=httpx.get) -> dict:
    params = {
        "inputMint": USDC_MINT, "outputMint": mint, "amount": "1000000",
        "slippageBps": "50", "restrictIntermediateTokens": "true",
    }
    buy = _get_json(get, QUOTE_URL, api_key=api_key, params=params)
    try:
        token_out = int(buy["outAmount"])
        buy_impact = float(buy["priceImpactPct"])
    except (KeyError, TypeError, ValueError) as exc:
        raise TokenDenied("invalid buy quote") from exc
    sell_params = {
        "inputMint": mint, "outputMint": USDC_MINT, "amount": str(token_out),
        "slippageBps": "50", "restrictIntermediateTokens": "true",
    }
    sell = _get_json(get, QUOTE_URL, api_key=api_key, params=sell_params)
    try:
        usdc_back = int(sell["outAmount"])
        sell_impact = float(sell["priceImpactPct"])
    except (KeyError, TypeError, ValueError) as exc:
        raise TokenDenied("invalid sell quote") from exc
    loss_pct = (1 - usdc_back / 1_000_000) * 100
    maximum = float(_settings(cfg).get("maximum_round_trip_loss_pct", 2.0))
    if token_out <= 0 or usdc_back <= 0 or max(buy_impact, sell_impact) > 0.0075 or loss_pct > maximum:
        raise TokenDenied("round trip is not safely sellable")
    return {"buy_output_atomic": token_out, "usdc_back_atomic": usdc_back,
            "round_trip_loss_pct": round(loss_pct, 6)}


def verify_onchain_mint(mint: str, expected_decimals: int, rpc_url: str, *, post=httpx.post) -> int:
    """Independently verify the live mint account using Solana RPC."""
    response = post(
        rpc_url,
        json={"jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
              "params": [mint, {"encoding": "jsonParsed", "commitment": "confirmed"}]},
        timeout=30,
    )
    if response.status_code != 200:
        raise TokenDenied(f"Solana RPC HTTP {response.status_code}")
    try:
        value = response.json()["result"]["value"]
        parsed = value["data"]["parsed"]
        info = parsed["info"]
    except (KeyError, TypeError, ValueError) as exc:
        raise TokenDenied("on-chain mint account unavailable") from exc
    if value.get("owner") != TOKEN_PROGRAM or parsed.get("type") != "mint":
        raise TokenDenied("on-chain token program is unsupported")
    if info.get("mintAuthority") is not None or info.get("freezeAuthority") is not None:
        raise TokenDenied("on-chain token authority remains enabled")
    decimals = info.get("decimals")
    if isinstance(decimals, bool) or decimals != expected_decimals:
        raise TokenDenied("on-chain token decimals mismatch")
    return decimals
