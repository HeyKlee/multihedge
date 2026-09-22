# Core Fixes Report — t_6633da90

Agent: thecryptobot (deepseek/deepseek-v4-flash via openrouter)
Date: 2026-09-22 20:08 NZST
Workspace: /home/kelly/multihedge

---

## Fix 1: live_inventory BUSY_TIMEOUT_SECONDS

**File:** live_inventory.py:203
**Change:** BUSY_TIMEOUT_SECONDS = 30.0 -> 15.0
**Rationale:** The 2026-09-12 container wedge post-mortem established a 15-second bounded-wait policy. The value had been increased to 30.0 at some point, exceeding the policy bound. mh_reasoner correctly used 10.0. Aligned to 15.0 to match the established policy while preserving a margin over typical live operations.

**Verification:** test_busy_wait_is_bounded_in_both_connection_helpers passes (asserts 15.0 <= 15.0).

---

## Fix 2: mh_reasoner DEFAULT_PARAMS alignment with config.yaml

**File:** mh_reasoner.py:31-38
**Changes:**

| Key | Before (code) | After (code) | config.yaml |
|-----|--------------|-------------|-------------|
| TAKE_PROFIT | 0.025 | 0.015 | 0.015 |
| TRAIL_ARM | 0.01 | 0.008 | 0.008 |
| TRAIL_DIST | 0.005 | 0.004 | 0.004 |
| CONFIDENCE_MIN | 0.55 | 0.35 | 0.35 |
| POSITION_FRACTION | 0.50 | 0.50 (unchanged) | 0.5 |
| STOP_LOSS | 0.015 | 0.015 (unchanged) | 0.015 |
| MAX_HOLD_SECS | 7200 | 7200 (unchanged) | 7200 |

**Behavioral impact:** This is a no-op at runtime. The load order is DB > config.yaml > code defaults. config.yaml defines every key in its `reasoner:` block, so it always overrides the code defaults. The applied DB override (TAKE_PROFIT=0.05 set by optimizer) continues to win at runtime. No running behavior changed.

**New tests added to test_config_consistency.py:**
- test_code_defaults_agree_with_config_block_values — asserts every shared key matches config.yaml
- test_all_reasoner_keys_have_config_block_values — asserts config.yaml defines all reasoner keys

---

## Fix 3: test_config_consistency read-only SQLite with bounded timeout

**File:** test_config_consistency.py:67-76
**Change:** Wrapped the `test_declared_values_match_what_the_reasoner_actually_applied` production DB query in try/except for `sqlite3.OperationalError`. Explicitly passes `timeout=5` to connect(). On "database is locked" or "busy", skips with a descriptive reason marker containing the actual error text. On any other OperationalError, re-raises.

**Behavior:** When the container holds the WAL lock (common during concurrent test runs), the test skips gracefully instead of failing with an unhandled OperationalError. When the DB is available, the test proceeds normally and may report config-vs-applied drift (expected — the optimizer sets its own values).

---

## Remaining Failures (out of scope)

| Test | Failure | Owner |
|------|---------|-------|
| test_tighter_trail_can_be_adopted | NOT_IMPROVED != TUNED (stale fixture after MEME default realignment) | sibling t_ |
| test_collection_tuning_and_paper_application_work_end_to_end | same stale fixture root cause | sibling t_ |
| test_legacy_path_does_not_permanently_poison_new_policy_cohort | same stale fixture root cause | sibling t_ |
| test_declared_values_match_what_the_reasoner_actually_applied | config 0.015 vs DB-applied 0.05 (DB override, out of scope) | pre-existing drift |

---

## Static Checks

- compileall: PASS (0 errors)
- git diff --check: PASS (no whitespace errors)

## Test Summary

338 tests ran (2 new), 4 failures (all out-of-scope), 4 skips.