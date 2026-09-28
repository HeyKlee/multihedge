# PROJECT ATLAS — FINDING 011: the 20% target is not reachable on these tokens, measured

**Status:** VERIFIED from 26,463 real price points across 56 mints
**Severity:** Strategic — this is a goal-setting input, not a code defect
**Source:** `ops/move_distribution.py`, built to answer Kelly's stated goal with data

## The goal as stated (Kelly, 2026-09-28)

> "20% every 30-60 min is the goal, working towards hitting that goal by taking 1.5% or
> more profits every 5 mins."

## What the ledger actually contains

Maximum absolute excursion reachable *after* each 60-second sample:

| Horizon | samples | median | p75 | p90 | p99 | ≥1.5% | ≥5% | ≥20% |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 5 min | 23,647 | **0.38%** | 0.70% | 1.28% | 5.30% | **7.7%** | 1.1% | **0.0%** |
| 15 min | 25,108 | 0.83% | 1.44% | 2.60% | 10.34% | 23.4% | 3.4% | 0.1% |
| 30 min | 25,640 | 1.28% | 2.21% | 3.80% | 14.80% | **42.0%** | 6.1% | 0.5% |
| 60 min | 25,962 | 1.98% | 3.39% | 5.58% | 20.34% | **64.3%** | 12.6% | **1.1%** |

## The three findings that matter

**1. "1.5% every 5 minutes" is not a base rate, it is an outlier.**
The *median* 5-minute move is **0.38%**. A 1.5% move occurs in only **7.7%** of 5-minute
windows. Roughly 92% of the time, these tokens do not move 1.5% in five minutes at all.

**2. Compounding 1.5% five-minute scalps to reach 20% does not close.**
20% requires ~13 consecutive 1.5% wins (1.015^13 ≈ 1.21). At a 7.7% per-window touch rate,
the expected number of 1.5% touches in a full 60-minute window is **7.7 spread across the
whole hour, not 12 in sequence** — and the windows overlap heavily, so they are the same
move counted repeatedly, not independent trades. Measured directly: a **20% excursion occurs
in 1.1% of 60-minute windows and 0.0% of 5-minute windows.**

**3. The 30-60 minute horizon is the right one, and 20% is the wrong number for it.**
A 20% target is a multi-hour or multi-day move on this asset class. Within 60 minutes the
realistic distribution is: median 1.98%, p90 5.58%, and only 1.1% of windows touch 20%.

## What this does and does not invalidate

**Still valid, and well supported:**
- Scalping small, consistent gains. 1.5% is reachable in 42% of 30-minute windows and 64%
  of 60-minute windows. That is a workable target.
- The 30-60 minute hold. The data supports this window directly.
- The current 1.5% take profit. It sits above the 5-minute p90 (1.28%) and below the
  30-minute median (1.28%) — i.e. it is a reasonable, achievable intraday target.

**Not supported by the data:**
- 20% as a recurring intraday outcome. It is a 1-in-90 event on a 60-minute window.
- "1.5% every 5 minutes" as an operating assumption. At a 0.38% median, most 5-minute
  windows contain no such move, so a system targeting it would sit in cash most of the time
  and would be trading noise into its spread when it did fire.

## The friction interaction that makes this worse

Entry friction on these tokens measured 0.37%-0.90% round trip (FINDING/CORRECTION
earlier today). Against a 5-minute median move of **0.38%**, a 0.4-0.9% round trip cost is
**larger than the median move itself**. Any strategy targeting the 5-minute horizon is
paying more in friction than the typical price move is worth. This is the single most
important number in this finding.

At a 15-minute median of 0.83%, a 0.5% round trip consumes 60% of the median move.
At 30 minutes (1.28%), it consumes ~39%.
At 60 minutes (1.98%), it consumes ~25%.

**The shorter the target, the more the round trip dominates.** This is the quantitative
justification for a 30-60 minute hold over a 5-minute one, and it is a stronger argument
than anything in the policy documents.

## Recommendation (Kelly's decision, not mine)

Do not raise take profit to 20%. On this data it would convert the system into a strategy
that exits almost never, holds losers far longer than winners, and pays friction on a
position it cannot exit. The evidence supports:

- **keep 1.5% as the working target** (it is achievable at 30-60 min horizons)
- **keep the 30-60 minute max hold** (directly supported)
- **retire "20% every 30-60 min" as an intraday objective**; it is a swing-trade horizon
- if 20% is the real ambition, it belongs to a different instrument class and a different
  holding period, not to 5-minute Solana memecoin scalps

## Method and limits

Computed from `mh_shadow_entry_observations`, the live shadow-scaler price series, sampled
at ~60s median. Excursions are measured from a sample, not from a trade entry, so this is
an opportunity envelope rather than a strategy backtest. It does not model slippage,
latency, exit-cadence misses, or position selection. It answers exactly one question — how
far do these tokens move, and how often — and it is the question the goal statement turns on.
