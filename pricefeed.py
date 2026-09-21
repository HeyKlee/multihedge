"""
MultiHedge PRICE FEED — generalized live pricing for ANY Solana SPL token.

Primary: Jupiter price API by MINT (works for any token, not just a hardcoded
symbol set). Secondary: CoinGecko/Binance for well-known symbols. Cached in the
DB (price_cache) with a short TTL so rapid ticks don't hammer APIs. Serves the
last known price on rate-limit instead of failing (stale-is-better-than-gone).

This is a generalization of AutoHedge's paprice.py: the coin universe is no
longer a small hardcoded set, it is whatever mints are configured. The by-mint
Jupiter path was already generic; this module makes it first-class.
"""

import base64
import json
import math
import os
import sqlite3
import time
import urllib.parse
import urllib.request
from pathlib import Path
from collections import deque

DB_PATH = Path(__file__).parent / "multihedge.db"
CACHE_TTL = 30          # seconds: serve cached price within this window

# CoinGecko symbol -> id (only the ones we know; everything else goes Jupiter)
COINGECKO_IDS = {
    "SOL": "solana", "BTC": "bitcoin", "ETH": "ethereum",
    "BONK": "bonk", "JUP": "jupiter-exchange-solana",
    "WIF": "dogwifcoin", "USDC": "usd-coin",
}
BINANCE_SYMBOLS = {
    "SOL": "SOLUSDT", "BTC": "BTCUSDT", "ETH": "ETHUSDT", "JUP": "JUPUSDT",
    "BONK": "BONKUSDT", "USDC": "USDCUSDT",
}

# ---- Live USD -> NZD rate (display layer) --------------------------------
FX_TTL = 3600          # re-fetch FX at most once/hour
FX_DEFAULT_NZD_PER_USD = 1.67   # fallback ~current NZD/USD if the feed is down
FX_JS_URLS = [
    "https://open.er-api.com/v6/latest/USD",
]
def nzd_per_usd() -> float:
    """Live NZD per 1 USD for denoinating wallet display. Falls back to cached
    value, then to a sensible default. Never raises."""
    cached = _cached("fx:NZDUSD", FX_TTL)
    if cached is not None:
        return cached
    rate = None
    for url in FX_JS_URLS:
        data = _get(url)
        if data and isinstance(data, dict):
            rates = data.get("rates") or {}
            nzd = rates.get("NZD")
            if nzd:
                try:
                    rate = float(nzd)
                    break
                except (TypeError, ValueError):
                    rate = None
    if rate and rate > 0:
        _set_cache("fx:NZDUSD", rate)
        return rate
    # feed down: fall back to any cached value, else default
    con = _connect()
    row = con.execute("SELECT px FROM price_cache WHERE key='fx:NZDUSD'").fetchone()
    con.close()
    return row["px"] if row else FX_DEFAULT_NZD_PER_USD


_JUP_PRICE = "https://api.jup.ag/price/v3"
_JUP_TOKEN_LIST = "https://token.jup.ag/all"
_JUP_TOKEN_TTL = 3600   # refresh symbol->mint cache hourly

# Module-level lazy cache: symbol -> mint address.
# ~15k SPL tokens including most liquid memecoins. Refreshed hourly.
_JUP_TOKEN_CACHE: dict[str, str] = {}
_JUP_TOKEN_LOADED_AT: float = 0.0


def _load_jup_token_map(force: bool = False) -> dict[str, str]:
    """Download + cache Jupiter's full token list (~15k SPL tokens).

    Returns a `symbol -> mint` map (uppercase symbols). Filters out obvious
    dust/scam tokens by requiring a non-empty name and decimal field. Refreshes
    every hour or on `force=True`."""
    global _JUP_TOKEN_CACHE, _JUP_TOKEN_LOADED_AT
    now = time.time()
    if not force and _JUP_TOKEN_CACHE and now - _JUP_TOKEN_LOADED_AT < _JUP_TOKEN_TTL:
        return _JUP_TOKEN_CACHE
    try:
        data = _get(_JUP_TOKEN_LIST, timeout=15)
        if not isinstance(data, list):
            return _JUP_TOKEN_CACHE
        sym_to_mint = {}
        for t in data:
            sym = (t.get("symbol") or "").upper()
            mint = t.get("address")
            name = t.get("name") or ""
            decimals = t.get("decimals")
            # basic sanity filter: valid Solana mint length, has decimals, has name
            if (sym and mint and len(mint) >= 32
                    and isinstance(decimals, int) and 0 < decimals < 12
                    and name and sym not in sym_to_mint):
                sym_to_mint[sym] = mint
        _JUP_TOKEN_CACHE = sym_to_mint
        _JUP_TOKEN_LOADED_AT = now
        # log it once
        print(f"[pricefeed] loaded Jupiter token list: {len(sym_to_mint)} symbols",
              flush=True)
    except Exception as e:
        print(f"[pricefeed] jupiter token list load failed: {e}", flush=True)
    return _JUP_TOKEN_CACHE


def mint_for_symbol(symbol: str) -> str | None:
    """Resolve a token SYMBOL to its canonical Solana MINT via Jupiter's list.

    Cached hourly. Returns None if the symbol isn't recognized."""
    if not symbol:
        return None
    return _load_jup_token_map().get(symbol.upper())


def _connect():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute(
        "CREATE TABLE IF NOT EXISTS price_cache (key TEXT PRIMARY KEY, px REAL, ts REAL)")
    return con


def _cached(key, ttl=CACHE_TTL):
    con = _connect()
    row = con.execute("SELECT px, ts FROM price_cache WHERE key=?", (key,)).fetchone()
    con.close()
    if row and time.time() - row["ts"] < ttl:
        return row["px"]
    return None


def _set_cache(key, px):
    con = _connect()
    con.execute(
        "INSERT INTO price_cache(key,px,ts) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET px=excluded.px, ts=excluded.ts",
        (key, px, time.time()))
    con.commit()
    con.close()


# ---- In-memory price history for RSI and volume calculations ----
# Each mint gets a deque of (ts, price, buy_vol, sell_vol) tuples, max 100 entries
_PRICE_HISTORY: dict[str, deque] = {}
_MAX_HISTORY_LEN = 100
_PRICE_HISTORY_DIR = Path(__file__).parent / "price_history"

def _update_price_history(mint: str, ts: float, price: float, buy_vol: float = 0, sell_vol: float = 0):
    """Append a price/volume sample to the mint's history."""
    _ensure_price_history_loaded(mint)
    if mint not in _PRICE_HISTORY:
        _PRICE_HISTORY[mint] = deque(maxlen=_MAX_HISTORY_LEN)
    _PRICE_HISTORY[mint].append((ts, price, buy_vol, sell_vol))
    # Persist periodically (every 10 updates)
    if True:  # persist on every update for reliable RSI/volume history
        _persist_price_history(mint)

def _persist_price_history(mint: str):
    """Save mint's price history to disk."""
    try:
        _PRICE_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        hist = _PRICE_HISTORY.get(mint)
        if hist:
            data = {
                "mint": mint,
                "updated": time.time(),
                "history": list(hist)
            }
            import json
            path = _PRICE_HISTORY_DIR / f"{mint}.json"
            path.write_text(json.dumps(data))
    except Exception as e:
        print(f"[pricefeed] persist price history failed for {mint}: {e}", flush=True)

def _load_price_history(mint: str) -> list | None:
    """Load mint's price history from disk."""
    try:
        path = _PRICE_HISTORY_DIR / f"{mint}.json"
        if path.exists():
            import json
            data = json.loads(path.read_text())
            return data.get("history", [])
    except Exception as e:
        print(f"[pricefeed] load price history failed for {mint}: {e}", flush=True)
    return None

def _ensure_price_history_loaded(mint: str):
    """Load history from disk if not in memory."""
    if mint not in _PRICE_HISTORY:
        hist_data = _load_price_history(mint)
        if hist_data:
            _PRICE_HISTORY[mint] = deque(hist_data, maxlen=_MAX_HISTORY_LEN)

def compute_rsi_14(mint: str, current_ts: float) -> float | None:
    """Compute RSI(14) on 15m intervals from price history."""
    _ensure_price_history_loaded(mint)
    hist = _PRICE_HISTORY.get(mint)
    if not hist or len(hist) < 15:
        return None
    
    # Filter to last ~15m window and compute price changes
    recent = [p for p in hist if current_ts - p[0] <= 900]  # 15 minutes = 900 seconds
    if len(recent) < 15:
        return None
    
    prices = [p[1] for p in recent]
    deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    
    gains = [d for d in deltas if d > 0]
    losses = [-d for d in deltas if d < 0]
    
    avg_gain = sum(gains) / 14 if gains else 0.0
    avg_loss = sum(losses) / 14 if losses else 0.0
    
    if avg_loss == 0:
        return 100.0
    
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

def compute_volume_avg_20(mint: str) -> float | None:
    """Compute 20-period average of 5m volume (buy + sell)."""
    _ensure_price_history_loaded(mint)
    hist = _PRICE_HISTORY.get(mint)
    if not hist or len(hist) < 20:
        return None
    
    volumes = [p[2] + p[3] for p in hist][-20:]  # Last 20 periods
    return sum(volumes) / len(volumes)


def _get(url, timeout=5, headers=None):
    for attempt in range(1):
        try:
            req = urllib.request.Request(url, headers=headers or {"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except Exception:
            return None
    return None


def _jupiter_mint(mint):
    """Jupiter price by mint address — generic for any SPL token."""
    if not mint or len(mint) < 32:
        return None
    key = os.getenv("JUPITER_API_KEY")
    qs = urllib.parse.urlencode({"ids": mint})
    headers = {"x-api-key": key} if key else {}
    data = _get(_JUP_PRICE + "?" + qs, headers=headers)
    if not data:
        return None
    data = data.get("data", data)
    v = data.get(mint)
    if not isinstance(v, dict):
        return None
    try:
        px = float(v.get("usdPrice", v.get("price")))
        return px if math.isfinite(px) and px > 0 else None
    except (TypeError, ValueError):
        return None


def _coin_gecko(symbol):
    cid = COINGECKO_IDS.get(symbol and symbol.upper())
    if not cid:
        return None
    data = _get("https://api.coingecko.com/api/v3/simple/price?ids=%s&vs_currencies=usd" % cid)
    if data:
        v = data.get(cid, {}).get("usd")
        return float(v) if v is not None else None
    return None


def _binance(symbol):
    bs = BINANCE_SYMBOLS.get(symbol and symbol.upper())
    if not bs:
        return None
    data = _get("https://api.binance.com/api/v3/ticker/price?symbol=%s" % bs)
    if data and data.get("price"):
        return float(data["price"])
    return None


def live_price(mint: str | None = None, symbol: str | None = None) -> float | None:
    """Best-effort live USD price for a coin by mint and/or symbol.

    Order: fresh cache -> Jupiter by mint (generic) -> CoinGecko -> Binance.
    Falls back to any cached value on rate-limit/failure.
    """
    if not mint and not symbol:
        return None
    # Only configured identities may use a symbol-based fallback. Arbitrary
    # token tickers are not unique and must never stand in for a mint.
    from config import COINS
    symbol = symbol.upper() if symbol else None
    known = COINS.get(symbol, {}) if symbol else {}
    if not mint:
        mint = known.get("mint")
    if not mint:
        return None
    key = mint
    cached = _cached(key)
    if cached is not None and math.isfinite(cached) and cached > 0:
        return cached
    px = _jupiter_mint(mint)
    same_identity = known.get("mint") == mint
    if px is None and same_identity:
        px = _binance(symbol)
    if px is None and same_identity:
        px = _coin_gecko(symbol)
    if px is not None and math.isfinite(px) and px > 0:
        _set_cache(key, px)
        # Update price history for RSI/volume calculations
        # Try to get buy/sell volume from market data if available
        _update_price_history(mint, time.time(), px)
        return px
    # No stale cache fallback for executable prices. Display uses pxhist with
    # its original timestamp and explicitly labels stale data.
    return None


def quotes_batch(mints: list[str]) -> dict:
    """Batch USD prices for many mints via Jupiter (single call)."""
    if not mints:
        return {}
    key = os.getenv("JUPITER_API_KEY")
    qs = urllib.parse.urlencode({"ids": ",".join(m for m in mints if m and len(m) >= 32)})
    headers = {"x-api-key": key} if key else {}
    data = _get(_JUP_PRICE + "?" + qs, headers=headers)
    out = {}
    if data:
        data = data.get("data", data)
        for k, v in data.items():
            if isinstance(v, dict) and v.get("usdPrice", v.get("price")) is not None:
                px = float(v.get("usdPrice", v.get("price")))
                if k in mints and math.isfinite(px) and px > 0:
                    out[k] = px
                    # Update price history for RSI/volume calculations
                    _update_price_history(k, time.time(), px)
    return out


if __name__ == "__main__":
    import sys
    mint = sys.argv[1] if len(sys.argv) > 1 else None
    sym = sys.argv[2] if len(sys.argv) > 2 else None
    px = live_price(mint, sym)
    print("price:", None if px is None else round(px, 8))