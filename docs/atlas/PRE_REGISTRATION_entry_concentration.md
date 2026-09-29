# PRE-REGISTRATION: entry concentration (task E)

Written 2026-09-29, BEFORE the analysis was run.
Author: XORA (Claude Opus 4.8)

This document exists because two numbers produced earlier in this session were
wrong and both were wrong in the same way: they flattered the conclusion I
wanted. See FINDING 012 (an apparent +9.6% entry edge that was eight sub-cent
tokens) and FINDING 013 (a measured 0.274% round-trip cost produced by an
instrument with three bugs).

The purpose of pre-registration here is to make this analysis falsifiable
without relying on my judgement. If the result is negative, that is a cheap
twenty-minute outcome. If the result is positive, that is exactly the case that
should be treated as suspicious, so the criteria below are fixed in advance and
do not move once I see a number.

## The claim under test

The bought cohort returns about +0.429% gross on trimmed 1-hour labels, against
a best-case replay gross of roughly +0.7% and a conservative round-trip cost of
1.800%. If the gross edge is concentrated in a small fraction of setups, then
trading fewer, better entries could raise expectancy per trade even if total
trade count falls.

## Selection rule (fixed in advance, and the whole point of this document)

A setup may be ranked ONLY on information observable at or before entry.

PERMITTED ranking features:
- liquidity, spread, depth, routeability at decision time
- volume, buy/sell imbalance, RSI at entry
- mint age, holder counts, metadata freshness
- the entry signal's own magnitude

FORBIDDEN ranking features, because each of them is FINDING 012 repeated:
- forward return at any horizon
- realised outcome, exit reason, or realised P&L
- anything computed from the price path AFTER entry
- coin identity, unless justified by a pre-entry attribute

If the ranking only becomes significant when buckets are formed by realised
return, the idea is DEAD regardless of any p-value.

## Method (fixed in advance)

- Population: mh_trades dynamic_scalper, joined to entry-time features only.
- Split: chronological, earliest 60% train, latest 40% test. Never shuffled.
- Cost: the conservative 1.800% round trip, always. Never the measured figure,
  which is not yet valid evidence (FINDING 013).
- Report both GROSS and NET. A gross-only result is not a result.
- Report the full-book baseline alongside every concentration figure, so any
  improvement is measured against something.

## Falsification criteria (decided now, not later)

The idea is DEAD if any of the following holds:

1. The best train-selected concentration has test-period NET <= 0.
2. The selected bucket contains fewer than 30 trades in the test window.
   This is INCONCLUSIVE, which I will report as inconclusive rather than
   promising.
3. The result depends on a FORBIDDEN ranking feature.
4. The result is positive in train and collapses in test.

## What I commit to

- Reporting the number I get, including a negative or inconclusive one, in the
  first message after the run. No burying it under caveats.
- Not changing these criteria after seeing results. If a criterion turns out to
  be wrong, that gets its own commit with a stated reason.
- Not promoting anything live. As with every other finding, promotion requires
  the evidence gate AND Kelly's explicit approval.
- Sending the finding to the council before it is treated as a candidate, given
  that the council correctly blocked task 1 and correctly identified that my
  instrument was broken.

## Expected outcome, stated in advance so a negative result is not a surprise

Tasks B and C both came back negative. My honest prior is that this one does
too. If it comes back strongly positive I will treat that as a reason for
suspicion, not celebration, and the pre-committed council review applies with
more force, not less.

A negative result here is a successful outcome of the method. It means the
strategy has no concentration effect worth trading, which is real information
about a $24 account and costs nothing to learn.
