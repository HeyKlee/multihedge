# FINDING 013: the route monitor had three bugs; all made cost look free

Date: 2026-09-29
Status: fixed in code, historical rows are invalid
Author: XORA (Claude Opus 4.8)
Relates to: the council review of task 1, which blocked lowering the 1.8% cost

## Summary

`ops/route_monitor.py` populated `mh_route_observations` with values that could
not be used as cost measurements. Three separate bugs combined, and the net
effect was that a large fraction of the friction on both sides of a trade was
never counted. The council was right to block the cost change, and it was right
for a reason I had not found: the instrument was broken, not merely optimistic.

Every `roundtrip_pct` value written before 2026-09-29 is invalid and must not be
read as a cost. The table is append-only, so the bad rows are retained as
evidence and are distinguished by timestamp, not deleted.

## Bug 1: the sell leg was sized in dust

```python
amt = int(usd * 10 ** dec)     # SELL leg
```

This multiplied the USD notional by the token's decimal scale instead of
converting USD into tokens. The resulting token *count* looked enormous, but its
USD *value* was `usd * px`:

| intent | token price | value actually requested |
|---|---|---|
| $2.00 sell | $0.0000017 | $0.0000034 |
| $2.00 sell | $0.27 | $0.54 |
| $10.00 sell | $0.27 | $2.70 |

A dust sell barely moves an AMM pool, so Jupiter returned a price essentially at
the reference. Measured sell slippage sat at a median of +0.010%, which is the
signature of a leg that was not actually quoting.

Fixed by deriving the token amount from the observed mid price:
`amt = int((usd / mid) * 10 ** dec)`.

## Bug 2: slippage signs were inverted

```python
bs = (mid - buy) / mid * 100.0    # negative when you PAY more
ss = (sell - mid) / mid * 100.0    # negative when you RECEIVE less
```

Both stored the negation of the cost. Every consumer that read these as a
positive cost was reading a negative number. Fixed to
`bs = (buy - mid) / mid` and `ss = (mid - sell) / mid`.

## Bug 3: the round trip was a geometric ratio that cancels itself

```python
rt = ((buy * sell / (mid * mid)) - 1.0) * 100.0
```

This is the severe one. Round-trip cost is the arithmetic spread you must cross
to buy then sell the same notional:

```python
rt = (buy - sell) / mid * 100.0
```

The geometric form multiplies the two legs together, so the premium paid on the
buy cancels the discount taken on the sell. An entirely ordinary 2% round trip
(buy 1% above mid, sell 1% below) evaluated to **-0.010%: free**.

Confirmed on a live row. For mint `PerPsCe2SJ7Q` at $2:

| | reported | actual |
|---|---|---|
| stored `roundtrip_pct` | +0.092% | |
| recomputed from raw prices | | **+1.928%** |

A 1.9% cost was recorded as 0.09%, a 21x understatement, on a row that passed
the plausibility guard and was counted as a successful route.

## Measured cost after the fix

Recomputed from raw `mid_px`/`buy_px`/`sell_px`, which is independent of the
stored column:

| population | n | median | mean | p75 | p90 | max |
|---|---|---|---|---|---|---|
| all rows | 277 | +0.212% | +0.411% | +0.852% | +1.799% | +3.988% |
| pre-fix (sell was dust) | 249 | +0.274% | +0.419% | +0.871% | +1.799% | +3.988% |
| **post-fix (two-sided)** | 28 | **+0.054%** | +0.340% | +0.248% | +1.909% | +1.945% |

The post-fix sample is small (28 observations) and 32% of its rows are negative.
A negative round-trip cost is impossible in a real trade, which means the
Jupiter `usdPrice` reference used as `mid_px` is not a fair mid. Taking only the
non-negative rows, the median is **+0.176%**.

## Why this is not yet grounds to lower the 1.800% charge

The instrument is now correct, but the sample is not sufficient:

- 28 two-sided observations across 8 mints, all within one hour, all at $2 and
  $5. The council's bar was >=100 observations per mint over >=7 days including a
  high-volatility session.
- The reference `mid_px` is demonstrably not a fair mid, so every figure carries
  an unquantified bias of unknown sign. Negative costs must be clamped at zero
  before use.
- The tail matters more than the median for a strategy whose best gross edge is
  around +0.7%. Observed p90 is +1.9% and the worst mints are near +2%, so sizing
  on the median would understate the cost of the trades that actually hurt.

The canonical 1.800% stays in place. What changed is that the evidence needed to
challenge it is now being gathered by a correct instrument, and the first
two-sided sample points at a real median cost far below 1.8% with a tail that
reaches it.

## Method note

The council's Skeptic and Arbiter both warned that the observed friction was
"structurally biased low" and refused to accept it. They were right, but for a
different reason than either stated: not unrepresentative sampling, but an
instrument that measured dust on the sell side and then cancelled the two legs
against each other. The lesson generalises. A measurement that disagrees with a
deliberately conservative prior deserves suspicion of the instrument first, and
the way to settle it is to read the code that produced the number rather than to
argue about the number.
