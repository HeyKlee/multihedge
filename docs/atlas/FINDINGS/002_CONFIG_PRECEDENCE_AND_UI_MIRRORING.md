# PROJECT ATLAS — FINDING 002: config precedence implemented 11 times, dashboard mirrors policy

**Status:** VERIFIED
**Severity:** High
**Baseline:** git `cd70d62`, config sha `386bb367c47873d5`

## Part 1 — No single settings loader

Rule B requires one validated config loader. Actual: **11 independent readers** of
`config.yaml`, each re-parsing with no validation and no shared precedence.

```
autonomous_live.py:711              dynamic_shadow_scalper.py:591
live_signer_worker.py:156            mh_coin_review.py:441
mh_dash.py:334                       ops/autotuner_daily.py:60
ops/backtest_live_policy.py:50       parameter_autotuner.py:369
strategy.py:38                       legacy/mh_bot_cron.py:75
legacy/mh_bot_cron_fixed.py:54
```

Consequence: a key added to `config.yaml` is read by one trader and silently ignored by
another. No validation error, no warning. The system keeps running on a value that was
changed and is not in effect — the "you change another and then nothing applies" case.

## Part 2 — Divergent default for the same path

`autonomous_live.py:511-513` resolves the evidence DB to `deploy/data/multihedge.db`:

```python
db_path = Path(os.getenv(
    "MULTIHEDGE_EVIDENCE_DB", str(Path(__file__).parent / "deploy/data/multihedge.db")
))
```

`paper.py:47` / `live_bridge.py:35` / `pricefeed.py:25` resolve to the **repo root**
`multihedge.db`. Two different defaults for the same conceptual store, selected by which
module happens to be running. See FINDING 001.

## Part 3 — Dashboard hardcodes a policy number

Rule C/G: the dashboard must not define or mirror policy. In `mh_dash.py` the MEME
trailing-stop pullback is rendered with a **hardcoded `'4.0'`** literal fallback:

```js
...(s.risk_params.MEME ? (s.risk_params.MEME.trail_distance_pct*100).toFixed(1) : '4.0')}% pullback from peak
```

If the API omits `risk_params.MEME`, the UI displays **4.0%** — a number that exists
nowhere in policy and is not read from any resolver. The operator sees a confident
policy figure that the system is not actually enforcing.

This is a projection lying about its source. It is exactly the class of defect that
makes a dashboard untrustworthy for decision-making.

## Required fix

1. `config/settings.py` — sole validated loader; unknown keys fail validation.
2. All 11 readers migrate to it; `check_single_truth.py` fails CI on a new direct reader.
3. `domain/policy/resolver.py` — sole `effective_policy()`.
4. Remove hardcoded UI policy fallbacks; the dashboard must render "unavailable", never
   a number it did not receive.

## Enforcement

`tools/atlas_scan.py` → `config_yaml_readers` count and the dashboard literal grep.
Both must reach zero / none respectively.
