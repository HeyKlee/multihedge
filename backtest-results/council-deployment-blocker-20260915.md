# Council Record: XORA-SURVIVAL Telemetry Deployment Blocker

## Decision question
What must be addressed next so the verified, paper-only entry-observation and outcome-linkage work can be safely deployed without bundling unrelated defects?

## Evidence
- Targeted telemetry tests: 22 passed.
- Static checks: compileall and `git diff --check` passed.
- Full host suite: 323 tests, 4 failures and 2 errors.
- Every full-suite failure is in `test_dashboard_frontend.py`, on the pre-existing uncommitted `mh_dash.py` surface.
- Failure mechanisms are concrete:
  - `#petBubble` computes as `position: static`, not `fixed`.
  - `.xora-hist-row` intercepts clicks intended for `#editToggleBtn`.
  - catalog-widget content does not fill an explicitly resized card.
  - Xora summary labels differ in capitalization from the tested contract.
  - pet drag does not move its bounding box.
- Production DB has neither telemetry table. No workspace change is deployed.

## Manual council, used because the OpenRouter free council previously produced malformed output and timed out

### Advocate
Repairing the dashboard suite unblocks deployment of the isolated paper-learning instrumentation. Its tests define existing UI contracts, and the failures are reproducible. A scoped repair prevents a deployment that mixes known UI regressions with unrelated telemetry.

### Skeptic
Do not assume all six failures arose from the current dashboard diff. Establish a test for each observed contract and make the smallest CSS or DOM correction. Do not alter trading code while resolving browser-only defects.

### Oracle
Telemetry is not present in the active database, while the full suite currently fails only on dashboard cases. The verified evidence supports telemetry correctness in source but does not support a deployment claim. The deployment prerequisite is a clean suite or a separately justified quarantine of demonstrably pre-existing failures.

### Contrarian
The safer short-term option is to deploy only telemetry via a surgical image change. This is rejected: the project build deploys the whole workspace image, so it would still include the dashboard diff. Separating the dashboard repair is lower risk than selectively declaring a broken suite irrelevant.

### Arbiter
Approve an isolated, test-first repair of `mh_dash.py` and its existing dashboard tests only. After the full suite passes, rebuild and deploy the telemetry work as a paper-only change, then verify the two tables inside `/app/multihedge.db`, supervisor status, and affected APIs. Do not modify entry thresholds, exit rules, sizing, wallet, signer, configuration, promotion, or live authority.

## Approved scope
1. Fix the six reproducible dashboard frontend contracts only.
2. Re-run the full suite and static checks.
3. If and only if green, build/recreate `multihedge` without `--remove-orphans` and verify deployed telemetry tables.

## Rejected scope
- Any live-trading activation or evidence-gate relaxation.
- Any strategy, entry, exit, sizing, or wallet change.
- Deploying from a known failing full-suite state.
