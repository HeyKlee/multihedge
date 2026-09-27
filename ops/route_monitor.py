#!/usr/bin/env python3
"""Route availability and slippage monitor for MultiHedge.

WHY THIS EXISTS
---------------
The canonical round-trip cost (1.8%) is DECLARED, not measured. A one-off sample
of 4 quotes found that of 12 mints traded, only 4 had any Jupiter route at all
at $1, and NONE routed at $2 or $5. If that ceiling is real, it constrains
position sizing more than any exit threshold does. This measures it over time
instead of guessing from one snapshot.

WHAT IT DOES
------------
For a sample of mints the system has actually traded, ask Jupiter what a real
executable route costs in BOTH directions at several notional sizes, and record:
  * whether a route existed at all
  * the mid price and the executable price for each leg
  * the resulting round-trip slippage versus mid

WHAT IT DOES NOT DO
-------------------
  * submit, sign, or broadcast any transaction
  * write to any trading table (mh_trades, mh_positions, mh_live_inventory)
  * change quote_bps, slippage_bps, or any threshold
  * place an order of any kind

It only asks Jupiter what a route WOULD cost. Read-only against the strategy.

It writes to exactly one new table, mh_route_observations, created idempotently.
The WAL-split rule applies: this MUST be run inside the container, never from
the host, or it will corrupt host readers of the shared database file.
"""
import json
import os
import random
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
JUP_PRICE = "https://api.jup.ag/price/v3"
SIZES = (1.0, 2.0, 5.0, 10.0)
MINT_SAMPLE = 20
TIMEOUT = 12
# Unauthenticated Jupiter throttles at roughly one request per 2 seconds.
# Measured success rate on a single mint: 0.2s pause 1/10, 1.5s 4/10,
# 3.0s 9/10. The 0.2s original produced a phantom 'no route' reading.
PAUSE_S = 3.0
BACKOFF_BASE = 1.5
BACKOFF_JITTER = 0.8
MAX_RETRIES = 4

# A quote implying more slippage than this is not friction, it is a dust or
# near-empty route. Observed: 6GmAFSYs4gk3 returned +99.900% / -99.900%
# symmetric slippage, which is what a nominally-positive-but-worthless
# outAmount produces when divided into a mid price. Recording that as
# measured friction put a -100% round trip into the sample. Real friction on
# a liquid-enough memecoin is single-digit percent; anything past this bound
# is a liquidity fact, not a cost measurement.
MAX_PLAUSIBLE_SLIP_PCT = 50.0

# Decimals discovery is a hidden time sink: without a cached value the probe
# tries up to 10 quote calls per mint, and a sampled run of 20 mints with a
# 3s pause cannot finish inside one session. Cache it after first discovery.
DEC_CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS mh_token_decimals (
    mint     TEXT PRIMARY KEY,
    decimals INTEGER NOT NULL,
    found_ts REAL NOT NULL
)
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS mh_route_observations (
    observed_ts   REAL NOT NULL,
    mint          TEXT NOT NULL,
    usd_notional  REAL NOT NULL,
    mid_px        REAL,
    buy_px        REAL,
    sell_px       REAL,
    buy_slip_pct  REAL,
    sell_slip_pct REAL,
    roundtrip_pct REAL,
    routeable     INTEGER NOT NULL,
    reason        TEXT
)
"""
SCHEMA_IDX = """
CREATE INDEX IF NOT EXISTS ix_route_obs_ts ON mh_route_observations(observed_ts)
"""


class RateLimited(Exception):
    """Jupiter throttled us. Says nothing about whether a route exists."""


class NoRoute(Exception):
    """Jupiter answered, and there is no route. A real liquidity fact."""


class FetchFailed(Exception):
    """Transport failure. Says nothing about liquidity either."""


def _get(url, timeout=TIMEOUT, retries=4):
    """GET JSON with bounded exponential backoff.

    A 429 must NEVER be recorded as "no route". Conflating throttling with
    absent liquidity is what made the first sample of this monitor report a
    fake routing ceiling at $2 and above. Retry, then raise a distinct error.
    """
    key = os.getenv("JUPITER_API_KEY")
    headers = {"User-Agent": "Mozilla/5.0"}
    if key:
        headers["x-api-key"] = key
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            try:
                e.read()
            except Exception:
                pass
            if e.code in (429, 502, 503, 504):
                last = RateLimited(f"http_{e.code}")
                # Respect Retry-After when present, else exponential backoff
                # with jitter so parallel mints do not resynchronise.
                delay = float(e.headers.get("Retry-After") or 0) or (
                    BACKOFF_BASE * (2 ** attempt))
                time.sleep(min(delay + random.uniform(0, BACKOFF_JITTER), 20.0))
                continue
            return {"__http_error__": e.code}
        except Exception as exc:
            last = FetchFailed(type(exc).__name__)
            time.sleep(BACKOFF_BASE * (2 ** attempt) + random.uniform(0, BACKOFF_JITTER))
    if last is None:
        last = FetchFailed("exhausted")
    return {"__failed__": last.args[0]}


def _classify(payload):
    """Turn a raw payload into a priced leg, or raise a specific failure.

    Keeps 'we were throttled' and 'no route exists' strictly separate.
    """
    if payload is None:
        raise FetchFailed("no_response")
    if "__failed__" in payload:
        msg = payload["__failed__"]
        raise RateLimited(msg) if str(msg).startswith("http_") else FetchFailed(msg)
    if "__http_error__" in payload:
        code = payload["__http_error__"]
        raise RateLimited(f"http_{code}") if code in (429, 502, 503, 504) \
            else FetchFailed(f"http_{code}")
    if "outAmount" not in payload or "inAmount" not in payload:
        raise NoRoute("no_route")
    try:
        ina, outa = float(payload["inAmount"]), float(payload["outAmount"])
    except (TypeError, ValueError):
        raise NoRoute("bad_payload")
    if ina <= 0 or outa <= 0:
        raise NoRoute("zero_amount")
    return ina, outa


def mid_price(mint):
    d = _get(f"{JUP_PRICE}?{urllib.parse.urlencode({'ids': mint})}")
    if not d or "__http_error__" in d:
        return None
    rec = d.get("data", d).get(mint)
    if not isinstance(rec, dict):
        return None
    raw = rec.get("usdPrice", rec.get("price"))
    if raw is None:
        return None
    try:
        p = float(raw)
        return p if p > 0 else None
    except (TypeError, ValueError):
        return None


def decimals_of(mint, con=None):
    """Token decimals, cached after first discovery.

    The brute-force probe below costs up to 10 quote calls. Caching keeps a
    repeated run inside the session budget on an unauthenticated endpoint.
    """
    if con is not None:
        row = con.execute(
            "SELECT decimals FROM mh_token_decimals WHERE mint=?", (mint,)).fetchone()
        if row and isinstance(row[0], int):
            return row[0]
    d = _get(f"{JUP_PRICE}?{urllib.parse.urlencode({'ids': mint})}")
    found = None
    if d and "__http_error__" not in d and "__failed__" not in d:
        rec = d.get("data", d).get(mint)
        if isinstance(rec, dict) and isinstance(rec.get("decimals"), int):
            found = rec["decimals"]
    if found is None:
        for guess in (6, 9, 4, 2, 8, 5, 0, 1, 3, 7):
            try:
                _classify(_get(JUP_QUOTE + "?" + urllib.parse.urlencode(
                    {"inputMint": mint, "outputMint": USDC,
                     "amount": 10 ** guess})))
            except NoRoute:
                continue
            except (RateLimited, FetchFailed):
                # Throttled, not absent. Stop rather than burn the budget
                # guessing, and do NOT record a negative result.
                return None
            found = guess
            break
    if found is not None and con is not None:
        con.execute(
            "INSERT OR REPLACE INTO mh_token_decimals(mint,decimals,found_ts)"
            " VALUES(?,?,?)", (mint, found, time.time()))
        con.commit()
    return found


def _is_infra(reason):
    """True when a failure says nothing about liquidity.

    A throttled or broken request must never be tallied as a missing route.
    This single predicate is the guard against repeating the phantom
    'unrouteable above $1' finding.
    """
    r = (reason or "").lower()
    if not r:
        # An unlabelled failure carries no liquidity information. Default to
        # infrastructure so an unknown reason can never be tallied as a
        # missing route and inflate a phantom ceiling.
        return True
    if r == "ok":
        return False
    return ("rate_limited" in r or "fetch_failed" in r
            or "no_decimals" in r or "no_mid" in r)


def _slips_are_plausible(mid, buy, sell):
    """Reject dust quotes before they become 'measured friction'.

    A route that returns a nominally positive but worthless outAmount yields
    near-symmetric +/-100% slippage. That is a real liquidity fact (the pool is
    empty) but it is NOT a cost measurement, and recording it as one put a
    -100% round trip into the friction sample.

    Returns (plausible, why). 'why' is empty when plausible.
    """
    if mid is None or mid <= 0:
        return False, "no_mid"
    for label, px in (("buy", buy), ("sell", sell)):
        if px is None or px <= 0:
            return False, f"no_{label}_px"
        slip = abs((px - mid) / mid * 100.0)
        if slip > MAX_PLAUSIBLE_SLIP_PCT:
            return False, f"implausible_{label}_{slip:.1f}pct"
    return True, ""


def leg_px(mint, side, usd, dec):
    """Executable USD-per-token for one leg.

    Returns (px, "ok") or (None, reason). A rate-limited or failed fetch is
    reported under its own reason and MUST NOT be counted as a missing route.
    """
    if side == "buy":
        amt = int(usd * 10 ** 6)
        params = {"inputMint": USDC, "outputMint": mint, "amount": amt}
    else:
        amt = int(usd * 10 ** dec)
        params = {"inputMint": mint, "outputMint": USDC, "amount": amt}
    try:
        ina, outa = _classify(
            _get(JUP_QUOTE + "?" + urllib.parse.urlencode(params)))
    except RateLimited as exc:
        return None, f"rate_limited:{exc}"
    except FetchFailed as exc:
        return None, f"fetch_failed:{exc}"
    except NoRoute as exc:
        return None, f"no_route:{exc}"
    try:
        return (ina / outa) if side == "buy" else (outa / ina), "ok"
    except ZeroDivisionError:
        return None, "no_route:zero_amount"


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(
        description="Route availability and slippage monitor for MultiHedge")
    ap.add_argument("--mints", type=int, default=8,
                    help="how many mints to sample (default 8; a full 20-mint "
                         "run exceeds one session on an unauthenticated key)")
    ap.add_argument("--sizes", type=float, nargs="+", default=[1.0, 2.0, 5.0],
                    help="USD notionals to probe")
    ap.add_argument("--pause", type=float, default=PAUSE_S,
                    help="seconds between legs (Jupiter throttles near 1/2s)")
    args = ap.parse_args(argv)
    sizes = tuple(args.sizes)

    if not os.path.exists(DB):
        print(f"FATAL: {DB} not found. This must run INSIDE the container.")
        return 2
    con = sqlite3.connect(DB, timeout=15.0)
    con.execute(SCHEMA)
    con.execute(SCHEMA_IDX)
    con.execute(DEC_CACHE_SCHEMA)
    con.commit()

    mints = [r[0] for r in con.execute(
        "SELECT mint FROM mh_scalp_excursions GROUP BY mint "
        "ORDER BY COUNT(*) DESC LIMIT ?", (args.mints,))]

    now = time.time()
    rows = []
    print(f"sampling {len(mints)} mints at sizes {sizes}, {args.pause}s pause")
    print("read-only against the strategy; writes only to mh_route_observations\n")

    for i, mint in enumerate(mints, 1):
        dec = decimals_of(mint, con)
        if dec is None:
            rows.append((now, mint, 0.0, None, None, None, None, None, None, 0,
                         "no_decimals"))
            print(f"  {i:>2}/{len(mints)} {mint[:12]} SKIP no decimals")
            continue
        mid = mid_price(mint)
        for usd in sizes:
            buy, br = leg_px(mint, "buy", usd, dec)
            sell, sr = leg_px(mint, "sell", usd, dec)
            ok = buy is not None and sell is not None
            bs = ss = rt = None
            reason = "ok" if ok else f"buy:{br}/sell:{sr}"
            # Guard all three values explicitly. A None mid with a good route
            # would make the slippage arithmetic raise, and a partially-solved
            # leg must never be recorded as a measurement.
            if ok and mid is not None and buy is not None and sell is not None:
                plausible, why = _slips_are_plausible(mid, buy, sell)
                if not plausible:
                    # Real liquidity fact, but not a cost measurement. Do not
                    # let a dust quote become a -100% friction datapoint.
                    ok = False
                    reason = f"no_route:{why}"
                else:
                    bs = (mid - buy) / mid * 100.0
                    ss = (sell - mid) / mid * 100.0
                    rt = ((buy * sell / (mid * mid)) - 1.0) * 100.0
            else:
                ok = False
                reason = f"{reason}/no_mid" if mid is None else reason
            rows.append((now, mint, usd, mid, buy, sell, bs, ss, rt,
                         1 if ok else 0, reason))
            if ok:
                print(f"  {i:>2}/{len(mints)} {mint[:12]} ${usd:<6.2f} "
                      f"rt {rt:+.3f}%  (buy {bs:+.3f}% sell {ss:+.3f}%)")
            elif "rate_limited" in reason or "fetch_failed" in reason:
                # Not a liquidity fact. Must never be tallied as a missing route.
                print(f"  {i:>2}/{len(mints)} {mint[:12]} ${usd:<6.2f} "
                      f"THROTTLED/FAILED  ({reason})")
            else:
                print(f"  {i:>2}/{len(mints)} {mint[:12]} ${usd:<6.2f} "
                      f"NO ROUTE  ({reason})")
            time.sleep(args.pause)

    con.executemany(
        "INSERT INTO mh_route_observations(observed_ts,mint,usd_notional,mid_px,"
        "buy_px,sell_px,buy_slip_pct,sell_slip_pct,roundtrip_pct,routeable,reason)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()

    total = len(rows)
    # Partition outcomes into three disjoint classes. Collapsing the second
    # into the third is what produced a phantom routing ceiling in the first run.
    routed = [r for r in rows if r[9]]
    no_route = [r for r in rows if not r[9] and not _is_infra(r[10])]
    infra = [r for r in rows if not r[9] and _is_infra(r[10])]
    decidable = len(routed) + len(no_route)

    print("\n" + "=" * 72)
    print(f"recorded {total} observations")
    print(f"  routeable            {len(routed):>4}")
    print(f"  no route (liquidity) {len(no_route):>4}")
    print(f"  throttled/failed     {len(infra):>4}   <- NOT a liquidity signal")
    if decidable:
        print(f"  route rate of decidable: {100*len(routed)/decidable:.1f}% "
              f"({len(routed)}/{decidable})")
    else:
        print("  route rate: UNDETERMINED, every observation was throttled or failed")
    print("=" * 72)

    print(f"\n  {'size':>8}{'routed':>9}{'no route':>10}{'infra':>8}"
          f"{'median rt':>12}{'worst rt':>11}")
    for usd in sizes:
        sub = [r for r in rows if r[2] == usd]
        n_ok = sum(1 for r in sub if r[9])
        n_nr = sum(1 for r in sub if not r[9] and not _is_infra(r[10]))
        n_in = sum(1 for r in sub if not r[9] and _is_infra(r[10]))
        rt_ok = [r[8] for r in sub if r[9] and r[8] is not None]
        med = f"{st.median(rt_ok):+.3f}%" if rt_ok else "-"
        worst = f"{max(rt_ok):+.3f}%" if rt_ok else "-"
        print(f"  ${usd:<7.2f}{n_ok:>9}{n_nr:>10}{n_in:>8}{med:>12}{worst:>11}")

    allrt = [r[8] for r in routed if r[8] is not None]
    if allrt:
        print(f"\n  overall median round trip : {st.median(allrt):+.3f}%")
        print(f"  overall worst  round trip : {max(allrt):+.3f}%")
        print(f"  declared canonical cost   : 1.8000% charged on entry->exit")
        if max(allrt) < 1.8:
            print("  -> declared cost EXCEEDS worst observed: conservative")
        else:
            print("  -> declared cost is BELOW worst observed: it understates the "
                  "bad tail, which is the conservative-safe direction to fix, "
                  "but the median is far better than declared")
    if not allrt:
        print("\n  no completed round trips this run: no friction verdict is "
              "possible. Throttling, not liquidity.")

    n = con.execute("SELECT COUNT(*) FROM mh_route_observations").fetchone()[0]
    print(f"\n  table now holds {n} observations across all runs")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
