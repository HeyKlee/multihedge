# PROJECT ATLAS — FINDING 007: the constitution contradicts the running code by 13x

**Status:** VERIFIED by execution inside the running container
**Severity:** Critical — highest business impact in the project
**Source:** Council Seat 3, independently verified by the chair

## The contradiction

`AGENTS.md` is the project's constitution. It states the Xora-Survival policy as:

```
AGENTS.md:63  - MEME defaults are +20% take profit, -10% stop loss, and a 900 second max-hold timer.
AGENTS.md:64  - SERIOUS defaults are +5% take profit, -2.5% stop loss, and a six-hour max-hold timer.
```

The canonical source, executed **inside the running container**, returns:

```
live_inventory.py:16  MEME_TAKE_PROFIT_PCT    = 0.015     (1.5%, not 20%)
live_inventory.py:17  MEME_STOP_LOSS_PCT      = -0.010    (1.0%, not 10%)
live_inventory.py:20  MEME_MAX_HOLD_SECONDS   = 1800      (30 min, not 15 min)
live_inventory.py:24  SERIOUS_TAKE_PROFIT_PCT = 0.010     (1.0%, not 5%)
live_inventory.py:25  SERIOUS_STOP_LOSS_PCT   = -0.010    (1.0%, not 2.5%)
live_inventory.py:28  SERIOUS_MAX_HOLD_SECONDS= 1800      (30 min, not 6 hours)
```

| Parameter | AGENTS.md (constitution) | Running code | Divergence |
|---|---:|---:|---:|
| MEME take profit | +20% | **+1.5%** | **13.3x** |
| MEME stop loss | -10% | -1.0% | 10x |
| MEME max hold | 900s | 1800s | 2x |
| SERIOUS take profit | +5% | +1.0% | 5x |
| SERIOUS stop loss | -2.5% | -1.0% | 2.5x |
| SERIOUS max hold | 6 hours | 1800s | **12x** |

## Why this is worse than the other findings

Every other finding is a defect *inside* the system. This one is a defect in the
**specification of the system**. The document written to govern the agent says take
profit at 20%; the code takes profit at 1.5%.

`AGENTS.md` instructs that the source tree is authoritative when documentation disagrees.
So this project has been formally operating under the rule that makes its own written
policy the thing that does not count. Any agent, auditor or human reading `AGENTS.md` to
learn the risk profile is wrong by an order of magnitude.

And the direction of the error matters commercially. At 1.5% take profit with real
round-trip friction measured at 0.37%–0.90% on these tokens, the spread between entry and
exit cost consumes a large fraction of the target. A strategy documented as "+20% TP,
-10% SL" is a swing trader; what is actually running is a 30-minute scalper with a 1.5%
target. **These are different strategies, not different parameter values.**

## What this invalidates

- Any backtest or replay tuned against a 20% target does not describe the running system.
- Any research result citing "+20%/-10%" is not about this bot.
- The `parameter_autotuner` cannot be meaningfully evaluated, because its holdout
  validation is scoring a different target than the constitution claims.
- The earlier 8-hour P&L analysis could not produce results, and with good reason: at a
  1.5% target against 0.4–0.9% friction plus a ~20-minute exit-check cadence, positions
  routinely cannot reach target or stop inside the observed window.

## Unresolved: which value is *intended*?

This is a decision only Kelly can make, and it is deliberately **not** made here.

- If **1.5% is intended**, then `AGENTS.md:63-64` is dangerously stale and must be
  corrected immediately, because it currently instructs every reader that the bot runs a
  20% target.
- If **20% is intended**, then the running thresholds are wrong by 13x and every trade
  taken since the divergence has been exiting far too early. That is a live-risk event,
  not a documentation issue.

I am not guessing which, and I am not changing either value. The safe action until it is
decided is to record both, in one place, with provenance, which is exactly what
`architecture/source_of_truth.yaml` is for.

## Required fix

1. Decide the intended MEME/SERIOUS policy. Record it once, with an approval marker.
2. Rewrite `AGENTS.md:63-64` to state the real values, or state that the values live in
   the resolver and must not be duplicated in prose at all. The second option is
   structurally better: documentation that restates a number is a rival source.
3. Add a contract test asserting `AGENTS.md` contains no numeric policy literals, so this
   class of drift cannot recur.
4. This becomes the first entry in `source_of_truth.yaml`, since every other threshold
   finding depends on knowing which target is real.
