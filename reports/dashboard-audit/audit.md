# MultiHedge Dashboard Audit Report

**Date:** 2026-09-22 17:45 NZST (+12:00 UTC)
**Auditor:** iraia (deepseek/deepseek-v4-flash via openrouter)
**Dashboard URL:** http://127.0.0.1:9052
**Production DB:** deploy/data/multihedge.db (container-mounted at /app/multihedge.db)
**Configuration:** config.yaml (non-secret settings)

---

## Executive Summary

This audit systematically inspected all 9 dashboard tabs, 18 API endpoints, source code files, and the 336-test suite of the MultiHedge trading system. Total surfaces inventoried: 38. Total issues found: 27.

### 47 Issues Reconciliation

The user reports 47 original issues. An itemized list was not located in the repository (no git reference, markdown file, reports artifact, or kanban entry). The /api/widget-issues endpoint returns 3 issues (IDs 1, 2, 3). This audit independently found **27 verifiable issues** through systematic inspection of all 9 dashboard tabs, 18 API endpoints, source code, and the 336-test suite. Coverage of the original claimed 47 cannot be confirmed or refuted without locating the source list; the 27 here are a reproducible subset.

### Test suite state
336 tests ran: **27 FAILURES, 11 ERRORS, 4 skipped** (exit code 1). The false claim that "all tests passed" is categorically disproven. The suite is heavily broken across 12 test modules.

### Key themes
1. Exit param documentation/code/UI all disagree with each other
2. max_hold exit dominates both traders despite policy saying it is a fallback only
3. Dashboard description text is wrong for Scalper and Memecoin traders
4. _entry_signal signature change broke 15+ tests
5. AGENTS.md contains stale default values that are 5-10x different from actual code
6. Widget overlap bugs on Overview tab
7. No dangerous security issues found (no wallet exposure, no live trading risk)

---

## 47 Issues Reconciliation (Detail)

The user-provided figure is 47. This audit did not locate an original itemized list in the repository — no git reference, no markdown file, no reports directory artifact, no kanban entry. The /api/widget-issues endpoint returns exactly 3 issues (IDs 1, 2, 3). It is possible the list was maintained outside the repository (separate document, chat conversation, or issue tracker).

This audit independently reproduces 27 verifiable issues through systematic inspection of all 9 dashboard tabs, 18 API endpoints, source code, and the 336-test suite. Coverage of the original claimed 47 cannot be confirmed or refuted without the source list. The 27 findings here form a reproducible subset.

---

## Issue Breakdown by Severity

| Severity    | Count | IDs |
|-------------|-------|-----|
| High        | 9     | i-001, i-002, i-003, i-004, i-006, i-009, i-010, i-011, i-012, i-013, i-014, i-026 |
| Medium      | 6     | i-005, i-007, i-008, i-015, i-016, i-018 |
| Low         | 6     | i-017, i-019, i-020, i-021, i-025, i-027 |
| Information | 2     | i-022, i-023 |
| Correct     | 1     | i-024 |

---

## Surface Inventory Summary

| Tab            | Widgets | Status Notes |
|----------------|---------|--------------|
| Overview       | 4       | 2 widgets have overlap issues (heatmap hidden by ticker) |
| Xora-Survival  | 8       | Complete separation of paper/live evidence; exit params correct per code |
| Market         | 1       | Chart with coin selector and timeframes; M disabled |
| Live Gate      | 1       | Gate table correct per API; header text 75% conflicts with config 66.67% |
| Scalper        | 4       | Description text wrong (2.5% vs 1.5% TP); exit chart shows 58% max_hold |
| Reasoner       | 4       | Exit chart shows 69% max_hold violating policy |
| Whale Copy     | 6       | All 5 wallets show exactly 15 tx/7d (sampling cap); no trades executed |
| Memecoin       | 5       | Description claims +50% TP but code uses 1.5%; all states empty |
| Grid           | 5       | All states active and consistent; no discrepancies found |

---

## Detailed Findings

### High Severity

**i-001 max_hold exit dominates both traders (policy violation)**
- Scalper: 479 of 825 exits (58%) are max_hold
- Reasoner: 463 of 667 exits (69%) are max_hold
- AGENTS.md states: "Max hold is a missed-TP execution fallback only ... Age alone never closes a position"
- This policy is violated in practice — max_hold is the primary exit, not a fallback
- Test: test_max_hold_only_falls_back_after_take_profit_was_missed also fails (returns {} instead of {max_hold:1})

**i-002 Overview widget overlap (ticker covers heatmap)**
- Live Market tick scroll overlay covers Last cycles heatmap widget
- The heatmap has server-side bounds of h=0,w=0,x=0,y=0 (zero layout space)
- Reported as widget-issues #1 and #2; still unfixed

**i-003 Scalper card description text wrong**
- Claims "2.5% TP / 1.5% SL, ~1h max hold"
- Actual code: MEME_TAKE_PROFIT_PCT=0.015 (1.5%), MEME_STOP_LOSS_PCT=-0.015 (1.5%), MEME_MAX_HOLD_SECONDS=1800 (30min)
- TP off by 1.0pp, max hold off by 30min

**i-004 Memecoin card description text massively wrong**
- Claims "+50% TP, -20% SL, 15-minute max hold, trailing +30%/-10%"
- Actual code: MEME TP=1.5%, SL=-1.5%, 30min max hold, 2% trail arm, 1% trail distance
- All five parameters wrong by factors of 2-30x

**i-006 Exit param "default_locked" source label is misleading**
- Values displayed (MEME 1.5% TP, SERIOUS 1.0% TP) were tightened via commit 35d01cb
- AGENTS.md documents completely different values (+20% TP, -10% SL, 900s for MEME)
- Label 'default_locked' implies these are the original defaults, which is false

**i-009 Test suite: 38 failures/errors out of 336 tests**
- 27 FAILURES + 11 ERRORS
- Exit param drift: config.yaml says 1.5% TP, runtime applies 5.0%
- MEME defaults test expects 5% TP, code has 1.5%
- _entry_signal signature change broke 15+ tests across 3 modules
- Dashboard trail_policy test expects 8% arm, code returns 2%
- Autotuner search space missing expected 8% trail arm
- Replay and scalper modules fail to close any trades in tests

**i-010 Config/runtime drift for reasoner TAKE_PROFIT**
- config.yaml declares TAKE_PROFIT=0.015 (1.5%)
- Runtime applied value is 0.05 (5.0%) — 3.5pp drift
- Cause: DB table mh_reasoner_params overrides config before config takes effect

**i-011 MEME default constants disagree with tests**
- Test expects MEME_TAKE_PROFIT_PCT=0.05 (5%)
- Code has MEME_TAKE_PROFIT_PCT=0.015 (1.5%)
- Tests are testing the old intended values, code was tightened

**i-012 Dynamic shadow scalper broken (11 test failures)**
- _entry_signal() signature changed: missing required 'now' parameter
- No trades open or close in test scenarios
- entry_filter tests also fail for same reason

**i-013 Volatility gate broken (4 errors)**
- _entry_signal() no longer accepts trend_filter_enabled keyword argument
- The volatility gate integration is completely broken

**i-014 Survival repair tests broken (9 failures)**
- All DB queries return None — mh_dynamic_scalp_positions and mh_scalp_policy_evidence tables not accessible
- The entry/close logic regression affects the entire survival repair module

**i-026 Widget editor auto-refresh bug**
- 120-second auto-refresh during edit mode causes widget overlay
- Bottom area becomes blocked after refresh, preventing widget placement
- Reported as widget-issue #3 (severity: broken, status: new)

### Medium Severity

**i-005 Open position count inconsistent across tabs**
- Overview: "5 open positions"
- Xora-Survival: "4 open positions"
- Likely real-time race but confusing

**i-007 All 5 whale wallets show exactly 15 transactions — sampling cap**
- "last-15 sample" methodology caps at 15
- All wallets display exactly 15 tx/7d regardless of actual volume
- Design limitation should be documented in UI

**i-008 Live Gate displays mixed metrics**
- Paper traders use "closed trades >= 20 at >= 75% wr"
- Xora-survival uses "50 at >= 66.7%"
- Grid uses "cycles, profitable rate"
- Different metrics per row confusing at a glance

**i-015 Autotuner test failures**
- Search space arms: {0.015, 0.03, 0.02} — missing expected 0.08 (8%)
- Tighter_trail test returns NO_CHANGE instead of TUNED
- Override test returns 0.015 instead of expected 0.2

**i-016 Dashboard trailing policy test fails**
- expects MEME trail_arm_pct=0.08 (8%)
- API returns 0.02 (2%) — correct per code, stale test expectations

**i-018 Replay runner tests fail**
- Both replay_runner_regression and sampled_price_replay expect 1 closed trade but get 0
- The replay engine is not closing trades in unit tests

### Low Severity

**i-017 Live_inventory busy-wait 30s exceeds 15s bound**
- Bounded wait timeout test fails: 30.0 > 15.0 max

**i-019 Grid row in Live Gate uses different metrics**
- Cycles/profitable rate vs closed trades/win rate for other rows

**i-020 Market tab Monthly button disabled with no explanation**

**i-021 Live Gate header says 75% threshold but config has 66.67%**
- Dashboard HTML hardcodes '75%'; config.yaml has min_win_rate: 0.6667

**i-025 Live wallet minimal activity: only 2 buys in 11 days, no sells**

**i-027 Sidebar xora-survival summary says '0 paper' but incubator shows 214 trades/$2.73 net**

### Information

**i-022 xora-survival shows NZ$0 paper but API has paper_equity_usd=8.36**
- Dashboard may be showing only "live" paper positions (value 0) and ignoring paper equity

**i-024 AGENTS.md contains stale exit parameter defaults**
- MEME: code has 1.5% TP, -1.5% SL, 30min max-hold
- AGENTS.md: claims +20% TP, -10% SL, 900s max-hold
- SERIOUS: code has 1.0% TP, -1.0% SL, 30min max-hold
- AGENTS.md: claims +5% TP, -2.5% SL, six-hour max-hold
- README.md also contains old Windows paths and stale values

### Correct Behavior (verified)

**i-023 Paper incubator gate correctly blocks promotion**
- 214/50 closed at 49.5% wr (needs 66.7%)
- Net $-1.64 (needs positive net)
- Both API and dashboard agree

---

## Paper vs Live Provenance

The Xora-Survival tab correctly separates paper and live evidence:
- Live wallet widget shows "on-chain fills" with 2 RECONCILED trades
- Paper incubator widget shows "shadow (go-live gate)" with 214 closed
- Paper trade history has explicit MODE=PAPER column on every row
- Live trade history has RECONCILED state labels

However, the other trader tabs (Scalper, Reasoner, Whale, Memecoin, Grid) do NOT clearly label their activity as paper. They describe their wallets as "own wallet" but only the "PAPER SIMULATION" banner at the top of every page indicates this is paper trading. The scalper/reasoner tabs could be mistaken for live execution paths.

---

## Exit Policy Source per Trader

| Trader      | Source of exit params | Values displayed | Actual code values |
|-------------|----------------------|-----------------|-------------------|
| Scalper     | live_inventory.py MEME constants | 1.5/1.5/2.0/1.0/30m (via Xora-Survival widget) | MEME_TAKE_PROFIT_PCT=0.015 |
| Reasoner    | config.yaml reasoner section + DB override | 1.5/1.5/0.8/0.4/2h (via config) | Runtime override: TP=5%!!! |
| Whale       | mh_dash.py card text (static) | 5/2/6h (text only) | Not in inventory constants |
| Memecoin    | live_inventory.py MEME constants | 50/20/15m (text only) | MEME_TAKE_PROFIT_PCT=0.015 |
| Grid        | grid config | N/A (ladder levels) | grid config in config.yaml |
| Xora-Survival | live_inventory.py MEME+SERIOUS + autotuner | Displayed table | MEME TP=0.015, SERIOUS TP=0.01 |

---

## Baseline Test Results

Run: `python3 -m unittest discover -q` in /home/kelly/multihedge
Result: FAILED (failures=27, errors=11, skipped=4)
Exit code: 1

Total tests found: 336
Tests run: 332
Tests skipped: 4

**Modules with failures:**
- test_config_consistency: 1 FAIL (config 1.5% vs runtime 5% TAKE_PROFIT)
- test_dashboard: 1 FAIL (trail_arm 0.02 vs expected 0.08)
- test_db_lock_discipline: 1 FAIL (30s > 15s bound)
- test_dynamic_shadow_scalper: 14 FAIL (entry/exit logic broken)
- test_entry_filter: 2 FAIL (entry logic broken)
- test_live_inventory_defaults: 2 FAIL (expected 5% TP, code has 1.5%)
- test_parameter_autotuner: 3 FAIL (search space mismatch)
- test_replay_runner_regression: 1 FAIL (0 vs 1 trades)
- test_sampled_price_replay: 2 FAIL (0 vs 1 trades)
- test_survival_repair: 10 FAIL+ERROR (None DB queries)
- test_volatility_gate: 4 ERROR (signature mismatch)

---

## Findings Requiring No Immediate Action

- **Dashboard settings widget (⚙)**: Present on all tabs, allows layout customization
- **Light mode toggle**: Functional, switches between dark/light themes
- **Refresh dashboard button**: Functional, shows last-updated timestamp
- **Xora-Survival pet chat button**: Present on all tabs, labeled "Hi, I am Xora"
- **Report issue buttons (🐞)**: Present on every widget, functional
- **No JS errors detected**: console was clean on all tabs inspected
- **No secrets exposed**: No wallet keys, seed phrases, or API tokens in dashboard UI
- **No live trading risk**: All gates block promotion correctly (no trader eligible)

---

## Limitations

- Read-only audit: no production DB writes, no trades, no config/threshold/signer changes
- Runtime price-history files preserved as-is (not modified or cleaned)
- Dashboard inspection via browser_snapshot (accessibility tree) — visual verification limited without screenshots
- Some widget-issues reported by users may have been cleared from the database before this audit
- Model usage/cost data: OpenRouter HTTP402 error occurred in earlier attempts due to token reservation exceeding credits; current session is budget-qualified per deepseek/deepseek-v4-flash. No token cost data available from API.
- The "47 issues" figure: an original itemized list was not located in the repository. This audit's 27 findings may not cover the original scope. The user-provided count remains unconfirmed until the source list is found.