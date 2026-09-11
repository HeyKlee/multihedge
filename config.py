"""
MultiHedge config helpers — shared access to the coin universe.

Reads config.yaml into a COINS dict: {symbol: {name, mint, symbol}} so every
module (loom, news reasoner, reasoner trader, dashboard) uses the SAME universe.
"""

from pathlib import Path

import yaml

CFG_PATH = Path(__file__).parent / "config.yaml"


def _load() -> dict:
    return yaml.safe_load(CFG_PATH.read_text(encoding="utf-8")) or {}


def _coins_map() -> dict:
    cfg = _load()
    out = {}
    for c in cfg.get("coins", []):
        sym = c.get("symbol")
        if sym:
            out[sym] = {"symbol": sym, "name": c.get("name", sym), "mint": c.get("mint", "")}
    return out


COINS = _coins_map()

if __name__ == "__main__":
    import json
    print(json.dumps(COINS, indent=2, default=str))