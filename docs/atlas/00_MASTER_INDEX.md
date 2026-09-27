# PROJECT ATLAS — MASTER INDEX

**Status:** IN PROGRESS — mapping phase (no refactor begun, per plan §11 step 9)
**Baseline:** git `cd70d62` → see `ATLAS_SCAN.json.baseline`; config sha `386bb367c47873d5`
**Rule enforced here:** no refactor starts while mapping holes exist.

## Read this first

| If you want to know... | Go to |
|---|---|
| why two traders can disagree today | `FINDINGS/001_DATABASE_SPLIT_BRAIN.md` |
| why a config change may not apply | `FINDINGS/002_CONFIG_PRECEDENCE_AND_UI_MIRRORING.md` |
| what the machine sees right now | `ATLAS_SCAN.json` |
| how to re-verify any of it | `../../tools/atlas_scan.py`, `../../tools/check_single_truth.py` |

## Verified so far

Measured from the exact commit, regenerable, not hand-written:

| Fact | Count | Rule |
|---|---:|---|
| Git-tracked files | 718 | — |
| …of which generated artifacts | **545** | — |
| Python source modules | 44 | — |
| Test modules | 47 | — |
| Independent DB-path constructions | **133 sites / 41+ files** | A |
| …root-relative `multihedge.db` | **61 sites** | A |
| Cross-module global mutations | **31** | A |
| DB env vars read outside a resolver | **9** | A |
| Independent `config.yaml` readers | **11** | B |
| Dashboard policy literals | **2** | G |
| Environment variable names | 25 | B |
| **Total single-truth violations** | **74** | — |

The gate exits non-zero while these exist. That is intentional: it blocks until the
canonical modules exist.

## The two facts that matter most

1. **Two databases, two different truths.** `deploy/data/multihedge.db` = 2,216 trades /
   49 tables. Root `multihedge.db` = 0 trades / 27 tables. A tool resolving to the wrong
   one reports an empty, plausible history instead of failing.
2. **The dashboard rebinds another module's global** (`mh_dash.py:30`) to force the two to
   agree. So correctness depends on import order.

## What "100% mapped" requires before refactor

Per plan §3, the scanner must emit zeros for: `UNMAPPED_FILES`, `UNKNOWN_ENTRYPOINTS`,
`UNKNOWN_DB_WRITERS`, `UNKNOWN_CONFIG_KEYS`, `UNKNOWN_ENV_VARS`, `UNKNOWN_API_SOURCES`,
`UNKNOWN_WIDGET_SOURCES`, `UNKNOWN_RUNTIME_MOUNTS`, `UNKNOWN_POLICY_OWNERS`,
`DUPLICATE_CANONICAL_OWNERS`. All are non-zero today. `check_single_truth.py` is the
first of these gates and currently fails.

## Council status

| Seat | Model | Role | Status |
|---|---|---|---|
| 1 | GPT-5.6 Sol (chair) | orchestration, integration | active |
| 2 | xkiro `qwen3.8-max:free` | What exists? | running |
| 3 | xkiro `qwen3.8-max:free` | Where does truth come from? | running |
| 4 | xkiro `qwen3-coder-plus:free` | Where is behaviour implemented twice? | pending |
| 5 | xkiro `qwen3-coder-plus:free` | How is this map wrong? | briefed, pending |

Routes were probed live before dispatch (`PROBE_OK` on both). Fallback is
`openrouter/free`. No seat's conclusions are accepted without file:line evidence, and no
bot certifies its own implementation.

## Canonical modules still to be created

These do not exist yet, which is why the gate fails:

```
runtime/paths.py          sole owner of DB/log/queue/state paths
config/settings.py        sole validated config loader
domain/policy/resolver.py sole effective_policy()
```

Until they exist, every "canonical owner" in the plan is aspirational rather than real.
