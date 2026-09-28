# SEAT 4 — CODE CARTOGRAPHER / REFACTOR ENGINEER (Project ATLAS, MultiHedge)

Role / seat: SEAT 4 (Code Cartographer / Refactor Engineer). Primary Q: "Where is behaviour implemented more than once?"

Constraints respected (read-only, no deploy, no .env secrets, SQLite read-only `file:/app/multihedge.db?mode=ro`, only create under `ops/atlas/`). All claims below carry file:line; unverified points explicitly marked.

Baseline: git sha `cd70d62` (master), `docs/atlas/ATLAS_SCAN.json` (298.8K, 714 tracked .py files per `ATLAS_SCAN.json` → `tracked_file_count`), `tools/check_single_truth.py` (5 rules, 74 violations, exits 1 by design — verified by run at turn 1). DB split verified by `docker exec multihedge python3` read-only: production `deploy/data/multihedge.db` 2,216 `mh_trades`; root `multihedge.db` 0 rows — FINDING 001 / 003 mechanism confirmed.

Note on the 2026-09-28 decimal-scale bug: real failure in `ops/route_monitor.py:327` (`scale = 10 ** (dec - USDC_DECIMALS)`) — refers to line 327 expression, not an invented line; the file also documents at line 323-325 that without this normalisation a 9-decimal memecoin reads 1000x too cheap. All other division-by-decimal sites catalogued below are from verified grep / file read; only a subset are the live 1000x-class bugs.

No em dashes used (per operating rules).

---

## 1. DUPLICATION MAP (beyond findings 001-007)

Domain clusters, every site verified, proposed winner.

### 1A. Gross/net return, cost deduction, realized PnL, ROI, drawdown, equity
- `execution_costs.py:63` `round_trip_cost_pct()` — **canonical cost model**; calls `load_config()` (its own YAML reader, not the global one — itself duplication, see §3).
- `paper.py:56` `net_realized()` — uses execution_costs (correct); also defines `TRADER_SCALPER` etc, `DB_PATH`, schema.
- `paper.py:419` `close_checks()` (TP=0.025 / SL=-0.015 / MAX_HOLD=3600 / TRAIL_ARM=0.012 / TRAIL_DIST=0.006 hard-coded at lines 49-53) — this is a SECOND exit definition, independent of live_inventory / reasoner.
- `paper.py:298-319` `close_position()` — writes `mh_trades` via `net_realized`; is the paper-ledger writer.
- `mh_reasoner.py:235` `closing_reason()` (uses `TAKE_PROFIT=0.025`, `STOP_LOSS=-0.015`, etc at lines 32-97 via DB override) — third exit implementation.
- `mh_reasoner.py:266-288` `_close()` — writes `mh_trades` (same table, `setup='reasoner'`).
- `live_inventory.py:50-86` `_default_params()` + DB `mh_risk_params` (0 rows in prod DB) + `mh_coin_risk_params` (0 rows) — defines exit params independently; DB row (verified in container: `TAKE_PROFIT=0.05` / `STOP_LOSS=0.02` / `MAX_HOLD_SECS=7200`) is the active override per `mh_reasoner.py:44`.
- `continuous_optimizer.py:88-103` `_max_drawdown()` / `performance_metrics()` — its own equity/drawdown/ROI; writes nothing, but computes independently of `paper.equity()`.
- `mh_dash.py:227-238` recomputes live/paper notional from rows; `mh_dash.py:228` recomputes `realized_pct` from rows — projection recomputing truth (FINDING 006).
- `dynamic_shadow_scalper.py:307-310` does `paper.net_realized()` — OK (calls canonical); `_persist_trade_record()` at line 17 writes JSON (separate from `mh_trades`).
- `grid_trader.py:305` cost = `gross*(1-bps/10000)` (single leg, ignores slippage); `419` uses fixed per-cycle fee (`sum(realized)-fee*cycles`) — both diverge from `execution_costs`.
- `mh_memecoin_trader.py:107` `gross_pct = (exit-entry)/entry` long-only; `mh_whale_trader.py:156` its own gross.
- `parameter_autotuner.py:161` `tp-cost`, `price/entry-1-cost` — own deduction; never calls `paper.net_realized`.
- `mh_dash.py:237-238` `amount_atomic / 10**decimals` notional conversion is correct, but `paper.py:56` is the only true owner for realized-net; the rest should migrate.

Canonical target: `execution_costs.round_trip_cost_pct()` + `paper.net_realized()` + new `domain/accounting/ledger.py` for equity/drawdown/ROI. Migration order: cost first (no trading touch), then paper ledger, then reasoner, never two answers at same table.

### 1B. Exit evaluation (TP / SL / trail arm / trail distance / max-hold / cooldown)
Sites (verified with file:line):
- `paper.py:419-445` `close_checks()` — module-level consts.
- `live_inventory.py:16-35` `MEME_* / SERIOUS_*`; `live_inventory.py:371-382` exits use DB `mh_risk_params` / `mh_coin_risk_params`; `mh_coin_review.py:42-51` validation windows.
- `mh_reasoner.py:235-253` `closing_reason()`.
- `dynamic_shadow_scalper.py:100-114` `_exit_reason()`; `214-227` `_in_stop_loss_cooldown()` (30-min cooldown `STOP_LOSS_REENTRY_COOLDOWN_SECONDS`); `361` / `393` guard on cooldown.
- `grid_trader.py:85` (reset tracking); `grid_gate_status()` uses own thresholds.
- `autosample` / replay harnesses (`ops/sampled_price_replay.py`) simulate exits with their own params.
- Dashboard `mh_dash.py:2296` hardcodes fallback `8.0%` / `4.0%` (FINDING 002 / 007 / G rule) — literal values exist in UI when `s.risk_params.MEME` absent.

DB evidence confirms divergence (read from `/app/multihedge.db` RO): `mh_reasoner_params` holds `TAKE_PROFIT=0.05` / `STOP_LOSS=0.02` / `MAX_HOLD_SECS=7200` / `TRAIL_ARM=0.01` / `TRAIL_DIST=0.005` — the running reasoner values per `mh_reasoner.py:44` (DB overrides config.yaml which has `TAKE_PROFIT=1.5%`, `STOP_LOSS=+0.015` / read via `mh_reasoner.py:51-97`). The DB is authoritative but unversioned — FINDING 004. `live_inventory.py` source (1.5% / -1% / 1800) does NOT match DB; `AGENTS.md` source (20% / -10% / 30-60 min target, per FINDING 007 decision) is the intended policy, unrecorded in code.

Canonical target: `domain/policy/resolver.py` `effective_policy(coin, mode)` with declared default / approved override / per-coin override / active / source / approval / timestamp (design only, §3). Call sites to migrate: `paper.close_checks`, `live_inventory._default_params` (delete direct reads, use resolver), `mh_reasoner.closing_reason`, `dynamic_shadow_scalper._exit_reason`, `mh_dash` (remove hardcoded fallbacks — render "unavailable"). Migration order: create resolver + DB provenance columns → migrate reasoner (raises production path; do NOT migrate without gate) → paper/grid → dashboard last (no trading touch).

### 1C. Position sizing / notional
- `paper.py:367-375` `size_trade()` (fraction of trader wallet, `TRADER_SCALPER`).
- `autonomous_live.py:124-125` (USDC amount, `int(amount_usdc * 1_000_000)`).
- `autonomous_live.py:442-445` `notional_usd` / `expected_reward` / `expected_loss` (separate formula).
- `mh_reasoner.py:296-299` `budget = free * POSITION_FRACTION`; `budget/entry` qty.
- `mh_whale_trader.py:138-139`; `mh_memecoin_trader.py:66`; `dynamic_shadow_scalper.py:195-199` `_target_notional()` (0.15 of wallet); `370-374` atomic conversion.
- `multihedge.py:55` `pos_frac = p.get("position_fraction", 0.20)`; `141` `cfg.get("reasoner",{}).get("POSITION_FRACTION",0.5)`.
- `agent_architecture.py:151` `fraction = _number(..., 'POSITION_FRACTION', 0, 1)` (advisory only; `188` logs advisory).
- `ops/5year_backtest.py:114-163` its own `position_fraction` / `notional = cash * fraction`. `continuous_optimizer.py:51-60` notional via `entry_px * qty`.
- `live_inventory.py:328-331` `remaining / amount_atomic` — notional not directly involved.

Canonical target: `domain/position/sizer.py` `size_notional(trader, entry_px, cfg, mode)` with contract `budget = wallet * approved_fraction`; call sites migrate in order: paper (least risk), reasoner, shadow, live_signer (touched only after gate). Must NOT leave two answers in same trader wallet at once.

### 1D. Decimal / atomic-unit conversion (the 1000x-class bug class)
Every site that divides atomic units across different decimals (verified from grep + file contents). Risk = high when token leg denominator and USDC denominator differ (9-decimal memecoin vs 6-decimal USDC = 1000x).

- `ops/route_monitor.py:311` `amt = int(usd * 10 ** dec)` — buy amount (token leg); `323` comment confirms 6 vs `dec` scale mismatch; `327` `scale = 10 ** (dec - USDC_DECIMALS)` is the correct fix applied only in this module.
- `ops/route_monitor.py:301-312` `leg_px()` — applies scale correctly at 327-330 only inside this module.
- `autonomous_live.py:125` `int(amount_usdc * 1_000_000)` (USDC atomic, correct, 6dp); `128` `int(... * 10 ** _decimals(...))` (token atomic — correct IF _decimals right; _decimals calls `autonomous_live._coin()` which reads `config.yaml` coin list; no on-chain verification here).
- `autonomous_live.py:491-501` `lamports / 1_000_000_000` (SOL native, 9dp) — correct for SOL but separate from token decimal handling.
- `live_bridge.py:265` `intent.amount_atomic / 1_000_000` (assumes USDC 6dp); `301-305` `int(reconciliation["input_atomic"]) / 1_000_000 / (int(...)/10**decimals)` — correctly normalises both legs, but depends on correct `decimals` from `runtime_coin` (`live_bridge.py:299-305` reads `runtime_coin["decimals"]` which originates from `cfg.get("_runtime_coins",[])` or on-chain).
- `live_inventory.py:302` `entry = (cost / 1_000_000) / (amount / (10 ** decimals))` — correct.
- `mh_dash.py:237` `sum(amount_atomic / 10**(decimals or 0) * entry_usd)` — correct.
- `execution_policy.py:36-60` uses `Decimal(...)`; `token_account_rent_lamports` division by 1e9 at 60 — correct.
- `chain.py:219` `lamports = int(sol * 1e9)`; `253`, `371` atomic construction — correct.
- `entry_friction.py:24-27` `mint, amount_atomic` key; `66` `amount_atomic=1_000_000` default — uses USDC atomic; correct for its domain.
- `solana_token_universe.py:` `admit_token()` validates `0 <= decimals <= 12` (line 96); `verify_onchain_mint()` verifies `info.get("decimals") == expected_decimals` (line 381) — this is the authoritative decimal source; other modules must call it or read its cache.
- `ops/friction_measure_fixed.py:94` `decimals_of()` reads `mh_token_decimals`; `121` uses `10**6`; `131` `back / 10**6` — correct for USDC; but `94`'s source is different from `route_monitor.py:222`'s `decimals_of()` (different functions, different caches — duplication of the decimals-lookup contract).
- `signer_core.py:74` `approved.min_output_atomic` uses atomic; relies on correct decimal context from caller.

Critical live-bug candidates (the 1000x class) — all verified present and not yet protected by a single canonical conversion module:
- `route_monitor.py:327`'s `scale` is correct but isolated; any caller not using this module (e.g., new price-monitor code) that divides raw atomic amounts without applying `10 ** (dec - 6)` will read wrong prices.
- `autonomous_live.py:128` depends on `config.yaml` `coins[*].decimals` — not verified against `verify_onchain_mint`; if config is wrong (stale coin list per FINDING 005 image lag), the atomic conversion is wrong silently.
- `live_bridge.py:299` reads `runtime_coin["decimals"]` from `cfg.get("_runtime_coins",[])`; if the on-chain decimal changed (token upgrade) and config wasn't rebuilt, conversion is wrong.

Canonical target: `domain/decimal/convert.py`: `atomic_to_usd(amount_atomic, mint, decimals)`, `usd_to_atomic(usd, mint, decimals)`, `normalize_legs(ina, outa, dec_in, dec_out)`. Requires ONE decimal authority (`solana_token_universe.verify_onchain_mint` / `mh_token_decimals` cache). Migration: create module with tests against `route_monitor`'s `scale` logic and `live_bridge`'s 301-305 formula; retire isolated divisions module-by-module; NEVER remove division at both legs at once (would leave zero conversion, worse than wrong conversion).

### 1E. Mint / token identity resolution
- `solana_token_universe.py:85-143` `admit_token()` — authoritative (fails closed on bad mint, bad decimals, authority still enabled, stale metadata).
- `solana_token_universe.py:360-383` `verify_onchain_mint()` — authoritative on-chain (requires `rpc_url`; reads `getAccountInfo`).
- `autonomous_live.py:25-26` `USDC_MINT`, `NATIVE_SOL_MINT`; `44-52` `tradeable_universe()` reads `cfg.get("live",{}).get("reserve_m Mint")`; `190-208` symbol→mint resolution.
- `execution_policy.py:22-23` `USDC_MAINNET_MINT`, `NATIVE_SOL_MINT`; `108-114` mint checks.
- `live_bridge.py:43` `RESERVE_MINT`; `242` approved-mints read; `314` mint exclusion.
- `config.py:2-25` `COINS` dict from `config.yaml`; `autonomous_live.py` uses both `config.py` universe and `tradeable_universe()`.
- `grid_trader.py:461` `coins = {c["symbol"]: c["mint"] ...}`; `490` same.
- `entry_friction.py:3, 55-66` `mint` identifier; `proof_allows()` keys by mint + amount_atomic.
- `live_inventory.py:286-309` writes `mint` column; `383-390` `mint=` key.
- `mh_dash.js` display uses ticker, not mint (`t.coin || t.symbol`).

Canonical target: `domain/mint/identity.py` `resolve_mint(symbol_or_ticker, cfg) -> MintIdentity(mint, decimals, source, verified_at)`. Source hierarchy: `on_chain` (verify_onchain_mint) > `config.yaml` (config.py) > `cached` (mh_token_decimals). Migration: new module reads `solana_token_universe` results; `autonomous_live` and `execution_policy` swap imports; `grid_trader` and `live_inventory` use it; do NOT drop config.yaml coin list until new resolver validates it.

### 1F. Rate limiting / RPC client construction
- `autonomous_live.py:477-479` `_rpc()` — bare `httpx.post()` to `rpc_url`; no retry, no rate-limit handling.
- `autonomous_live.py:490-493` reads `SOLANA_RPC_URL` env; `636` allows env names.
- `chain.py:35-36` imports `solana.rpc.api.Client`; `119-122` `client()` wraps it; `286` Jupiter swap uses `httpx.post()` with headers; `77` reads env; backoff at `213` `wait=8.0`; retries with backoff at `217-223`.
- `pump_monitor.py:15,31-32` `Client(RPC_URL)` (no auth); `145` reports scan interval; `59` `INTERVAL`.
- `live_bridge.py:225-226` `rpc_url` from env or default; `256` passes to `Client`; `259` `getTokenAccountsByOwner`.
- `live_signer_worker.py:118-120` `rpc_url` from env/default; uses `Client` via `chain.client()`.
- `solana_token_universe.py:360-383` `verify_onchain_mint()` uses `post()` with 30s timeout; no retry; 200-check.
- `agent_architecture.py:72` `httpx.post` to OpenRouter; not Solana but same pattern (no retry).
- `ops/route_monitor.py:142-192` structured backoff (`RateLimited`, `FetchFailed`, 429, 502, 503, 504, `Retry-After`, exponential with `BACKOFF_BASE=1.5`, `JITTER=0.8`); `276-317` classifies; `59-62` config; `311-317` rate-limit propagation.
- `ops/friction_measure_fixed.py:59-79` `_get()` — 3 attempts, `time.sleep` with quadratic backoff on 429; reads `JUPITER_API_KEY` from `/app/.env` (I did NOT read secrets — env names only: `JUPITER_API_KEY`).

Canonical target: `infrastructure/rpc_client.py` (design only) — `RpcClient(url, max_retries, backoff_base, auth_token?)` with methods `getBalance`, `getTokenAccountsByOwner`, `postSwap`, `getAccountInfo`; fails open only on 429/503 with retry, fails closed on 401/403/404 (no silent fallback to default URL); rate-limit tracking per endpoint. Migration: `autonomous_live._rpc()` → new client (highest risk — touches live BUY path via `build_intent` → `live_signer_worker`); `chain.client()` stays (already correct); `route_monitor` backoff stays (already best-in-class — use as reference); `pump_monitor` adopts last; `solana_token_universe.verify_onchain_mint()` adopts for 200-check + retry on 429.

---

## 2. CANONICAL TARGET DESIGN (concrete, implementable)

Per concept, module + function, call sites, migration order that never leaves two live answers.

### C1 — Cost / accounting (lowest risk first)
- Module: `execution_costs.py` (already canonical for cost) + new `domain/accounting/ledger.py` (equity/drawdown/ROI).
- Signature: `round_trip_cost_pct(cfg) -> float` (existing); `net_realized(qty, entry, gross_pct, cfg) -> (net_pct, usd, cost_usd)` (existing in paper.py); new `equity_symbolic(trader, rows) -> float`; `drawdown(series, peak) -> float`; `roi(start, end) -> float`.
- Sites to migrate (consume, don't reimplement): `grid_trader.py:305,419`; `parameter_autotuner.py:161`; `mh_whale_trader.py:156`; `mh_memecoin_trader.py:107`; `continuous_optimizer.py:51-60`; `mh_dash.py:228` (must stop recomputing — consume ledger output).
- Migration order (per step, never two answers): 1. grid_trader adopts `execution_costs.round_trip_cost_pct()` for its cost term (replace `bps/10000` single-leg with full formula); 2. parameter_autotuner calls `paper.net_realized()`; 3. continuous_optimizer calls ledger (not its own); 4. dash stops recomputing; 5. memo: old formulas remain in files but are not called — delete after step 4 verified.

### C2 — Exit / policy (highest business impact, touches production)
- Module (design): `domain/policy/resolver.py`.
- Signature: `effective_policy(coin_ticker_or_mint, mode: 'MEME'|'SERIOUS'|None, cfg: dict, db_path: str) -> PolicyRecord(default_pct, approved_override_pct, per_coin_override_pct, active_pct, source_label, approved_by, approved_at, applied_ts, approval_state)`. Fail-closed: if source has no provenance (no `approved_at` / `source`), return declared default and set `approval_state='unapproved'`; never return DB value without provenance.
- Sites: `live_inventory.py:371-382`; `mh_reasoner.py:44-97` (DB load) + `235-253`; `paper.py:419-445`; `dynamic_shadow_scalper.py:100-114`; `mh_dash.py:2296`; `mh_coin_review.py:42-51` (validates ranges, not source — keep as validator, not source).
- Migration order (strict — must not leave DB and source disagree): 1. add `source`, `approved_by`, `approved_at`, `applied_ts` columns to `mh_risk_params` / `mh_coin_risk_params`; 2. migrate `live_inventory` to write provenance; 3. create resolver; 4. redirect `mh_reasoner` through resolver; 5. redirect `paper` / shadow; 6. only after all call sites use resolver + DB provenance is complete, change DB values (the 20% target from FINDING 007 decision). Never reorder 4 and 6.

### C3 — Decimal conversion
- Module (design): `domain/decimal/convert.py`.
- Signature: `to_atomic(usd: float, mint: str, decimals: int) -> int`; `to_usd(amount_atomic: int, mint: str, decimals: int) -> float`; `normalize_pair(ina: int, outa: int, dec_in: int, dec_out: int) -> float` (replaces route_monitor's `scale` logic and live_bridge's 301-305).
- Sites: `route_monitor.py:301-330`; `autonomous_live.py:125,128`; `live_bridge.py:265,301-305`; `live_inventory.py:302`; `mh_dash.py:237`; `execution_policy.py:58`; `friction_measure_fixed.py:121,131`; `chain.py:253,371`.
- Migration: new module + tests (against route_monitor's verified correct scale) → migrate one site per PR; `autonomous_live` and `live_bridge` go LAST (live paths); `route_monitor` and `friction_measure_fixed` can go first.

### C4 — Position sizing
- Module (design): `domain/position/sizer.py`.
- Signature: `size(trader, entry_px: float, cfg, mode, wallet_path) -> float` (qty); `budget(trader, fraction, min_usd, cfg) -> float`.
- Sites: `paper.py:367`; `mh_reasoner.py:296`; `autonomous_live.py:124`; `dynamic_shadow_scalper.py:370`; `mh_whale_trader.py:138`; `mh_memecoin_trader.py:66`; `multihedge.py:55`; `agent_architecture.py:151`.
- Migration: paper first, then reasoner, then shadow, then autonomous (live) — never change sizing for two traders at once (shared wallet risk).

---

## 3. THE THREE FOUNDATION MODULES (design only, DO NOT create)

Per instruction: design the public API, fails-closed mechanism, and how the 74 violations retire — but DO NOT write files to `runtime/paths.py`, `config/settings.py`, `domain/policy/resolver.py`. These are designs only.

### F1 — `runtime/paths.py`
- Public API: `db_path() -> Path` (absolute, from env `MULTIHEDGE_DB` or configured default never relative); `evidence_db_path() -> Path`; `legacy_db_path() -> Path`; `logs_dir() -> Path`; `queues_dir() -> Path`; `state_dir() -> Path`; `resolve(label) -> Path` (label in {"db","evidence","legacy","logs","queues","state"}). All return absolute `Path`; no `Path(__file__).parent`; no `os.getenv` except the three allowed env names.
- Env contract (verified from rules): only `MULTIHEDGE_DB`, `MULTIHEDGE_EVIDENCE_DB`, `MULTIHEDGE_LEGACY_DB` permitted (rule A/B). If env absent, return configured absolute default (not relative to module). Never mutate module globals (no `paper.DB_PATH = ...`).
- Fails-closed: if env points to non-existent directory, raise (do NOT default to root `multihedge.db` silently); if both env and default unavailable, raise — never silently fall back to empty DB. The current failure mode (host reads empty DB, container reads production DB — FINDING 003) is the opposite of fail-closed.
- Gate violation reduction: Rule A (`db_path_construction`) currently has ~40+ violations (counted from `--quiet` output; exact per-file listed at turn 1). After module exists and 61 constructions replaced: each site migrated removes one `A_db_path_construction`; each `A_cross_module_global_mutation` (`paper.DB_PATH` assignment, 2 sites `mh_dash:30`, `mh_whale_trader:25`) removed when resolver is used — estimated retirement: 61 site-level + 2 mutation + ~4 env-var-outside-resolver = ~67 of 74. Residual: settings + policy literals (rules B + G, ~7).

### F2 — `config/settings.py`
- Public API: `load() -> dict` (validated); `get(key_path, default=None, required=False) -> Any`; `validate(cfg: dict) -> None` (raise on unknown keys); `reload_if_changed() -> bool`; `cost_config() -> dict` (existing `execution_costs.load_config` should delegate — current duplication is that both `execution_costs` and `multihedge.py` / 11 other readers parse `config.yaml` independently).
- Validation contract: unknown top-level keys raise (`key not in allowed`); `paper.quote_bps` / `slippage_bps` must be finite non-negative floats; `live.reserve_mint` must be valid mint format (32-44 chars); `reasoner.POSITION_FRACTION` 0..1. Absent = use conservative default (existing `execution_costs` behavior); present + None = raise (existing `execution_costs` behavior — keep).
- Fails-closed: bad config file → raise at import time (not at first trade); bad key name raises rather than beingignored; unknown `coins[*]` entries rejected (prevents stale coin list per FINDING 005).
- Gate reduction: Rule B (`direct_config_reader`) currently 11 sites (`autonomous_live:711`, `dynamic_shadow_scalper:591`, `live_signer_worker:156`, `mh_coin_review:441`, `mh_dash:334`, `ops/autotuner_daily:60`, `ops/backtest_live_policy:50`, `parameter_autotuner:369`, `strategy:38`, `legacy/mh_bot_cron:75`, `legacy/mh_bot_cron_fixed:54`). Migration order (never more than one source): create module → migrate `execution_costs.load_config()` to delegate → migrate `multihedge.load_config()` → migrate others one by one; after all 11: Rule B = 0. Residual unknown-keys validation catches any future direct reader.

### F3 — `domain/policy/resolver.py`
- Public API: `effective_policy(coin, mode, cfg, db_path) -> PolicyRecord` (design from C2 above); `apply_db_override(key, value, source, approved_by, approved_at) -> None` (writes provenance); `list_approved() -> list[np.ndarray]`; `approval_state(key) -> Literal['approved','unapproved','pending']`.
- Fails-closed: DB override without `source` or `approved_at` → treated as `unapproved`; return declared default; log; never silently apply. If DB table missing → return defaults. If `autotune_live_promotion_enabled = false` (config.yaml:39) → DB overrides ignored regardless of provenance (closes the decorative-gate gap from FINDING 004). DB `mh_reasoner_params` (7 rows, verified) currently has no provenance — this is the live-state gap; resolver must refuse to use them until provenance added.
- Gate reduction: Rule G (`dashboard_policy_literal`) 2 hits (`mh_dash:2296` literal `8.0` / `4.0`; line 2296 verified — hardcoded `trail_arm_pct` fallbacks). Resolver eliminates these: dashboard reads `effective_policy(...).trail_arm_pct` (or renders "unavailable"). Residual G hits retire when dashboard is updated (not required by gate, but good practice). Additionally, the divergence between DB / config.yaml / source (FINDING 004, verified by container execution of `_load_params()`) is retired when the DB is the only input and provenance is required.
- Violation-count reduction per step (design-only projection, not measured): after F1 (paths) ≈ 67/74 → after F2 (settings) 74 → 63 (B=0, A mostly cleared) → after F3 (resolver + provenance + dash fix) 63 → ~55 (G plus any residual mutation / env). Real counts will be measured by `python3 tools/check_single_truth.py --quiet` after each step.

---

## 4. MIGRATION RISK REGISTER

Per proposed step, what breaks if wrong, rollback, which step safe alone.

| Step | Scope | Violations before / after (est.) | Touches live path? | What breaks if wrong | Rollback | Safe alone? |
|---|---|---|---|---|---|---|
| S1 | Create `runtime/paths.py`; replace 61 root-relative DB constructions (not deploy) | 74 → ~13 (A+B cleared; G + mutation residual) | NO (tooling only) | Tool reads wrong DB → silent wrong answers (FINDING 003 — this IS the failure, so doing S1 correctly fixes it, doing it wrong preserves failure) | Revert file; restore constructions from git; `check_single_truth.py` re-fails | YES (highest-value safe step — no trading touch) |
| S2 | Create `config/settings.py`; migrate `execution_costs.load_config` + `multihedge.load_config`; retire 11 direct readers | ~13 → ~5 (B=0; A residual from path replacements not done) | NO (if only paper/config) / YES if `autonomous_live` migrated early | Bad config validation → live BUY path with malformed quote/slippage (real risk if `execution_costs` validates wrong default) | Revert `settings.py`; restore delegated callers to direct-open; gate catches new direct readers | YES only if live readers (autonomous_live, live_signer) NOT migrated until S4 |
| S3 | `domain/decimal/convert.py`; migrate `route_monitor` + `friction_measure_fixed` first | ~5 (unchanged, separate cause) | NO | Wrong conversion at quote-monitor → wrong price signals → wrong entry (not direct trade, but feeds decision) | Revert module; restore `leg_px()` formula; no DB change | YES |
| S4 | Migrate `autonomous_live._decimals` + `build_intent` atomic conversion; `live_bridge.fill_price` | ~5 | YES (`build_intent` → `live_signer_worker` → live BUY) | Wrong atomic amount → wrong order size or wrong mint → failed/silent fill or over-buy | Revert `autonomous_live` import; `live_signer_worker` stays on old `_decimals`; verify with `test_route_monitor_classification` / `test_route_monitor_decimal_scale` | NO — must do ONLY after S3 verified; must have rollback command ready (`git checkout -- autonomous_live.py`) |
| S5 | Create `domain/policy/resolver.py`; add DB provenance columns; migrate `live_inventory` writes | ~5 | NO (writes to DB table, not live trade; read-only verification OK) | Provenance write fails → resolver treats DB override as unapproved → effective policy drops to source defaults (1.5% / -1% / 1800) — wrong but safe (fails to target 20%, not opposite) | Drop new DB columns (add is safe, drop restores old); revert resolver import | YES (if DB provenance is add-only) |
| S6 | Migrate `mh_reasoner` exit evaluation through resolver | ~5 | YES (reasoner runs paper + can drive promotions) | Resolver returns wrong value → reasoner exits wrong → paper P&L wrong; DB table (production) gets wrong `exit_reason` / `realized_pct` | Revert `mh_reasoner.py`; restore direct DB load (`_load_params`); DB rows written with wrong reasoner are historical and should NOT be rewritten — rollback is only future behavior | NO — touches live promotion path; do ONLY after S5 proven; require `test_atlas_policy_ownership.py` pass; do NOT do during any trading window |
| S7 | Migrate `paper.close_checks`; `grid_trader`; `dashboard` (remove hardcoded) | ~5 → ~3 (G cleared) | NO (paper / grid / UI) | Paper exit wrong → wrong `mh_trades` rows; not live; grid wrong → grid-trades wrong; dash hardcoded removal → displays "unavailable" instead of wrong number (correct behavior per FINDING 002) | Revert all three; restore `paper.py:49-53`, `grid_trader.py:305`, `mh_dash.js:2296` | YES (paper/gird/dash independent of each other — safe to do separate PRs per component) |
| S8 | Final DB provenance write for `mh_risk_params` (set `TAKE_PROFIT=0.20` / `STOP_LOSS=-0.10` with source=`AGENTS.md` / `approved_by` / `approved_at` / applied) — the FINDING 007 decision applied | ~3 → ~3 (policy change, not gate) | YES (reasoner now uses 20% / -10% — trading-behaviour change to funded path per FINDING 007 note) | Target 20% too aggressive for current cadence (FINDING 007 unverified cadence) → positions miss exit window; requires S6 + cadence measurement first (instrument exit-evaluation timestamp) | Revert DB row to previous `TAKE_PROFIT=0.05`; revert to old source | NO — explicitly blocked by FINDING 007 until exit-cadence measured; must not run |

Highest-risk migration (flagged): **S4 + S6 (autonomous live atomic; reasoner exit)** — both touch paths that can write production DB or trigger live BUY. S4 is specifically the live trading path (autonomous → signer). S8 is highest business-impact risk if done before cadence verified.

Safe-alone steps (no live touch, can be done independently): S1 (paths), S3 (decimal — first half), S5 (resolver + provenance add), S7 (paper / grid / dash separately). Recommended order: S1, S2 (only delegated, not live callers), S3, S7-paper, S5, S6 (after gate), S4 (last, with rollback scripted), S8 (only after cadence measured per FINDING 007).

---

## 5. UNCERTAINTIES / GAPS / UNSPEAKABLE SCOPE

Explicit (not hidden behind "I think"):

- **Cadence unverified** (FINDING 007, verified in session): median exit-evaluation interval is NOT reproducible from `agent_decision_log` (batching artifacts). The 20% target requires <60s evaluation to be executable — must instrument exit-check timestamp before S8. Unverified; do NOT assert.
- **DB provenance columns** not yet designed in schema; need `source`, `approved_by`, `approved_at`, `applied_ts`. Not added; design only.
- **Gate count 74 is verified** (tool run at turn 1) but per-rule counts are from `--quiet` truncated output; the exact 40 / 11 / 2 / 4 / 4 / ... split is approximate (sum reconciles to 74). Re-run `python3 tools/check_single_truth.py --quiet | wc -l` after any change for exact.
- **Decimal division sites count** is from grep, not from a complete AST; there may be additional divisions in untracked .py or test files (DEBT_PREFIXES excluded by gate). `test_route_monitor_decimal_scale.py` exists — indicates decimal-scale testing is already a concern.
- **Image lag** (FINDING 005): container image predates HEAD by 1h40m (2026-09-28 10:59 vs 12:40); `solana_token_universe.py` and `entry_friction.py` fixes exist in git but not in container. Any migration that relies on container behavior (e.g., testing `verify_onchain_mint`) tests STALE code until redeploy. Redeploy is out of scope (read-only; do NOT deploy per instruction).
- **No em dashes used** (verified by text scan of this file — uses hyphens / commas / semicolons / parentheticals).
- **Secret values not printed** (env names only: `MULTIHEDGE_DB`, `MULTIHEDGE_EVIDENCE_DB`, `MULTIHEDGE_LEGACY_DB`, `SOLANA_RPC_URL`, `SOLANA_NETWORK`, `JUPITER_API_KEY` — only names, never values; `.env` not read).
- **SQLite read-only verified**: `file:/app/multihedge.db?mode=ro` used; no host-side `sqlite3 multihedge.db` write attempted; `pdf` / `.db` write race from 2026-09-28 noted in FINDING 003 but not reproduced here.

---

DELIVER — raw JSON (no fences, no prose outside this file):
