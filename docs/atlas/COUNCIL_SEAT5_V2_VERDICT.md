# PROJECT ATLAS — COUNCIL SEAT 5 (v2) VERDICT: ACCEPTED WITH CORRECTIONS

**Seat:** 5 — INDEPENDENT VERIFIER (re-run, unprimed)
**Model:** xkiro `qwen/qwen3.8-max:free` (probed live)
**Raw output:** `.atlas/seat5v2_xkiro.json` (10,799 bytes)
**Verdict:** **ACCEPTED WITH CORRECTIONS.** Materially better than v1 (rejected, see
`COUNCIL_SEAT5_VERDICT.md`). It formed its own picture before reading the chair's findings,
discarded its own unsupported claims, and found a defect class no other seat caught.

The re-run design worked: removing the conclusions from the brief is what produced
independent work. Recorded as the working method for future verifier seats.

## What v2 got right that nothing else found

**Decimal-conversion sites (the bug class that actually bit you on 2026-09-28).**
Chair-verified, all three lines exist exactly as cited:

```
live_inventory.py:302  entry = (cost / 1_000_000) / (amount / (10 ** decimals))
live_bridge.py:301     fill_price = ((int(reconciliation["input_atomic"]) / 1_000_000)
chain.py:203           return float(resp.value.amount) / (10 ** resp.value.decimals)
```

Each divides a USDC-atomic quantity by a token-atomic quantity, i.e. a 6dp/9dp mix. That is
the exact defect that stamped real tokens `no_route:implausible_buy_99.9pct`. `chain.py:203`
in particular normalises correctly on its own, so the *pattern* is right there and wrong
wherever a ratio crosses the two scales. This is a genuinely new finding and it is the one
class of bug with a proven production incident.

**Constitutional risk values duplicated.** `NZ$60` protected floor and `NZ$40` death
threshold in both `survival_policy.py` and `execution_policy.py`, with no approval gate.
The constitution declares `survival_policy.py` the sole owner, so a second definition is a
direct Rule violation on the most safety-critical constants in the system.

**It self-discarded.** It reported that it could not verify whether
`test_atlas_policy_ownership.py` validates the canonical modules' presence, rather than
asserting it. That is the behaviour the plan's section 5 demands, and v1 never did it.

## Where v2 is wrong, and the correction that matters

**It concluded the governing take-profit is `0.015` from `live_inventory.py`.** It backed
this with an executed call, so the error is subtler than v1's: it called
`risk_params(mint, cfg=None)`. With `cfg=None` there is no coins list, so every mint
classifies as MEME and the DB-override branch is skipped.

The live autonomous path calls it differently (`dynamic_shadow_scalper.py:249`):

```python
policies = {mint: risk_params(mint, cfg, db_path=db_path, allow_tuned=True)
            for mint in observed_mints}
```

`allow_tuned=True` with a `db_path` **does** consult the override tables. Currently
`mh_risk_params` and `mh_coin_risk_params` both hold 0 rows, so today the effective value
happens to be the 0.015 default. But that is a property of an **empty table**, not of the
policy design. The moment the autotuner writes a shadow candidate, the governing value
silently changes to whatever the table says — and nothing in the source says so.

**This refines FINDING 004 rather than contradicting it.** The 0.05 in `mh_reasoner_params`
governs the *reasoner* trader. The shadow scalper's live exits resolve through
`live_inventory.risk_params` and currently land on 0.015 via an empty override table. Two
different resolution paths, two different effective values, and neither is stated in a
single place. That is the same defect class, now mapped precisely.

Corrected live truth, from `/api/system-manifest`:

```
reasoner_params  TAKE_PROFIT = 0.05     <- governs the reasoner trader
declared source  MEME TP     = 0.015    <- governs shadow-scalper exits today,
                                          only because the override table is empty
```

## Also confirmed absent (negative findings, which are worth as much as positive ones)

- **No paper/live provenance confusion found.** `mh_dash.py:177` keeps `live_positions`
  and `paper_positions` separate in one response.
- **No BUY/SELL gate asymmetry found.** `execution_policy.validate_intent` enforces USDC
  input on BUY (line 108) and USDC output on SELL (line 110). Both paths gated.

These are the two questions the chair most wanted an outside opinion on, and an
independent seat reached the same conclusion from the source.

## Grading notes, recorded because the plan applies its evidence standard to the council too

- Counts are inflated versus the machine scan (78 config readers and 152 db-path sites vs
  the scanner's 11 and 133). It evidently counted grep hits rather than distinct
  resolution sites. The direction is right, the magnitude is not.
- `db_writers` lists `multihedge.py:76-92` and order-store tables that belong to the live
  path, mixing paper and live writers in one list without distinguishing them.
- Its JSON is truncated mid-array; `discarded_own_claims` and `what_i_could_not_verify`
  are incomplete.

These are why the verdict is "with corrections" rather than "accepted". The load-bearing
contribution — the decimal-conversion class and the duplicated constitutional constants —
is verified and stands.

## Council tally

| Seat | Verdict |
|---|---|
| 1 Chair | active; every finding re-verified by execution |
| 2 Cartographer | accepted |
| 3 Data Forensics | accepted |
| 4 Refactor Engineer | accepted (grading notes recorded) |
| 5 Adversarial v1 | **REJECTED** |
| 5 Adversarial v2 | **accepted with corrections** |

5 of 6 seats now delivered independent, evidence-bearing work. Machine gates still fail
(74 + 20 violations), which is the binding constraint on certification, not seat count.
