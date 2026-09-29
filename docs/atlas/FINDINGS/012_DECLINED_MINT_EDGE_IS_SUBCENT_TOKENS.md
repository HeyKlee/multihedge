# FINDING 012: the +9.6% "declined mint" edge is eight sub-cent tokens

Date: 2026-09-29
Status: verified, closed negative
Author: XORA (Claude Opus 4.8)
Supersedes: the task-B lead reported in conversation on 2026-09-29

## The claim being retired

Splitting `mh_shadow_entry_observations` by whether the system actually bought
the mint showed the declined group returning **+10.6% mean on the 1-hour
horizon** against **+0.52%** for the bought group. It was reported as the only
lead in the dataset pointing up, and as the place positive gross edge might be
hiding. The entry filter may have been rejecting the best setups.

## What the data actually shows

| population | n | mean 1h | median 1h |
|---|---|---|---|
| declined, raw | 594 | **+9.592%** | +2.889% |
| declined, trimmed (>50% removed) | 533 | **-0.123%** | +0.380% |
| bought, raw | 29,497 | +0.509% | +0.221% |
| bought, trimmed | 29,459 | +0.429% | +0.217% |

The mean sits at +9.6% while the median sits at +2.9%. That gap is the whole
story: **61 observations, spanning only 8 distinct mints, each returning more
than +50%**, contribute +5,770 percentage points to the raw mean. Remove them
and the declined group does not merely lose its edge, it goes slightly
negative, and it is *worse* than the bought group.

The bootstrap on the raw data was genuinely positive (95% CI [+6.81%,
+12.57%]), so the number was not imaginary. Weighting each of the 28 declined
mints equally also kept it positive (+12.44%, 95% CI [+2.08%, +25.39%]). Both
tests pass, and both are still wrong, because neither one is robust to a
population of eight assets.

## Why the outliers are not tradeable

Median price of the >50% movers: **$0.00169**. Median price of everything
observed: **$0.27**. Median price of what the system actually bought: **$0.27**.

A +180% move on a sub-cent token is a large *percentage* and a trivial
*absolute* change, on an asset with no meaningful depth. At the $24 account
size there is no route into these that could capture the move, and exiting them
at the quoted price is not realistic. These are not missed profits. They are
percentages without dollars behind them.

The effect also spreads across 09-21 to 09-30 rather than one event, so it is
not a single data glitch that could simply be discarded. It is a persistent
feature of a handful of ultra-cheap tokens.

## Net of cost

| population | gross 1h | net at 1.800% round trip |
|---|---|---|
| declined, trimmed | -0.123% | **-1.923%** |
| bought, trimmed | +0.429% | **-1.371%** |

Neither is profitable. The bought group is the better of the two, which inverts
the original claim: the entry filter is not rejecting the best setups, it is
preferentially rejecting an untradeable tail.

## Method notes

The lesson is about the estimator, not just this dataset. Three checks each
individually said the effect was real:

1. bootstrap on raw observations: CI excludes zero
2. per-mint equal weighting: CI excludes zero
3. median vs mean flagged a 3x discrepancy

Only the third was diagnostic. A bootstrap resamples rows, so it inherits the
same eight-asset concentration and reports the outliers' own variance. Equal
per-mint weighting fixes the row weighting but not the fact that eight assets
*are* the sample. Neither test asked whether the effect is capturable.

Any future edge claim on this dataset must be reported alongside: the trimmed
mean, the per-mint count, the median price of the movers, and the net figure
after the 1.800% conservative round trip. An effect that survives only in the
untrimmed mean is not an edge.
