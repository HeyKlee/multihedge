# PROJECT ATLAS — SEAT 4: REFACTOR PLAN

**Seat:** 4 — Code Cartographer / Refactor Engineer
**Companion to:** `ops/atlas/SEAT4_DUPLICATION_MAP.md`
**Baseline:** git `35f922a`
**Status:** PLAN ONLY. Nothing in this document has been applied. No module,
config, database or test was modified. No deploy or trading cycle was run.

> **Read `SEAT4_ADDENDUM_A.md` first.** It supersedes the Step 1 acceptance
> criteria below. The original "full suite green" criterion is wrong: the three
> suites covering these four engines are already red at HEAD, pre-dating this
> seat. Step 1 must use a failure-delta criterion, not a green-suite one, and
> the four pre-existing failures must be triaged before Step 1 starts.

Every step below obeys the `AGENTS.md` change workflow: behavioural regression
test first, smallest complete change, focused tests, full host suite, static
checks, deploy, verify the deployed artifact by content, read back external
state, commit only intended files.

## Ordering principle

Refactor in dependency order, not severity order. Each step must be able to land
and be verified on its own. Step 1 is first because it is the only step that is
simultaneously a duplication fix and a safety-constitution fix.

Steps 1 and 2 are the two I would not defer. Steps 3 to 5 are real debt but do
not currently produce a wrong answer on a live path.

---

## Step 1 — One exit engine (fixes D1, Critical)

**Problem:** four engines decide exits, and `paper.py:434` and
`mh_memecoin_trader.py:149` close on age alone, violating `AGENTS.md:70-72`.
The trail arm is latched at `paper.py:437-439` but re-tested every cycle at
`mh_memecoin_trader.py:152`.

**Target:** `domain/risk/exit_policy.py` owns:

```
evaluate(position, price, now, params) -> reason | None
arm_state(previous_state, position, price) -> new_state
```

Semantics are fixed by the constitution, not by majority vote of the existing
engines. The conformant pair (`dynamic_shadow_scalper.py:108`,
`live_inventory.py:375`) is the reference implementation:

- take profit: `price/entry - 1 >= take_profit_pct`
- stop loss: `price/entry - 1 <= stop_loss_pct`, unconditional
- max hold: timer AND `peak/entry - 1 >= take_profit_pct`
- trail: `peak/entry - 1 >= trail_arm_pct` AND `price/peak - 1 <= -trail_distance_pct`

The arm latch becomes explicit state owned by the engine, replacing both the
in-memory latch and the per-cycle re-test.

**Regression test first**, asserting the invariant rather than source text
(per `AGENTS.md` testing rules):

1. A position whose age exceeds `max_hold_seconds` but whose peak never crossed
   TP returns `None`. This currently FAILS for `paper.close_checks` and
   `mh_memecoin_trader._eval_exit`, and passes for the other two. The test pins
   the constitution, so it is allowed to fail before the change.
2. A position that armed, retraced below the arm, then re-extended produces the
   same verdict through every caller.
3. Stop loss fires regardless of age, peak, or arm state.

**Then:** migrate `paper.py:419`, `mh_memecoin_trader.py:143`,
`dynamic_shadow_scalper.py:100`, `live_inventory.py:335` to the shared engine,
deleting the four local decision functions. Keep the per-module constants for
now; Step 1 changes decision logic, not threshold ownership. Consolidating both
at once would make a failure ambiguous.

**Acceptance:** `ops/atlas/seat4_gate.py --quiet` reports 0 for
`E1_single_exit_engine` and `E2_max_hold_age_alone`. Full suite green. Live exit
behaviour for `forced_exit` unchanged, verified by comparing
`live_inventory.forced_exit` output before and after on recorded price paths.

**Deploy note:** this changes live exit behaviour on a funded mainnet path.
`AGENTS.md` requires Kelly's explicit approval for that exact action. Do not
bundle it with an unrelated change.

---

## Step 2 — One promotion gate (fixes D2, Critical)

**Problem:** seven gates, and the dashboard contradicts the engine.
`paper.py:384` reads `min_win_rate` from config (currently 0.6667);
`mh_dash.py:543-544` and `dash_web.py:73` hardcode `n >= 20 and wr >= 0.75`.
`config.yaml` also sets `live.min_closed_trades: 20`, so only the win rate
disagrees. Gate 4 (`continuous_optimizer.py:175`) is win-rate-free by design and
must not be forced into the same shape; it is a research-promotion gate, not a
live-readiness gate. Gate 5 (`grid_trader.py:389`) reads `grid_trades` with
grid-specific thresholds, which its own docstring at `grid_trader.py:390-400`
justifies explicitly. Keep both, and declare their scope in code so the next
reader does not merge them by accident.

**Target:** one `promotion_gate(trader, cfg, db_path)` returning the verdict and
its inputs. `paper.paper_gate_status` becomes the live-readiness implementation.
`mh_dash.api_livegate` and `dash_web._read_db` consume the returned dict and
render it. They may format; they may not recompute.

**Regression test first:**

1. A trader with n=20 and wr=0.70 is `eligible` under the engine and the
   dashboard reports the identical value. This currently FAILS, because the
   dashboard applies 0.75.
2. The gate returns the threshold values it used, so the dashboard can display
   them instead of holding its own.
3. Grouping is by `setup` per `paper.py:392-399`, not by `coin` as at
   `dash_web.py:66`. A regression test must pin which key identifies a trade's
   owner, because the two currently differ.

**Then:** delete the hardcoded literals at `mh_dash.py:543-544` and
`dash_web.py:73`. Extend the existing `G_dashboard_policy_literal` regex in
`tools/check_single_truth.py` to cover eligibility thresholds, not just the five
`*_pct`/`*_seconds` policy keys. Note this edits an existing tool, which is
outside this seat's write scope; it is listed here for the chair to schedule.

**Acceptance:** `ops/atlas/seat4_gate.py --quiet` reports 0 for
`E3_dashboard_gate_literal`. `GET /api/livegate` and
`paper.paper_gate_status` agree for every trader in `paper.TRADERS`.

---

## Step 3 — One position sizer (fixes D3, High)

**Problem:** `mh_reasoner.py:32` 0.50, `mh_whale_trader.py:41` 0.40,
`mh_memecoin_trader.py:28` 0.20, `dynamic_shadow_scalper.py:46` 0.15.
`multihedge.py:55` defaults to 0.20 while `multihedge.py:141` reads the reasoner
value, so one process holds two fractions. `survival_policy.py:17-18` adds
`MAX_TOTAL_POSITION_FRACTION = Decimal("0.05")` and
`MAX_RISK_POSITION_FRACTION = Decimal("0.20")` on a constitutional footing.

**BLOCKER — resolve before starting.** I could not verify whether "free
balance" and "treasury" share a denominator. If the caps at
`survival_policy.py:17-18` are intended to bind position sizing, then the
reasoner's 0.50 is a live exposure question, not merely a duplication. If they
are unrelated measures, this step is pure tidiness. Settle this with Kelly before
refactoring, or the consolidation may silently tighten or loosen live exposure.

**Target:** `domain/risk/sizing.py` resolves a fraction from an explicit intent
(scalp, day-trade, risk-capped) plus config plus the survival caps, returning the
fraction and the reason it was clamped. Callers pass intent, not a number.

Use `Decimal` per `AGENTS.md` where constitutional NZD amounts are involved.

**Regression test first:** each current fraction is reproduced exactly for its
own intent, so the consolidation is provably behaviour-preserving. Then a test
that the survival cap is applied when the requested fraction exceeds it.

**Acceptance:** `E4_position_sizing_literal` reaches 0. Every historical trade
reproduces under the new sizer at unchanged parameters.

---

## Step 4 — One model transport (fixes D4, Medium)

**Problem:** four transports at `agent_architecture.py:68-78`,
`autonomous_live.py:284`, `mh_ui.py:490-503`, `mh_news.py:144-164`. Three JSON
salvage parsers at `agent_architecture.py:81-93`, `autonomous_live.py:170-180`,
`mh_ui.py:623-633`. `autonomous_live.py:178` catches only `ValueError` while the
other two catch `(ValueError, TypeError)`, so a `TypeError` is fail-closed in one
path and raises in another.

**Target:** one transport owning auth, timeouts, `X-Title`, status handling and
model-pin verification. **The pin check at `autonomous_live.py:268-270` must
become the default for all callers**, not remain one caller's extra care. That is
the substantive safety gain; the rest is consolidation.

One `recover_json_object(content)` raising one error type, catching
`(ValueError, TypeError)`.

**Regression test first:** a `TypeError`-raising salvage input is denied, not
propagated, through every caller.

**Do not** change `mh_news.py`'s provider preference. Its local-model-first
ordering at `mh_news.py:170-177` is deliberate, and the transport layer must
accept a base URL rather than assume OpenRouter.

**Acceptance:** one transport, one salvage parser, zero direct
`chat/completions` construction outside it.

---

## Step 5 — Delete the duplicated universe (fixes D5, Medium-High)

**Problem:** `track_whales.py:47-51` hardcodes three mints directly beneath a
comment at `track_whales.py:46` asserting `config.yaml` is the single source of
truth. The mints currently match config exactly, verified by reading
`config.yaml`. The silent failure is `track_whales.py:100`:
`symbol = COIN_MINTS.get(mint, mint)` returns a 44-character raw mint as a
symbol with no error. `track_whales` is supervised
(`deploy/multihedge.supervisord.conf:97-98`).

The whale wallet list is duplicated at `track_whales.py:38-44` and
`mh_whale_trader.py:32-38`. I verified the two address sets are byte-identical
across all 5 addresses, with no difference in either direction. The tracker copy
carries `handle` and `rank`; the trader copy discards both, so a wallet added to
the tracker is not tradeable by the trader.

**Target:** `track_whales` imports `COINS` from `config.py`, which already
exports it (`config.py:19-29`) and is already the owner used by
`mh_reasoner.py:25`, `mh_whale_trader.py:22` and `pricefeed.py:308`. Delete
`COIN_MINTS`. Make an unmapped mint a loud, logged skip, never a raw-mint symbol.

`mh_whale_trader` reads the wallet list from the tracker's table
(`mh_whale_wallets`, `track_whales.py:80-83`) rather than holding a copy.

**Regression test first:** adding a coin to the config universe makes
`track_whales` resolve its symbol, and an unknown mint produces a skip with a
reason rather than a raw-mint row.

**Acceptance:** `E5_hardcoded_universe` reaches 0. No mint-to-symbol map exists
outside `config.py`.

---

## Cross-cutting: schema ownership

Not a duplication finding, but it blocks clean verification of Step 1.

Three modules declare `mh_trades` independently: `paper.py:98` (includes
`cost_usd`), `dynamic_shadow_scalper.py:67` (omits it), and `mh_reasoner.py:143`
(omits it, and every column nullable). Production has 14 columns including
`cost_usd`, confirmed read-only from the container, so `paper.py`'s declaration
won the race. I did not test what happens on a fresh database where
`mh_reasoner.py` connects first.

Give the schema one owner and a migration registry, so the table definition is
stated once. This is a prerequisite for trusting any test that asserts on trade
columns, and it belongs with Step 1 because exit verification writes to
`mh_trades`.

---

## What this plan does not resolve

- **Exit-evaluation cadence.** Unchanged from FINDING 007. There is still no
  instrumented exit-check timestamp, so whether the system could observe a
  20% move inside 30 to 60 minutes remains unanswerable from data. Step 1 makes
  one engine; it does not make it fast. Instrument first if the 20% target is
  still intended.
- **The 20% take-profit decision.** Recorded in FINDING 007 as decided by Kelly
  on 2026-09-28 and not yet applied. This plan does not change thresholds.
- **The `74` versus `76` violation count.** The index at
  `docs/atlas/00_MASTER_INDEX.md:33` is stale against HEAD `35f922a`; the real
  count is 76. Also worth separating explicitly: the scanner's 133 DB-path sites
  and the gate's 27 are different regexes over the same tree, not a
  contradiction.
