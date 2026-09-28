# PROJECT ATLAS — FINDING 009: two constitution violations found by council Seat 4

**Status:** VERIFIED by the chair, independently of Seat 4
**Severity:** Critical (E1) — this changes realised trading behaviour
**Source:** Council Seat 4 (`qwen3-coder-plus:free`) via `ops/atlas/seat4_gate.py`, re-verified by hand

Seat 4's JSON return value was malformed and one `write_file` dropped its content, so the
report was graded on its artifacts, not its prose. Its tooling runs and produces findings.
That was the deciding factor: a seat whose tools execute and whose claims survive chair
re-verification has delivered value regardless of how its summary serialised.

## E1 — `paper.py` closes positions on age alone, violating the constitution

`AGENTS.md:65-67` (the project's own constitution):

> "Max hold is a missed-TP execution fallback only. It may close after the timer only if
> the recorded peak already crossed TP but the TP sale did not complete. **Age alone never
> closes a position.** Stop loss remains unconditional."

`dynamic_shadow_scalper.py:100-114` **complies** — it requires the peak-crossed-TP guard:

```python
if (now - float(position["opened_ts"]) >= params["max_hold_seconds"]
        and peak / entry - 1 >= params["take_profit_pct"]):
    return "max_hold"
```

`paper.py:434-435` **violates it** — the guard is absent:

```python
if time.time() - pos["open_ts"] >= MAX_HOLD_S:
    return "max_hold"
```

Consequence: every paper position is force-closed at 3600s regardless of whether it ever
approached target. Combined with FINDING 006 (14 rival PnL implementations) this means the
paper ledger, which is the evidence substrate for the autotuner, is systematically
truncating positions the constitution says must be held. It also contaminates the evidence
used to research TP/SL, so any conclusion drawn from closed paper trades inherits the
defect.

`mh_memecoin_trader.py:150` has the same unguarded pattern.

This is the finding that would change money. It was not in the chair's own map, which is
the entire argument for running a council seat with a different mandate than the chair.

## E2 — four traders size positions differently, against a config value

`config.yaml:72` declares `POSITION_FRACTION: 0.5`. Independently verified:

| Module | Own value | Ratio vs config |
|---|---:|---:|
| `mh_reasoner.py:32` | 0.50 | 1.0x (matches) |
| `mh_whale_trader.py:41` | 0.40 | 0.8x |
| `mh_memecoin_trader.py:28` | 0.20 | 0.4x |
| `dynamic_shadow_scalper.py:46` | 0.15 | 0.3x |

None of these read the config key. So `POSITION_FRACTION` in `config.yaml` governs
**nothing** — it is a dead setting, and the four live traders each apply a different,
unrecorded risk appetite. Changing that config value would appear to succeed and change
nothing. That is precisely the failure mode the whole project exists to end.

## E3 — dashboard hardcodes eligibility thresholds

```
dash_web.py:73   "eligible": n >= 20 and wr >= 0.75
mh_dash.py:543   and further sites (10 total)
```

Rule G violation (FINDING 002 class). The dashboard displays an eligibility verdict from
its own literals rather than from the API. An operator reading it is reading a number the
system does not enforce.

## E4 — `track_whales.py` re-declares the mint universe

The token universe is hardcoded in source instead of resolved from the canonical registry.
Combined with Rule E (mint is identity), this is a second place where the set of tradeable
assets is defined without a single owner.

## Seat 4 tooling (kept)

- `ops/atlas/seat4_detector.py` — static detector for the above classes
- `ops/atlas/seat4_gate.py` — non-zero exit gate, currently **20 violations** across
  E2/E3/E4/E5
- `ops/atlas/SEAT4_DUPLICATION_MAP.md`, `SEAT4_REFACTOR_PLAN.md`,
  `SEAT4_REFACTOR_ANALYSIS.md`, `SEAT4_ADDENDUM_A.md`

These are analysis artifacts and a second enforcement gate alongside
`tools/check_single_truth.py` (74 violations). Two independent gates is the point: neither
is trusted alone.

## Grading note

Seat 4's machine-readable return value did not parse (2 fenced blocks, both invalid JSON),
and its first `write_file` call for the main analysis document silently dropped its content.
Both are recorded because the plan requires evidence standards to apply to the council too,
not only to the code. The artifacts it did produce are substantive and its single most
important claim (E1) is correct.

## Required order (E1 first)

1. Fix `paper.py` and `mh_memecoin_trader.py` to require the peak-crossed-TP guard. This
   changes realised paper behaviour and therefore invalidates part of the evidence base,
   so it must be done before any autotuner conclusion is trusted.
2. Route all four `POSITION_FRACTION` values through `domain/policy/resolver.py`, or
   declare each trader's sizing as an explicit, documented per-trader policy rather than a
   silent literal.
3. Remove dashboard eligibility literals; render "unavailable" instead of a number the API
   did not send.
4. Move the mint universe to the canonical asset registry.
