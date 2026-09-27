# PROJECT ATLAS — FINDING 004: four disagreeing risk-threshold sets, DB override wins silently

**Status:** VERIFIED by execution inside the running container
**Severity:** High — this is the exact "two sources of truth" failure, inside the risk layer
**Source:** Council Seat 2 (Cartographer) + chair independent verification

## The divergence

Four layers each define take-profit / stop-loss / max-hold. They do not agree:

| Layer | Location | TP | SL | MAX_HOLD |
|---|---|---:|---:|---:|
| Python source (Xora exit params) | `live_inventory.py:16,17,20` | 1.5% | -1.0% | 1800s |
| Python source (paper ledger) | `paper.py:50,51,49` | 2.5% | -1.5% | 3600s |
| `config.yaml` reasoner block | `config.yaml:73,74,79` | 1.5% | +0.015 | 7200s |
| **DB override (production)** | `mh_reasoner_params` | **5.0%** | **0.02** | **7200s** |

## Which one actually governs

`mh_reasoner.py:44` documents the precedence: *"DB table wins, then config.yaml `reasoner:`
block, then defaults."* Verified by executing `_load_params()` **inside the running
container**:

```
CONFIDENCE_MIN    = 0.35
MAX_HOLD_SECS     = 7200.0
POSITION_FRACTION = 0.5
STOP_LOSS         = 0.02      <- from DB
TAKE_PROFIT       = 0.05      <- from DB
TRAIL_ARM         = 0.01
TRAIL_DIST        = 0.005
```

So the effective reasoner uses **5% TP / 2% SL**, which appears in **no source file**. Read
`live_inventory.py` and you would believe the system runs 1.5% / 1.0%. That is a 3.3x
misread of the take-profit and a 2x misread of the stop.

The DB row is written by the monthly optimizer and **survives image rebuilds**
(`mh_reasoner.py:57` comment). Editing any Python constant or `config.yaml` value has
**no effect** on the running reasoner. This is precisely the reported symptom: change a
value, and nothing applies.

## Second defect: an unversioned, unreviewed override

- The override is a bare `key/value REAL` table. No author, no timestamp, no approval
  record, no config-file version it was derived from.
- `config.yaml:39 autotune_live_promotion_enabled = false` is supposed to gate promotion,
  but `_load_params()` applies DB values unconditionally, with no promotion check.
- So a live promotion gate exists in config while the code path that consumes promoted
  values never consults it. The gate is decorative for this path.

## Not a defect (checked, so it is not misreported)

`STOP_LOSS` is stored positive everywhere it is overridden, and consumed as
`if pct <= -STOP_LOSS` (`mh_reasoner.py:241`). The sign convention is internally
consistent. This is a naming wart worth normalising in the resolver, not a live bug.

## Required fix (Rule C + Rule F)

1. `domain/policy/resolver.py` becomes the only reader of thresholds.
2. The DB override must carry provenance: `source`, `approved_by`, `approved_at`,
   `config_version`. An override without provenance fails closed or is ignored.
3. `autotune_live_promotion_enabled` must actually gate the promotion path, or the flag
   must be deleted so nobody believes it is protecting anything.
4. `/api/system-manifest` (plan §13) should expose `policy_version` and the effective
   values, so the dashboard and any operator can see what is actually in force.

## Why this ranks above the other findings

The DB-path split (001/003) makes a tool read the wrong file. This makes the system
behave on a value that **exists nowhere in the source tree**. Only a resolver plus a
runtime manifest makes the active policy observable.
