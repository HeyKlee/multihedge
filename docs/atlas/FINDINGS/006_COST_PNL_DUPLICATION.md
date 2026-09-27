# PROJECT ATLAS — FINDING 006: cost/PnL is recomputed in 14 independent places

**Status:** VERIFIED (council Seat 3 + chair independent verification)
**Severity:** Critical — Rule D (one cost/accounting engine) is entirely absent
**Also contains:** a correction to a claim the chair made earlier in this project

## Part 1 — 14 rival cost/PnL implementations

Rule D requires one accounting contract. Actual:

| # | Site | What it does | Diverges how |
|---|---|---|---|
| 1 | `execution_costs.py:125` | `(total_bps*2)/10000` | **the canonical cost owner** |
| 2 | `paper.py:72` | `net = gross - round_trip_cost(cfg)` | uses #1 |
| 3 | `grid_trader.py:305` | `gross*(1-bps/10000)` | **single leg, ignores slippage** |
| 4 | `grid_trader.py:419` | `sum(realized) - fee*cycles` | **fixed per-cycle fee model** |
| 5 | `parameter_autotuner.py:161` | `tp - cost`, `price/entry-1-cost` | own cost deduction |
| 6 | `continuous_optimizer.py:45,59,60` | own gross/cost split | **never calls `paper.net_realized`** |
| 7 | `dynamic_shadow_scalper.py:306` | `price/entry - 1` | **algebraically different form** |
| 8 | `mh_reasoner.py:266` | own gross then `paper.net_realized` | duplicates the gross half |
| 9 | `mh_whale_trader.py:156` | own long/short gross | duplicates |
| 10 | `mh_memecoin_trader.py:107` | long-only gross | **no short branch** |
| 11 | `live_inventory.py:328` | live realized PnL | separate venue |
| 12 | `live_bridge.py:301` | live proceeds | duplicates #11 |
| 13 | `mh_dash.py:228` | recomputes realized PnL from rows | **projection recomputing truth** |
| 14 | `ops/sampled_price_replay.py:153` | external friction model | deliberately avoids double-count |

The gross-return formula alone has **6 variants** (`paper.py:299/301`, `mh_reasoner.py:266`,
`mh_whale_trader.py:156`, `mh_memecoin_trader.py:107`, `dynamic_shadow_scalper.py:306`).
Two of them differ structurally: `dynamic_shadow_scalper.py:306` computes
`price/entry - 1`, which is not the same expression as `(exit-entry)/entry`.

**Consequence:** a research result, a dashboard number and a paper trade can each be
"correct" under their own formula and still disagree. No single value is the answer to
"what did this trade actually make".

## Part 2 — one table, 17 writer modules

`mh_trades` (2,216 rows) is written by `paper.py`, `mh_reasoner.py`, `mh_whale_trader.py`,
`mh_memecoin_trader.py`, `dynamic_shadow_scalper.py`. `mh_accounts` (5 rows) is written by
**9 modules**. Roughly 17 modules write the single production DB file, and 3 host-side
tools write that same file from outside the container, which is the known WAL-split
corruption vector (`incident-20260925-walsplit/`).

## Part 3 — CORRECTION to an earlier chair claim

Earlier in this project the chair reported:

> "no DB writes outside the filter (DB verified read-only)"

That was wrong. Verified now:

```
SELECT COUNT(*) FROM mh_entry_friction_quotes;  ->  11 rows in PRODUCTION
```

The friction gate **does** write to the production database, by design: it caches quote
proofs for 300s.

What was checked and is **correct**:

- `live_signer_worker.py:92` (the live BUY path) passes **no** `db_path`, so the signer
  never reads or writes the cache. Every live BUY takes a fresh quote.
- `solana_token_universe.py:259` and `dynamic_shadow_scalper.py:566` pass the production
  `db_path`, so the two paper paths populate and reuse the cache.
- Cache reuse is fail-closed (`entry_friction.py:80-83`): a cached result is reused only
  if it is `BLOCKED`, or if `proof_allows()` re-validates mint, amount, cost, threshold
  and 300s freshness. A cached `ALLOWED` can never bypass a threshold change, because the
  reuse is additionally gated on `cached['threshold_pct'] == result['threshold_pct']`.

So the security posture is sound; the *reporting* was not. The correction is recorded here
rather than quietly amended.

## Required fix (Rule D + Rule F)

1. One `accounting` module owns gross/cost/net/PnL. Every trader, replay, backtest,
   autotuner and **dashboard** consumes it. The dashboard may format, never recompute.
2. `grid_trader` must either adopt the canonical cost model or document, in code, why a
   different venue mechanic makes a different model correct. Right now it is ambiguous.
3. `mh_memecoin_trader` missing the short branch is either a bug or an undeclared
   long-only strategy. It must be one or the other explicitly.
4. `mh_accounts` needs a single owning service; 9 writers is a correctness risk regardless
   of intent.
5. `mh_entry_friction_quotes` must be declared in the schema registry with an owner and
   retention policy, like every other table.

## Ordering note

This finding outranks the DB-path work in business impact. Duplicate accounting is why
the earlier 8-hour simulation could not produce trustworthy P&L in the first place: it
was reading the wrong database, and no definition of "profit" would have been consistent
across the components that produce the same number.
