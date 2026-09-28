# PROJECT ATLAS — FINDING 010: the NZ$60 protected floor is defined twice on the live path

**Status:** VERIFIED by execution (chair, independently of Seat 5 v2)
**Severity:** Critical — this is the most safety-critical constant in the system
**Source:** Council Seat 5 v2, which named it as the highest-risk finding

## The duplication

`AGENTS.md` (Non-Negotiable Safety Invariants) states:

> "Preserve the immutable NZ$60 protected floor and NZ$40 death threshold in
> `survival_policy.py`. Do not reinterpret missing treasury data as zero."

`survival_policy.py` is therefore the **declared single owner**. It defines:

```
survival_policy.py:15  PROTECTED_FLOOR_NZD   = Decimal("60.00")
survival_policy.py:16  DEATH_THRESHOLD_NZD   = Decimal("40.00")
```

`execution_policy.py` **independently redefines the floor as its own literal**:

```
execution_policy.py:27   PROTECTED_FLOOR_NZD = Decimal("60.00")
execution_policy.py:227  if pre < PROTECTED_FLOOR_NZD or post < PROTECTED_FLOOR_NZD:
execution_policy.py:228      raise PolicyDenied("protected NZ$60 floor would be breached")
```

Two `Decimal("60.00")` literals, in two modules, with no import between them. `DEATH_THRESHOLD_NZD`
is not duplicated, so the floor is the specific exposure.

## Why this ranks above every other structural finding

Every other duplicate-truth defect in this project produces a stale number or a wrong
answer in a report. This one is on the **pre-trade execution gate**: `execution_policy.py`
is what refuses an order that would breach the floor. It is the last check before a signed
transaction.

The two values agree today. That is exactly the condition the plan calls a defect:
"Project ATLAS therefore treats duplicate truth as a defect, even when the duplicated
values currently happen to match."

The concrete failure is a maintenance one, and it is severe. If the floor is ever
recalibrated, amended, or converted to a different currency, and someone updates
`survival_policy.py` — the documented owner — then:

- `survival_policy.py` reports the NEW floor and behaves correctly.
- `execution_policy.py` silently enforces the OLD floor.
- The two disagree with **no error anywhere**, and the system enforces whichever one the
  order gate happens to read.

That is either a blocked trade that should have been allowed, or a permitted trade that
breaches the constitutional floor. The second outcome is the one that matters.

## Aggravating factor: no approval gate

The values are literals, not config, so they cannot be changed through an approval
mechanism. The autotuner writes overrides to other tables; there is no equivalent,
auditable, reversible path for the treasury floor. Seat 5 flagged the absence of an
approval gate correctly.

## Required fix

1. `execution_policy.py` must **import** `PROTECTED_FLOOR_NZD` from `survival_policy.py`.
   One definition, one owner, as the constitution already requires.
2. Add a contract test asserting the floor and death threshold are defined in exactly one
   module. Extend `tools/check_single_truth.py` with a rule for constitutional constants,
   so a re-introduction fails CI rather than surviving review.
3. If a future requirement is for the floor to be *adjustable*, it must be an approved
   override with `source`, `approved_by`, `approved_at` and `config_version` recorded —
   never a bare literal. A bare literal is precisely what makes this unsafe.
4. The dashboard must render the floor from the API, never from its own copy.

## Verification already performed

```
$ grep -nE 'PROTECTED_FLOOR|DEATH_THRESHOLD' survival_policy.py execution_policy.py
survival_policy.py:15:  PROTECTED_FLOOR_NZD = Decimal("60.00")
survival_policy.py:16:  DEATH_THRESHOLD_NZD = Decimal("40.00")
execution_policy.py:27: PROTECTED_FLOOR_NZD = Decimal("60.00")   <-- second literal
execution_policy.py:227: if pre < PROTECTED_FLOOR_NZD or post < PROTECTED_FLOOR_NZD:
```

No `import survival_policy` in `execution_policy.py`. No config key carries the value
(`grep -nE 'floor|threshold|protected|death' config.yaml` returns nothing). Confirmed by
direct read, not inference.

## Note on scope

This finding is a **one-line import change** and carries no behavioural risk while the
values match. It is the cheapest high-severity item in the whole programme, and unlike the
directory-mount change it does not touch runtime topology. It is the one structural fix I
would make first, ahead of the age-only exit repair, because the repair changes behaviour
and this one cannot.
