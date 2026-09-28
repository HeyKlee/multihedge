# SEAT 4 — ADDENDUM A: test coverage of the exit divergence, and a pre-existing red suite

**Baseline:** git `35f922a`
**Status:** analysis only. No test, module, config or database was modified.
This addendum exists because it changes the plan in
`ops/atlas/SEAT4_REFACTOR_PLAN.md`, and I would rather say so than let the chair
discover it mid-refactor.

## A1. The age-alone invariant is tested in 2 of 4 engines

`AGENTS.md:70-72` and the `AGENTS.md` testing rules both require the invariant
"age alone does not exit". It is pinned for two engines and silently absent for
the other two.

| Engine | Test exists | Evidence |
|---|---|---|
| `live_inventory.forced_exit` | **YES** | `test_live_inventory.py:62-75` |
| `dynamic_shadow_scalper._exit_reason` | **YES** | `test_dynamic_shadow_scalper.py:72-90` |
| `paper.close_checks` | **NO** | `grep -rn close_checks test_*.py` returns nothing |
| `mh_memecoin_trader._eval_exit` | **partial, wrong assertion** | `test_whale_audit.py:61-62` |

The two conformant engines are exactly the two that are tested. The two
non-conformant engines are the two with no coverage. That is not a coincidence,
it is the mechanism: the invariant was fixed where someone was already looking,
and never propagated to the paths nobody was exercising.

`test_live_inventory.py:66-67` states the invariant in a comment and asserts it:

```
# Age alone must no longer force a sale if TP was never reached.
self.assertIsNone(li.forced_exit(self.db, {MINT: .001}, now=2901))
```

`test_dynamic_shadow_scalper.py:72` is named
`test_stop_loss_is_deterministic_and_age_alone_does_not_exit` and asserts
`result["closed"] == 0` after the hold timer expires.

**`test_whale_audit.py:61-62` is worse than absent.** It is the only test touching
`mh_memecoin_trader._eval_exit`, and it asserts the trail path only:

```
def test_memecoin_trail_remains_armed(self):
 self.assertEqual(meme._eval_exit({'entry':100,'peak':140,'ts':time.time()},125),'trail_stop')
```

The name claims the arm "remains armed", but the dict passes `ts=time.time()`,
so the max-hold branch at `mh_memecoin_trader.py:149` is never reached and the
per-cycle arm re-test at `mh_memecoin_trader.py:152` is never distinguished from
the latching behaviour at `paper.py:437-439`. The test name promises the
invariant the code does not honour.

**Consequence for Step 1:** the regression test I specified in the plan does not
need to invent the assertion. `test_live_inventory.py:62-75` is the reference
shape, and copying it against `paper.close_checks` and
`mh_memecoin_trader._eval_exit` is the smallest possible test-first step. It will
fail on both, which is the point.

## A2. Pre-existing red suite, unrelated to this seat

Running the three exit-relevant suites at HEAD:

```
$ python3 -m unittest test_live_inventory test_dynamic_shadow_scalper test_whale_audit -q
Ran 34 tests in 2.177s
FAILED (failures=3, errors=1)

FAIL: test_live_memecoin_trail_ignores_noise_then_protects_larger_move (test_live_inventory)
FAIL: test_memecoin_trail_ignores_small_noise_then_protects_larger_move (test_dynamic_shadow_scalper)
FAIL: test_risk_params_classify_serious_vs_memecoin (test_dynamic_shadow_scalper)
ERROR: test_unregistered_wallet_token_cannot_be_treated_as_bot_inventory (test_live_inventory)
```

One assertion is directly informative:

```
test_dynamic_shadow_scalper.py:125
    self.assertEqual(meme["stop_loss_pct"], -0.015)
AssertionError: -0.01 != -0.015
```

The test expects MEME stop loss -0.015. The code returns -0.01, which is
`live_inventory.py:17` `MEME_STOP_LOSS_PCT = -0.010`. So a test is pinning a
stop-loss value that no longer exists in the source. This is FINDING 007's
constitution-versus-code drift surfacing as a red test rather than as a
documented decision, and it is the same class of defect: a number that changed
ownership without the change being recorded in one place.

**These four failures predate this seat.** Verified by mtime, not asserted:

```
test_live_inventory.py         2026-09-22 13:16
test_dynamic_shadow_scalper.py 2026-09-27 12:02
test_whale_audit.py            2026-09-27 12:02
live_inventory.py              2026-09-24 22:14
ops/atlas/* (my writes)        2026-09-28 19:22-19:27
```

I did not modify any of them, and I have not run the full suite
(`python3 -m unittest discover -q`) because it is outside this seat's remit to
report a project-wide baseline. What I can say precisely: the three suites that
cover the four exit engines are already red, and two of those four failures are
about trailing-stop and stop-loss thresholds in exactly the engines Step 1 must
unify.

**Consequence for Step 1:** Step 1 cannot use "full suite green" as its exit
criterion, because the suite is not green now. The plan's acceptance line is
wrong and I am correcting it. The correct criterion is a *delta*: the count of
failures attributable to exit semantics must not increase, and the four
pre-existing failures must be triaged and either fixed or explicitly accepted
before Step 1 starts. Otherwise a genuine regression is indistinguishable from
the existing mess.

I would not start Step 1 on a red suite. Triage first.

## A3. `AGENTS.md:108` already forbids what D1 does

```
AGENTS.md:108   `live_inventory.py` is the shared source of active Xora-Survival
                exit parameters. Do not add independent threshold constants to
                callers.
```

This is the constitution already stating the rule that `paper.py:49-51` and
`mh_memecoin_trader.py:22-26` break, and it names `live_inventory.py` as the
owner. So Step 1's target is not an invention of this seat, it is an existing
constitutional boundary that the code has drifted away from. That materially
strengthens the case for doing Step 1 first and lowers the risk of it being
rejected as speculative.

Note the constitution names `live_inventory.py` as owner of *parameters*. It
does not name an owner for the *exit decision*, which is the part actually
duplicated. Closing that wording gap is a one-line change to `AGENTS.md`, and per
FINDING 007's precedent it should be made only with Kelly's explicit approval,
since `AGENTS.md` is the document governing this agent.

## A4. Revised acceptance criteria for Step 1

Replacing the plan's original line:

**Wrong:** "`ops/atlas/seat4_gate.py --quiet` reports 0 for E1/E2. Full suite
green."

**Correct:**

1. Triage the four pre-existing failures first. Record which are exit-semantics
   and which are unrelated. Do not start Step 1 until triaged.
2. Add the `test_live_inventory.py:62-75` assertion shape against
   `paper.close_checks` and `mh_memecoin_trader._eval_exit`. Both must fail
   before the change.
3. After consolidation, both must pass, and `E1`/`E2` in `ops/atlas/seat4_gate.py`
   must report 0.
4. Failure count must not exceed the triaged baseline. State the baseline number
   explicitly in the commit message.
5. Verify `live_inventory.forced_exit` output is byte-identical before and after
   on recorded price paths, since it is the conformant reference and the one
   engine whose behaviour must not change at all.
6. Deploy requires Kelly's explicit approval, because this changes exit
   behaviour on a funded mainnet path.
