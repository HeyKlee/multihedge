"""
Memecoin Pump Monitor – scans mh_whale_events for whale buys of tokens
outside SOL/JUP/ETH, resolves symbol→mint via Jupiter token list,
verifies on-chain safety (mint_authority=null, freeze_authority=null)
via Helius RPC, and pushes a memecoin_tracker bias.

Dependencies:
  - pricefeed.mint_for_symbol (Jupiter token list cache, refreshed hourly)
  - Helius RPC (SOLANA_RPC env var) for on-chain checks
"""
import os, json, time, logging, sqlite3
from pathlib import Path
try:
    from solders.pubkey import Pubkey
    from solana.rpc.api import Client
except ImportError:
    class _UnavailablePubkey:
        @staticmethod
        def from_string(_mint):
            raise RuntimeError("Solana dependencies unavailable")

    class _UnavailableClient:
        def get_account_info_json_parsed(self, *_args, **_kwargs):
            raise RuntimeError("Solana dependencies unavailable")

    Pubkey = _UnavailablePubkey
    Client = None
from pricefeed import mint_for_symbol

# Helius RPC endpoint (injected via env var SOLANA_RPC)
RPC_URL = os.getenv('SOLANA_RPC', 'https://api.mainnet-beta.solana.com')
client = Client(RPC_URL) if Client is not None else _UnavailableClient()

# Config
INTERVAL = int(os.getenv('MEME_POLL_INTERVAL', '30'))   # seconds between scans
MIN_TX_SOL = float(os.getenv('MEME_MIN_TX_SOL', '0.5')) # ignore dust buys < this SOL

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(message)s')

def _log(msg, lvl="INFO"):
    print(f"[{time.strftime('%H:%M:%S')}] [{lvl}] {msg}", flush=True)


def _seen_event_ids():
    """Track which mh_whale_events ids have already been pushed as signals."""
    con = sqlite3.connect(Path(__file__).parent / "multihedge.db", check_same_thread=False, timeout=30)
    cur = con.execute(
        "SELECT DISTINCT CAST(substr(rationale, instr(rationale, 'event ')+6) AS INTEGER) "
        "FROM mh_news_bias WHERE provider='memecoin_tracker'")
    seen = {row[0] for row in cur.fetchall() if row[0]}
    con.close()
    return seen


def _is_safe_token(mint: str) -> bool:
    """On-chain safety gate via Helius RPC.
    Returns True iff:
      - mint_authority == None (cannot mint more)
      - freeze_authority == None (cannot freeze accounts)
    (Additional checks like LP burn or top-holder concentration can be added later.)"""
    try:
        resp = client.get_account_info_json_parsed(Pubkey.from_string(mint))
        val = getattr(resp, "value", None)
        if not val:
            return False
        data = val.data.parsed.get("info", {})
        mint_auth = data.get("mintAuthority")
        freeze_auth = data.get("freezeAuthority")
        return (str(val.owner) == "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA" and val.data.parsed.get("type") == "mint" and "mintAuthority" in data and "freezeAuthority" in data and mint_auth is None and freeze_auth is None)
    except Exception as e:
        _log(f"safety check RPC error for {mint}: {e}", "WARN")
        return False  # fail-closed: if we can't verify, treat as unsafe


def _resolve_mint(symbol: str) -> str | None:
    """Try Jupiter token list first, fall back to known core mints."""
    # 1) Jupiter list (~15k symbols)
    mint = mint_for_symbol(symbol)
    if mint:
        return mint
    # 2) Fallback to SOL/JUP/ETH in case list is stale
    CORE = {
        "SOL": "So11111111111111111111111111111111111111112",
        "JUP": "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",
        "ETH": "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs",
    }
    return CORE.get(symbol.upper())


def scan_once():
    """Read recent mh_whale_events and emit a memecoin_tracker bias for any
    whale buy of a token NOT in SOL/JUP/ETH that passes safety checks."""
    seen = _seen_event_ids()
    con = sqlite3.connect(Path(__file__).parent / "multihedge.db", check_same_thread=False, timeout=30)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT * FROM mh_whale_events ORDER BY id DESC LIMIT 200"
    ).fetchall()
    con.close()

    new_count = 0
    for r in rows:
        eid = r["id"]
        if eid in seen:
            continue

        symbol = r["symbol"]
        if not symbol:
            continue

        # Skip core tradeable set (SOL/JUP/ETH) – they have their own strategies
        if symbol.upper() in ("SOL", "JUP", "ETH"):
            continue

        mint = _resolve_mint(symbol)
        if not mint:
            _log(f"skip {symbol}: could not resolve mint", "DEBUG")
            continue

        # Optional: check trade size in SOL if we had tx amount; for now trust whale curator

        if not _is_safe_token(mint):
            _log(f"skip {symbol} ({mint}): failed safety check", "DEBUG")
            continue

        # Passed all filters – push signal
        con = sqlite3.connect(Path(__file__).parent / "multihedge.db", check_same_thread=False, timeout=30)
        con.execute(
            "INSERT INTO mh_news_bias(symbol,direction,confidence,rationale,headlines,provider,ts) "
            "VALUES(?,?,?,?,?,?,?)",
            (symbol, "UP", 0.85,
             f"whale memecoin buy event {eid}",
             "[]",
             "memecoin_tracker", time.time()))
        con.commit()
        con.close()
        _log(f"SIGNAL memecoin {symbol} ({mint}) UP (whale event {eid})")
        seen.add(eid)
        new_count += 1

    return new_count


if __name__ == "__main__":
    _log(f"memecoin monitor up: scan every {INTERVAL}s via {RPC_URL}")
    while True:
        try:
            n = scan_once()
            if n:
                _log(f"emitted {n} new memecoin signal(s)")
        except Exception as e:
            _log(f"scan error: {e}", "ERROR")
        time.sleep(INTERVAL)