# CORRECTION: the "unroutable tokens" finding was a measurement bug, not a market fact

**Date:** 2026-09-28
**Corrects:** my earlier statement that "3 of your last 5 entries were on tokens with no
sell route (`implausible_buy_99.9pct`)" and that this was "independent of the remapping work".

## What I claimed

I reported that 3 of 5 real entries were on tokens the router could not sell, citing
`mh_route_observations` reasons `no_route:implausible_buy_99.9pct` and `899.4pct`. I
framed it as a live trading problem costing real money, and recommended it as the highest
priority item.

## What was actually true

The defect was in the **measurement code**, not the market. Commit `bbc2817` (authored by
XORA, not by me, at 12:55) fixes `ops/route_monitor.py`:

```python
-        return (ina / outa) if side == "buy" else (outa / ina), "ok"
+        scale = 10 ** (dec - USDC_DECIMALS)
+        if side == "buy":
+            return (ina / (outa / scale)), "ok"
+        return (outa / (ina / scale)), "ok"
```

`ina` is USDC-atomic (6 decimals) and the token leg is in the mint's own decimals. A raw
ratio therefore carries `10**(dec-6)` of extra scale, so **every 9-decimal memecoin was
measured ~1000x too cheap** and stamped `no_route:implausible_buy_99.9pct`.

The commit message documents RED/GREEN verification against the real production value:
old logic `0.000256919`, new logic `0.2569197` for `6GmAFSYs4gk3` — a 1000x correction.

## Correct statement

Those tokens were **not** proven unroutable. They were mis-measured by a decimal-scale
defect in a monitoring script. I presented a bug in my own diagnostics tooling as a
market fact, and I did so while telling you the system could not trust its own numbers.

The current distribution is consistent with that:

```
routeable=1, reason='ok'                                   249 rows
routeable=0, reason='no_route:implausible_buy_99.9pct'     21 rows   <- pre-fix artifacts
routeable=0, reason='buy:http_429/sell:http_429'            15 rows   <- rate limiting
routeable=0, reason='no_decimals'                           12 rows
```

## What this changes

1. The `no_route:*` rows still in the database are **pre-fix artifacts** and must not be
   read as current evidence. They are not corrected retroactively.
2. My "highest priority, costing real money right now" framing was wrong. The 20% take
   profit decision (FINDING 007) is the genuinely urgent item, not routing.
3. This is the **third** time in this session a tool result was reported as a market fact
   when it was actually a defect in the measuring instrument: the 8-hour simulation
   (synthetic mints), the 12-day window presented as 8 hours, and now the decimal scale.
   The common cause is that diagnostic code was treated as authoritative. That is a
   direct instance of the problem Project ATLAS exists to fix, and it was mine.

## Still true

The **live** routing path is unaffected. `solana_token_universe.verify_round_trip` uses
Jupiter's quoted `outAmount` directly and is validated by quote-identity checks; it never
performed the atomic-unit division that was defective. The entry-friction gate runs through
that path, so entry costs were measured correctly. Only `ops/route_monitor.py` had the
defect, and only its persisted `no_route:*` rows are suspect.
