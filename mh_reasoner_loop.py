"""
MultiHedge REASONER TRADER LOOP — runs mh_reasoner.tick_symbols() every
TICK_SECS (default 180s = 3 min) so the slow news-swing layer keeps trading
the per-coin bias. Used by supervisord in the container.
"""

import sys
import time

import mh_reasoner

TICK_SECS = int(sys.argv[1]) if len(sys.argv) > 1 else 180


def main():
    print("[mh_reasoner_loop] starting; tick every", TICK_SECS, "s. Ctrl-C to stop.")
    while True:
        try:
            out = mh_reasoner.tick_symbols()
            opens = [x for x in out if x.get("action") == "open"]
            closes = [x for x in out if x.get("action") == "close"]
            if opens or closes:
                print("[mh_reasoner_loop]", {"opens": opens, "closes": closes})
        except Exception as e:
            print("[mh_reasoner_loop] error:", type(e).__name__, str(e)[:140])
        time.sleep(TICK_SECS)


if __name__ == "__main__":
    main()