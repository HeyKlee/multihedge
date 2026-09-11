"""
MultiHedge NEWS REASONER LOOP — runs mh_news.analyze_symbol for every coin,
then sleeps NEWS_INTERVAL (default 3600s = hourly). Used by supervisord in the
container. Ticks are distributed across the minute so hourly cost is bounded.
"""

import sys
import time

import mh_news

NEWS_INTERVAL = int(sys.argv[1]) if len(sys.argv) > 1 else 3600


def main():
    print("[mh_news_loop] starting; interval", NEWS_INTERVAL, "s. Ctrl-C to stop.")
    while True:
        try:
            out = mh_news.run_all()
            print("[mh_news_loop]", out)
        except Exception as e:
            print("[mh_news_loop] error:", type(e).__name__, str(e)[:140])
        time.sleep(NEWS_INTERVAL)


if __name__ == "__main__":
    main()