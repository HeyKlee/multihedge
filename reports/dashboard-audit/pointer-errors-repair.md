# Pointer-Interception Error Repair

## Task t_200bf1e8

Date: 2026-09-23
Commit: (uncommitted — mh_dash.py + test_dashboard_frontend.py)
Hash: mh_dash.py patched, test_dashboard_frontend.py patched

---

## Background

widget-qa-v3.json (2026-09-23, commit 79d3b2bd57) reported 347 tests:
339 pass, 4 ERROR, 4 skip. The 4 errors were Playwright pointer-interception
issues across the test suite. Three of those four were resolved by prior
tasks (t_bc12159c widget overlap fix, t_13423c92 test config fix), leaving
1 active error at the start of this session.

---

## The Single Remaining Error

### Test
test_mobile_modals_catalog_and_chat_stay_within_viewport

### Before Fix (2026-09-23 15:47)
```
ERROR: line 219 — self.page.locator("#xoraPet").click()
TimeoutError: Locator.click: Timeout 5000ms exceeded.
Call log:
  - element is visible, enabled and stable
  - scrolling into view if needed
  - done scrolling
  - <button type="button" class="wallet-tab good"
    data-wallet-tab="survival"> from <div class="app-shell">...</div>
    subtree intercepts pointer events
```

### Root Cause Diagnosis
Investigated with Playwright inspector script (2026-09-23 15:49):

1. In mobile view (360x780), the test enters edit mode via
   `enter_edit_mode()`. This adds `free-edit` class to body, which causes
   ALL `.card,.kpi` elements to get `position:absolute` per CSS rule
   `.free-edit .card,.free-edit .kpi{position:absolute}`.

2. The wallet-hub (a `.kpi`) contains the `.wallet-tabs` grid, which
   includes 6 wallet-tab buttons including a "survival" tab.

3. The function `applyWidgetStyle()` (mh_dash.py:2640) sets:
   ```
   w.style.zIndex = (st.z != null ? st.z : (st.y != null ? Math.max(1, 10000 - Math.round(st.y)) : 1));
   ```
   For the wallet-hub at y=0, this computes `z-index = 10000 - 0 = 10000`.

4. The xoraPet CSS had `z-index: 91`.

5. Bounding boxes:
   - xoraPet: x=262, y=673, w=70, h=76 (right edge: 332, bottom: 749)
   - survival wallet-tab: x=301, y=665, w=120, h=131 (right edge: 421, bottom: 797)
   - Overlap region: x=301-332, y=665-749

6. `elementFromPoint(x=297, y=712)` returned `DIV#wallet-tabs` —
   the wallet-tabs div was topmost because the wallet-hub had
   `z-index: 10000` (inline style) while xoraPet had `z-index: 91` (CSS).

Result: Clicking the pet at viewport coordinates (297, 712) hit the
wallet-tabs div instead. Playwright detected the pointer interception
and refused the click.

### Verification Data (before fix)
```
pet bounding box:      {x:262, y:673, w:70, h:76}
survival tab box:      {x:301, y:665, w:120, h:131}
elementFromPoint(297,712):  DIV#wallet-tabs
pet computed z-index:       91
pet computed position:      fixed
survival computed z-index:  auto
wallet-hub inline z-index:  10000
wallet-hub position:        absolute
body has free-edit:         true
```

---

## Fix Applied

### Fix 1: xoraPet z-index (mh_dash.py line 1121)

Changed `.xora-pet` CSS from:
```
z-index: 91
```
to:
```
z-index: 10001
```

Rationale: The widget-style JS (applyWidgetStyle at line 2640) assigns
`z-index = 10000 - y` to cards in edit mode, capping at 10000. The pet
needs to be above the maximum possible widget z-index. Value 10001 is
1 above the max and so guarantees clickability without interfering with
other stacking.

Important: `!important` is NOT needed because the z-index comparison is
cross-element in the root stacking context — computed z-index 10001 > 10000
regardless of style origin. The pet is `position: fixed` which creates its
own stacking context within the root context, and the widget's inline
`z-index: 10000` is in the root context as well.

### Fix 2: dblclick in test (test_dashboard_frontend.py line 219)

Changed the test from `.click()` to `.dblclick()`:

From:
```
self.page.locator("#xoraPet").click()
```
To:
```
self.page.locator("#xoraPet").dblclick(timeout=5000)
```

Rationale: The xoraPet only registers a `dblclick` event handler for
opening/closing the chat panel (mh_dash.py:2958). There is no `click`
handler. The previous `.click()` would never open the chat, but the
pointer-interception error prevented the test from reaching the assertion
on line 222. The bug was latent — the test was never able to exercise
lines 220-223 because line 219 always errored first. Now that clicking
the pet succeeds, the test must correctly double-click to open the chat
panel.

---

## After Fix Verification

### Test passes
```
test_mobile_modals_catalog_and_chat_stay_within_viewport ... ok
```

### elementFromPoint confirmation
```
elementFromPoint at pet center (297,712): BUTTON#xoraPet
(no longer intercepted by wallet-tabs div)
```

### Full suite
```
Ran 347 tests in ~55s
OK (skipped=4)
```

### Static checks
```
compileall:  PASS (exit 0)
git diff --check:  PASS (exit 0, no whitespace errors)
```

---

## Files Changed

1. `/home/kelly/multihedge/mh_dash.py` — line 1121: xoraPet z-index 91 -> 10001
2. `/home/kelly/multihedge/test_dashboard_frontend.py` — line 219: .click() -> .dblclick()

---

## No Unrelated Changes

Preserved: Kelly saved layout, production DB, trading thresholds, config.yaml,
signer isolation, authority. No CSV or policy edits. No deploy.

---

## Status

All 4 pointer-interception errors from widget-qa-v3 are now resolved.
3 were resolved by prior tasks (t_bc12159c, t_13423c92).
1 resolved by this session (t_200bf1e8).

Ready for QA child task release.