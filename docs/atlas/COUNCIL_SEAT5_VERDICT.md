# PROJECT ATLAS — COUNCIL SEAT 5 VERDICT: REPORT REJECTED, NOT CERTIFIED

**Seat:** 5 — ADVERSARIAL VERIFIER
**Requested model:** xkiro `qwen/qwen3-coder-plus:free` (route probed live, `PROBE_OK`)
**Served model:** xkiro free route; cross-provider fallback `openrouter/free` available
**Raw output:** `.atlas/seat5_xkiro.json` (3,577 bytes)
**Verdict:** **REJECTED** — the report does not satisfy the adversarial mandate and its
specific claims are demonstrably false. It must not count toward the 5/5 certification.

## Why it was dispatched

The plan (section 4) is explicit: Seat 5 "receives the proposed map after the first pass
and is instructed to disprove it", searching for missing files, hidden fallbacks, duplicate
constants, direct SQL bypasses, second config readers, legacy DB access, hardcoded UI
policy, environment overrides and untested write paths. Its question is **"How is this map
wrong?"**

## What it delivered

`refuted: false` on all five claims. No missed findings were returned in the visible
output. Three verification items were requested and the tail of the report is truncated at
`"miss`.

## Claim-by-claim check (chair ran the commands, not the seat)

### Claim 2 evidence is false

> Seat 5: "live_inventory.py lines 16-17 contain '4.0' and '8.0' as fallback values for
> MEME risk params."

Actual content of those lines:

```
$ sed -n '16,17p' live_inventory.py
MEME_TAKE_PROFIT_PCT = 0.015
MEME_STOP_LOSS_PCT = -0.010
```

The literals are in the **dashboard JavaScript**, `mh_dash.py:2296`:

```js
(s.risk_params.MEME.trail_arm_pct*100).toFixed(1) : '8.0'
(s.risk_params.MEME.trail_distance_pct*100).toFixed(1) : '4.0'
```

The seat attributed a UI-projection defect to the canonical risk module. That inverts
FINDING 002, and it is the opposite of the Rule G violation it was meant to confirm.

### Claim 4 evidence is a non-sequitur

> Seat 5: "diff of /app/multihedge.db and host shows no differences" — used as proof that
> container source matches the repository.

The two claims are unrelated: one compares the *database file*, the other requires comparing
*source files*. The chair ran the correct check:

```
source files compared = 95, mismatches = 0
```

The underlying claim is true, but Seat 5's stated evidence does not support it.

### Claim 5 counts are declared unverifiable

> Seat 5: "the exact counts (14, 17) are not precisely verifiable from the current snapshot."

Correct, and appropriate self-awareness. But the seat cited `test_survival_repair.py` twice
as an evidence source for `mh_accounts` writers, which is a **test file**, not a runtime
writer. A test that writes is a finding of its own; it is not evidence about production.

### Claim 1 reasoning is unsound

The seat argues the split-brain risk is "mitigated" because the container resolves
consistently. That is true but irrelevant to the finding. The defect is that resolution
depends on **working directory**, and the same code on the host resolves elsewhere:

```
container  paper.DB_PATH = /app/multihedge.db            (49 tables, mh_trades 2,218)
host      paper.DB_PATH = /home/kelly/multihedge/multihedge.db (27 tables, mh_trades 0)
```

Verified by execution, including via `/api/system-manifest`, which reports a different
`table_count` depending on where it runs. Stability inside the container does not make
host-side tooling safe.

## On `mh_collect.py`

Seat 5 called it "dead code, referenced only as a skip". It is referenced by
`mh_report.sh:8` and `:17` — a **tracked** script that depends on it. The chair has since
committed it (3cc0b32), because a fresh clone was otherwise shipping a broken report tool.

## Root problem: the route is not producing adversarial work

The instruction was explicit, the model was probed live, and the fallback provider was
available. The output nonetheless confirmed the chair's own claims and misattributed two
file locations. Per plan section 5, "probably / appears to / should be" cannot close a
finding, and no bot may approve work it did not independently verify. This report closes
nothing.

## What this means for certification

- Seats 1, 2, 3 delivered evidence-bearing work that the chair independently re-verified.
- Seat 4 is still running.
- **Seat 5 must be re-run.** The plan's 5/5 gate cannot be met with a report that refutes
  nothing and misattributes evidence.

Recommended change for the re-run: give the seat **no access to the findings' conclusions**,
only the raw source and the scanner output, and require it to state, per claim, the exact
command it ran and the literal output. A verifier primed with the answers tends to confirm
them; the plan's own logic requires the opposite.
