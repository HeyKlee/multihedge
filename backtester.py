#!/usr/bin/env python3
"""Back‑test integration helper.

This tiny script rings up the 5‑year‑back‑test JSON files that live in
`backtest-results/5year/` (one per coin‑strategy pair) and copies/moves them into a
structure that the rest of the MultiHedge system expects.  The destination is:

    backtest-results/final/<coin>_<strategy>.json

If the target already contains a file it is overwritten because the
``backtester.py`` run is idempotent – you can safely run it any time a new
back‑test finishes.

The script is laid out for easy audit: it prints a concise summary of how many
files were processed and any errors that came up.

Author: XORA‑AI
"""

import json
import pathlib
import sys

SRC_ROOT = pathlib.Path("backtest-results/5year")
DST_ROOT = pathlib.Path("backtest-results/final")

STRATEGIES = ["momentum_breakout", "mean_reversion", "rsi_oversold", "vwap_reversion"]
COINS = ["SOL", "JUP", "ETH"]


def main():
    DST_ROOT.mkdir(parents=True, exist_ok=True)
    processed = 0
    failed = []
    for coin in COINS:
        for strat in STRATEGIES:
            src = SRC_ROOT / coin.lower() / f"{strat}.json"
            if not src.exists():
                # Might be a legacy naming scheme
                continue
            dst = DST_ROOT / f"{coin.lower()}_{strat}.json"
            try:
                # Copy verbatim – no transformation
                dst.write_text(src.read_text(encoding="utf-8"))
                processed += 1
            except Exception as exc:
                failed.append((str(src), exc))
    print(f"Back‑tester finished – {processed} files copied.")
    if failed:
        print("Errors:")
        for fn, exc in failed:
            print(f"  {fn}: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()