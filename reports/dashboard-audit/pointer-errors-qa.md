# Pointer-Interception Error QA — Independent Verification

## Task t_49287be3

Date: 2026-09-23
Agent: iraia (deepseek/deepseek-v4-flash via openrouter)
Parent: t_200bf1e8

---

## Verdict: CONDITIONAL PASS — fix correct, test flaky, deploy required

The z-index fix (91 -> 10001) resolves the pointer-interception error at the
CSS stacking level. Source code verified correct.

**But** the test suite is flaky: 2/3 runs pass clean, 1/3 fails with
"element is not stable" on dblclick. This is a pre-existing CSS animation
issue (petBob keyframes), not a defect in the fix.

---

## 1. Source Code Verification

### Fix 1: z-index (mh_dash.py line 1121)
- Expected: `.xora-pet{...z-index:10001...}`
- Found:   `z-index:10001` — VERIFIED CORRECT

### Fix 2: dblclick (test_dashboard_frontend.py line 219)
- Expected: `.dblclick(timeout=5000)`
- Found:   `.dblclick(timeout=5000)` — VERIFIED CORRECT
- Old (before fix): `.click()` — correctly replaced

### Source hashes
- mh_dash.py:                `31cd50f742a5f59e79002814516cdf858c066c0db9dbe581b1b5759d49f64fbf`
- test_dashboard_frontend.py:`06609bc650b5e068fc73c52acf9543abad73e1eb4e2ee1fd6a3e65685b8cfdb6`

---

## 2. Full Test Suite Results (3 consecutive runs)

| Run | Result    | Pass | Fail | Error | Skip | Duration |
|-----|-----------|------|------|-------|------|----------|
| 1   | OK        | 343  | 0    | 0     | 4    | 50.5s    |
| 2   | FAILED    | 342  | 0    | 1     | 4    | 52.8s    |
| 3   | OK        | 343  | 0    | 0     | 4    | 49.2s    |

**Flaky rate: 33% (1/3 runs fail)**

### Single failing test
`test_mobile_modals_catalog_and_chat_stay_within_viewport`
- Error: `Locator.dblclick: Timeout 3000ms exceeded`
- Detail: "element is not stable" — Playwright stability check catches
  the CSS animation mid-cycle
- NOT the old pointer-interception error (which is resolved)

### Skipped tests (4 — all documented)
1. `test_chain_config` — Solana deps unavailable (no `solders` module)
2. `test_pet_speech_updates_when_hovering_widget` — no card elements in mock view
3. `test_solana_signer_backend` — Solana deps unavailable (no `solders` module)
4. `test_widget_parsing` — diagnostic script, not automated test

---

## 3. Flakiness Root Cause

The xoraPet element has a CSS animation:
```css
.xora-pet-inner {
  animation: petBob 3.2s ease-in-out infinite;
}
@keyframes petBob {
  0%, 100% { transform: translateY(0); }
  50%      { transform: translateY(-3px); }
}
```

Playwright's built-in actionability check waits for the element to be
"stable" (not moving). The perpetual animation causes the stability check
to time out intermittently (1/3 runs).

The test already disables JS-based idle roam (line 218) but does NOT pause
the CSS animation. This was latent before the fix because the previous
pointer-interception error always blocked line 219 before the animation
timing could matter.

---

## 4. Static Checks

| Check                   | Result  | Detail              |
|-------------------------|---------|---------------------|
| python3 -m compileall   | PASS    | exit 0              |
| git diff --check        | PASS    | exit 0, no whitespace errors |

---

## 5. Browser Verification (Production — port 9052)

### Container state
- Container mh_dash hash: `62b4880a0c085c8589384e76e3e2d43e2e27ad67027a46d83ccd94575d7c5539`
- Source mh_dash hash:    `31cd50f742a5f59e79002814516cdf858c066c0db9dbe581b1b5759d49f64fbf`
- **Source differs from deployed** — fix is NOT in production

### Deployed z-index (still the old value)
- xoraPet z-index: 91 (not 10001)
- elementFromPoint at pet center returns BUTTON#xoraPet (no interception in
  current desktop viewport — pet is at x=1151, wallet survival tab is elsewhere)

### Widget issues 1,2,3 verification (deployed)
| Issue | Description | Status |
|-------|-------------|--------|
| 1     | last-cycles-heatmap obscured by Live Market Tick | FIXED (deployed) |
| 2     | live-market-tick covers other widgets | FIXED (deployed) |
| 3     | 120s refresh overlays during editing | FIXED (deployed) |

No absolute-positioned cards found in view mode. CSS Grid layout correct.

### API endpoints
- All 9 supervisor processes RUNNING
- /api/survival: returns valid JSON with live+paper positions and trades
- /api/livegate: returns valid JSON with trader stats
- Dashboard renders correctly on desktop viewport

---

## 6. Actionable Findings

### A. Deploy required
The z-index fix (10001) is in source only. Container still has z-index:91.
Deploy `mh_dash.py` to production after this QA cycle.

### B. Flaky test
`test_mobile_modals_catalog_and_chat_stay_within_viewport` fails 1/3 runs
due to petBob CSS animation. Suggested fix: pause element animation before
dblclick in the test:
```
self.page.evaluate('''() => {
  const inner = document.querySelector('.xora-pet-inner');
  if (inner) inner.style.animationPlayState = 'paused';
}''')
```
This does not count as "forced click" — it only pauses the visual animation,
the test still uses real Playwright click with actionability checks.

### C. No source edits needed for the z-index fix itself
The actual fix (91 -> 10001) is correct. The root cause analysis from the
parent is accurate: `applyWidgetStyle()` sets inline z-index=10000 on cards
in edit mode, and the pet needs z-index > 10000 to avoid interception.

---

## 7. Safety Invariants

- Production DB: UNTOUCHED (read-only inspection)
- Kelly layout: PRESERVED (no changes)
- config.yaml: UNTOUCHED
- Trading policy: UNTOUCHED
- Signer isolation: UNTOUCHED
- No --remove-orphans
- autohedge container untouched