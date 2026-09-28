# PROJECT ATLAS — FINDING 007: constitution contradicted the running code (RESOLVED: policy decided)

**Status:** VERIFIED, and the intended policy is now **decided by Kelly (2026-09-28)**
**Severity at discovery:** Critical
**Resolution:** recorded below; not yet applied to runtime

## The contradiction found

`AGENTS.md` (the project constitution) stated:

```
AGENTS.md:63  MEME: +20% take profit, -10% stop loss, 900 second max-hold
AGENTS.md:64  SERIOUS: +5% take profit, -2.5% stop loss, six-hour max-hold
```

The code, executed inside the running container, returned:

```
live_inventory.py:16  MEME_TAKE_PROFIT_PCT     = 0.015
live_inventory.py:17  MEME_STOP_LOSS_PCT       = -0.010
live_inventory.py:20  MEME_MAX_HOLD_SECONDS    = 1800
```

A 13.3x divergence on take profit, 12x on SERIOUS max-hold.

## DECISION (Kelly, 2026-09-28)

> "20% every 30-60 mins / at minimum 1.5% or more every min"

Interpreted as the governing intent:

| Parameter | Decided value | Note |
|---|---|---|
| MEME take profit (target) | **20%** | the real objective |
| MEME take profit (floor) | **1.5%** | minimum acceptable scalp |
| MEME max hold | **1800–3600s (30–60 min)** | supersedes the 900s in AGENTS.md |
| MEME stop loss | **-10%** | matches AGENTS.md, not the -1.0% running |

**What this means:** the currently running `0.015` is the *floor* leg, not the target. The
system is configured to bank the minimum 1.5% scalp and never express the 20% objective.
That is a coherent strategy (scalp the floor), but it is **not** what the constitution
describes and not what Kelly just confirmed as the intent.

**This is not yet applied.** Changing live exit thresholds is a trading-behaviour change
to a funded mainnet path, so it is recorded here and gated rather than pushed silently.

## The blocker that must be solved first (cadence currently UNVERIFIED)

A 20% target in 30–60 minutes is only executable if exits are evaluated fast enough to
catch it. **I have not been able to measure that reliably, and I am not going to assert a
number I cannot reproduce.**

What I tried and what happened:

- `agent_decision_log.created_at` gaps: median 0.0s, p90 31.6s — but 29,648 gaps over a
  median of zero means multiple rows share a timestamp, so this measures row batching, not
  cycle cadence.
- `agent_decision_log.tick_ts` deduped: 29,649 distinct ticks, median gap 0.00 min,
  p90 0.53 min. Same problem — it is a per-decision row, not a per-cycle marker.
- The hermes cron entry is `* * * * *` ("every 1m", enabled), so the **launcher** fires
  every minute. That is a real, verified fact.
- `ops/multihedge_autonomous_live.py:105` has `interval = 300`, an unclear 300s bucket.

An earlier figure in this session (median 20 min, p90 72 min) was reported from a
measurement I can no longer reproduce. It should be treated as **unverified** and is
withdrawn rather than repeated.

**What must be measured before the 20% target is enabled:** the wall-clock interval at
which an *open position's* exit conditions are actually evaluated, per trader. That
requires instrumenting the exit path itself (a timestamp written on each exit check), not
inferring it from decision-log row spacing. Until that instrumentation exists, the
question "could the system have caught a 20% move inside 30–60 minutes?" cannot be answered
from data — and per this project's own standard, an unanswerable question must block the
change rather than be assumed favourable.

## Required sequence (do not reorder)

1. Reduce exit-evaluation cadence to a bound that can observe a 30–60 minute target
   (target: well under 60s, with a hard max-hold fallback that is genuinely time-based).
2. Deploy the reviewer fix that is currently **not** live (FINDING 005).
3. Only then set the target TP to 20% with the 1.5% floor retained as a partial-exit or
   trailing-stop arm.
4. Re-run the friction filter: at a 20% target a 0.5% entry gate is far less binding than
   at 1.5%, so the trade-off changes and the gate should be re-derived, not carried over.

## Prevention

`test_atlas_policy_ownership.py` fails on `AGENTS.md:63-64` today, pinning the drift. The
durable fix is that the constitution should name the owner and the intent, and never
restate the numbers. That change to `AGENTS.md` is deliberately **not** made here without
explicit approval, since the constitution is the document that governs this agent.
