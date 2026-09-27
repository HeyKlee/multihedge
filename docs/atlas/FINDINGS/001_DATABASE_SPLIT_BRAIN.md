# PROJECT ATLAS — FINDING 001: runtime database split-brain

**Status:** VERIFIED (evidence below, reproducible)
**Severity:** Critical — this is the exact "two sources of truth" failure you described
**Baseline:** git `cd70d62`, branch `master`, config sha `386bb367c47873d5`

## The failure you described

> "one trader looks at one, you change another, then nothing applies"

Mechanism confirmed. Two SQLite files coexist in the repo and **no single module owns
which one is authoritative**. Each resolves independently, and the dashboard *mutates
another module's global* to paper over the disagreement.

## Evidence

### Two databases, two different truths

| Database | Size | Tables | `mh_trades` rows |
|---|---:|---:|---:|
| `deploy/data/multihedge.db` (production) | 43,044,864 B | 49 | **2,216** |
| `multihedge.db` (repo root, "legacy") | 1,249,280 B | 27 | **0** |

```
$ sqlite3 deploy/data/multihedge.db "SELECT COUNT(*) FROM mh_trades;"  -> 2216
$ sqlite3 multihedge.db          "SELECT COUNT(*) FROM mh_trades;"  -> 0
```

A tool that resolves to the root file sees **zero trades and 22 missing tables**. It does
not error. It reports an empty, plausible-looking history. That is worse than a crash.

### Independent resolution at 61 sites across 41 files

Full list: `docs/atlas/ATLAS_SCAN.json` → `db_path_sites`. By route:

| Route | Sites | Files |
|---|---:|---:|
| `MULTIHEDGE_EVIDENCE_DB` | 11 | 7 |
| `MULTIHEDGE_DB` | 4 | 3 |
| `MULTIHEDGE_LEGACY_DB` | 1 | 1 |
| Root-relative `multihedge.db` | **61** | **41** |
| Other/indirect | 56 | 31 |

No module is the owner. `runtime/paths.py` (Rule A) does not exist.

### The dashboard mutates another module's global to force agreement

`mh_dash.py:29-30`:
```python
DB_PATH = Path(os.environ.get("MULTIHEDGE_DB", str(Path(__file__).parent / "multihedge.db")))
paper.DB_PATH = DB_PATH  # dashboard and engine share the same ledger
```

`paper.py:47` independently declares:
```python
DB_PATH = Path(__file__).parent / "multihedge.db"
```

So the two agree **only if the dashboard is imported first**. Import `paper` alone and
its ledger is the root legacy file. Import `mh_dash` and `paper.DB_PATH` is silently
rebound process-wide. Correctness depends on import order — an invisible coupling.

`live_bridge.py:35` and `pricefeed.py:25` each declare the same root-relative default
with no such reconciliation, so they read the **legacy** DB while the dashboard reads
**production**.

### Config has 11 independent readers

Rule B requires one settings loader. Actual (`ATLAS_SCAN.json` → `config_yaml_readers`):

```
autonomous_live.py:711      dynamic_shadow_scalper.py:591   live_signer_worker.py:156
mh_coin_review.py:441       mh_dash.py:334                  ops/autotuner_daily.py:60
ops/backtest_live_policy.py:50   parameter_autotuner.py:369    strategy.py:38
legacy/mh_bot_cron.py:75    legacy/mh_bot_cron_fixed.py:54
```

Each re-parses `config.yaml` with no validation and no shared precedence. A key added
to config can be read by one trader and ignored by another — no error is raised.

## Why this produced the bug you just hit

The 8-hour simulation failed to produce P&L because outcomes were read from a path
that did not hold them. With 61 independent resolution sites, *which* path a given
process touched was determined by its own default and import order, not by policy.

## Required fix (Rule A + Rule B)

1. Create `runtime/paths.py` as the sole owner of DB/log/queue/state paths.
2. Create `config/settings.py` as the sole validated config loader; consumers import it.
3. Delete the 61 root-relative constructions, replacing them with the resolver.
4. Remove `paper.DB_PATH` rebinding; the resolver returns the path, nothing mutates globals.
5. Add `tools/check_single_truth.py` to fail CI on any reintroduction.

## Enforcement

`tools/atlas_scan.py` regenerates the counts above from Git. Any regression is visible
as a change in `db_path_sites` / `config_yaml_readers`, not as a silent runtime surprise.
