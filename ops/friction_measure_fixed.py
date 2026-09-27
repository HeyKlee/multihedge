#!/usr/bin/env python3
"""Corrected round-trip friction measurement.

Fixes three defects in the original route monitor's friction figure:

1. SIGN. The original stored rt = (buy*sell/mid^2 - 1)*100, which is
   structurally always <= 0 because buy >= mid and sell <= mid for any
   real bid/ask. Negative means COST, not gain. Reading it as a positive
   cost inverted every statistic derived from it.

2. STALE MID. The original compared a live quote against a CACHED mid
   (pricefeed.mid_price). On a fast-moving memecoin the cached mid lags,
   so both legs can look favourable and the round trip appears to pay you.
   That produced 98 of 147 rows with a positive rt, i.e. "free money",
   which is impossible. This version derives the reference price from the
   quote pair itself, so no external cached price enters the arithmetic.

3. NOTIONAL-ROUNDING. The sell leg is quoted for a whole-token amount.
   Sub-token dust makes the sell leg quote badly. This version quotes the
   sell leg for the token amount the buy leg actually produces, so the
   round trip is self-consistent.

The cost reported is the true round trip: buy at the executable ask, sell
at the executable bid, both sized from the same notional.

Read-only with respect to the trading database: writes only to
mh_friction_measurements, a research table, from inside the container.
"""

import json
import os
import sqlite3
import statistics as st
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DB = "/app/multihedge.db"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
JUP_QUOTE = "https://api.jup.ag/swap/v1/quote"
SIZES = (1.0, 2.0, 5.0)

KEY = ""
for _f in ("/app/.env", "/app/agent.env"):
    if os.path.exists(_f):
        for line in open(_f):
            if "JUPITER_API_KEY" in line and "=" in line:
                KEY = line.split("=", 1)[1].strip().strip("'\"")
if not KEY:
    print("  ERROR: no JUPITER_API_KEY found. Cannot measure without auth.")
    sys.exit(1)

THROTTLED = 0
FAILED = 0


def _get(url, tries=3):
    global THROTTLED, FAILED
    for attempt in range(tries):
        req = urllib.request.Request(url)
        req.add_header("x-api-key", KEY)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                THROTTLED += 1
                # Jittered backoff. A fixed sleep here produced a phantom
                # "no route" ceiling in the first run.
                time.sleep(1.5 * (1 + attempt) + 0.4 * (attempt * attempt))
                continue
            FAILED += 1
            return None
        except Exception:
            FAILED += 1
            return None
    return None


def quote(inp, out, amt):
    url = JUP_QUOTE + "?" + urllib.parse.urlencode(
        {"inputMint": inp, "outputMint": out, "amount": int(amt)})
    d = _get(url)
    if not isinstance(d, dict) or "outAmount" not in d:
        return None
    try:
        return int(d["outAmount"])
    except (TypeError, ValueError):
        return None


def decimals_of(mint):
    """Decimals from the cache the route monitor already populates.

    api.jup.ag/token/v2/ now returns 404, and lite-api is Cloudflare-walled
    (403 error code 1010). Falling back to a live token call made every
    quote report NO ROUTE, which would have read as absent liquidity.
    """
    try:
        con = sqlite3.connect(DB, timeout=15)
        row = con.execute(
            "SELECT decimals FROM mh_token_decimals WHERE mint=?", (mint,)
        ).fetchone()
        con.close()
        if row and row[0] is not None:
            return int(row[0])
    except sqlite3.Error:
        pass
    return None


def measure(mint, usd):
    """True round-trip cost in PERCENT, positive = cost.

    Buy `usd` worth of the token, then immediately sell exactly the tokens
    the buy produced. Any shortfall versus `usd` is the friction actually
    paid, expressed in percent of notional.
    """
    dec = decimals_of(mint)
    if dec is None:
        return None
    in_usdc = int(usd * 10 ** 6)
    tok = quote(USDC, mint, in_usdc)
    if not tok or tok <= 0:
        return None
    back = quote(mint, USDC, tok)
    if back is None or back <= 0:
        return None
    out_usdc = back / 10 ** 6
    if out_usdc <= 0:
        return None
    # Positive = cost. A correct measurement is never negative.
    return (usd - out_usdc) / usd * 100.0


def main():
    con = sqlite3.connect(DB, timeout=30)
    con.execute("""CREATE TABLE IF NOT EXISTS mh_friction_measurements (
        measured_ts REAL NOT NULL, mint TEXT NOT NULL, usd_notional REAL NOT NULL,
        roundtrip_cost_pct REAL NOT NULL, PRIMARY KEY(mint, usd_notional))""")
    con.commit()

    mints = [r[0] for r in con.execute(
        "SELECT mint FROM mh_shadow_entry_observations "
        "WHERE mint IN (SELECT mint FROM mh_token_decimals) "
        "GROUP BY mint ORDER BY MAX(observed_ts) DESC LIMIT 20")]
    print(f"  Measuring {len(mints)} mints at sizes {list(SIZES)}")
    print("  Cost is computed from the quote pair only; no cached mid is used.")
    print()

    rows, ok, fail = [], 0, 0
    for i, mint in enumerate(mints, 1):
        for usd in SIZES:
            c = measure(mint, usd)
            if c is None:
                fail += 1
                rows.append((time.time(), mint, usd, None, "no_route"))
                print(f"  {i:>2}/{len(mints)} {mint[:10]} ${usd:<5.2f} NO ROUTE")
            else:
                ok += 1
                rows.append((time.time(), mint, usd, c, "ok"))
                print(f"  {i:>2}/{len(mints)} {mint[:10]} ${usd:<5.2f} "
                      f"cost {c:+.4f}%")
            time.sleep(0.35)
        con.execute("DELETE FROM mh_friction_measurements WHERE mint=?", (mint,))
        con.executemany(
            "INSERT OR REPLACE INTO mh_friction_measurements "
            "(measured_ts,mint,usd_notional,roundtrip_cost_pct) VALUES(?,?,?,?)",
            [(t, m, u, c) for t, m, u, c, r in rows if c is not None and m == mint])
        con.commit()

    v = [c for _, _, _, c, r in rows if c is not None]
    print()
    print("=" * 72)
    print("CORRECTED ROUND-TRIP FRICTION  (positive = cost)")
    print("=" * 72)
    if not v:
        print("  No successful measurements.")
        return 1
    v.sort()
    n = len(v)
    print(f"  successful quotes      {n}   (failed {fail}, throttled {THROTTLED})")
    print(f"  median                 {st.median(v):.4f}%")
    print(f"  mean                   {sum(v)/n:.4f}%")
    print(f"  min                    {v[0]:.4f}%")
    print(f"  p90                    {v[int(n*0.90)]:.4f}%")
    print(f"  max                    {v[-1]:.4f}%")
    neg = sum(1 for x in v if x < 0)
    print(f"  negative values        {neg}  (must be 0; a negative cost is a bug)")
    print()
    print("  by notional:")
    for usd in SIZES:
        s = [c for _, _, u, c, r in rows if c is not None and u == usd]
        if s:
            print(f"    ${usd:<6.2f} n={len(s):<4d} median {st.median(s):.4f}%  "
                  f"max {max(s):.4f}%")
    print()
    print(f"  AMM fee floor is ~0.50%. Measured median {st.median(v):.4f}%.")
    if st.median(v) < 0.45:
        print("  WARNING: median is below the AMM floor. Verify before use.")
    else:
        print("  Median is consistent with the AMM fee floor, as it should be.")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
