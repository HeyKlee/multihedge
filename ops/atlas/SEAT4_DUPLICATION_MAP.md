# PROJECT ATLAS — SEAT 4: DUPLICATION MAP (beyond FINDINGS 001-007)

**Seat:** 4 — Code Cartographer / Refactor Engineer
**Question answered:** "Where is behaviour implemented more than once?"
**Baseline:** git `35f922a` (ATLAS_SCAN.json baseline was `cd70d62` — see §6)
**Detector:** `ops/atlas/seat4_detector.py` (read-only, regenerable, this directory only)
**Scope note:** findings 001-007 own DB paths, config readers, risk thresholds, cost/PnL
and image drift. This map deliberately excludes those domains and reports only
duplication they do **not** already cover.

## 0. How to reproduce

```
python3 ops/atlas/seat4_detector.py          # human report
python3 ops/atlas/seat4_detector.py --json   # machine readable
python3 tools/check_single_truth.py --quiet  # the existing gate (76 violations)
```

The detector parses source text only. It opens no database and writes nothing
outside `ops/atlas/`.

---

## 1. D1 — FOUR exit engines, and they do not agree on what an exit *is*

**This is the most serious duplication in the repo, and it is a semantic split,
not a constant split.** Four modules independently decide take-profit,
stop-loss, max-hold and trailing-stop. Two of them will close a position on age
alone. Two will not. The project constitution says age alone never closes a
position, so the paper path currently violates `AGENTS.md`.

| # | Site | Driver | Max-hold requires TP-crossed peak? | Trail uses |
|---|---|---|---|---|
| 1 | `paper.py:419` `close_checks` | module constants | **NO** — `paper.py:434` | `(peak-px)/peak` |
| 2 | `mh_memecoin_trader.py:143` `_eval_exit` | module constants | **NO** — `mh_memecoin_trader.py:149` | `(peak-px)/peak` |
| 3 | `dynamic_shadow_scalper.py:100` `_exit_reason` | params dict | **YES** — `dynamic_shadow_scalper.py:108-110` | `price/peak - 1` |
| 4 | `live_inventory.py:335` `forced_exit` | params dict | **YES** — `live_inventory.py:375-377` | `price/peak - 1` |

Evidence, verbatim:

```
paper.py:434            if time.time() - pos["open_ts"] >= MAX_HOLD_S:
paper.py:435                return "max_hold"

mh_memecoin_trader.py:149   if time.time() - pos["ts"] >= MAX_HOLD_S:
mh_memecoin_trader.py:150       return "max_hold"

dynamic_shadow_scalper.py:108  if (now - float(position["opened_ts"]) >= params["max_hold_seconds"]
dynamic_shadow_scalper.py:109          and peak / entry - 1 >= params["take_profit_pct"]):
dynamic_shadow_scalper.py:110      return "max_hold"

live_inventory.py:375      elif (now - float(position["opened_ts"]) >= params["max_hold_seconds"]
live_inventory.py:376            and peak / entry - 1 >= params["take_profit_pct"]):
```

`AGENTS.md:70-72` states the governing rule:

> "Max hold is a missed-TP execution fallback only. It may close after the timer
> only if the recorded peak already crossed TP but the TP sale did not complete.
> Age alone never closes a position."

So engines 1 and 2 are non-conformant with the constitution, and engine 1 is the
shared paper ledger used by the reasoner, the scalper and the dashboard's stats.

**Second divergence: the trailing arm is re-derived from live price in 2 and 4,
but latched in 1.**

- `paper.py:437-439` reads `pos.get("trail_armed")` and latches it in memory.
- `paper.py:441` / `mh_memecoin_trader.py:152` test `(peak - px) / peak`.
- `live_inventory.py:378-379` tests `price / peak - 1 <= -trail_distance_pct`.

Algebraically `(peak-px)/peak >= d` and `px/peak - 1 <= -d` are the same
inequality, so those four are equivalent. The real split is the **arm**:
`paper.py:437-439` latches, `mh_memecoin_trader.py:152` re-tests
`(peak/entry - 1) >= TRAIL_ARM_PCT` every cycle with no latch. A memecoin
position that arms, retraces below the arm, and re-extends will behave
differently between the two engines.

**Third divergence: the exit reason vocabulary is not shared.** `paper.py` returns
`"take_profit" | "stop_loss" | "max_hold" | "trail_stop"`. `mh_memecoin_trader.py:145-153`
returns the same four strings. But `mh_dash.py:231` buckets `exit_reason` for
display with a free-text `reasons` dict, so a reason string invented by any writer
lands in the UI as a new bucket rather than failing loudly.

**Severity: Critical.** Two engines violate a stated safety invariant, and the
non-conformant pair is the one that writes the paper ledger the dashboard reports.

> **Coverage note (`SEAT4_ADDENDUM_A.md`):** the age-alone invariant is pinned by
> a test for the two *conformant* engines (`test_live_inventory.py:62-75`,
> `test_dynamic_shadow_scalper.py:72-90`) and has no coverage for the two
> *non-conformant* ones. That asymmetry is the mechanism, not a coincidence. It
> also means the test-first step for the exit consolidation already exists as a
> reference shape, so the fix is smaller than it first appears.

---

## 2. D2 — SIX live-promotion gates, four different answers to "is this ready?"

Six independent implementations decide promotion eligibility. They do not agree on
thresholds, on which trades count, or even on the name of the minimum.

| # | Site | Gate function | Min n | Min win rate | Reads trades from |
|---|---|---|---|---|---|
| 1 | `paper.py:379` | `paper_gate_status` | 20 (`paper.py:385`) | **0.75** (`paper.py:384`) | `mh_trades`, setup-filtered |
| 2 | `mh_dash.py:526` | `api_livegate` | 20 (`mh_dash.py:543`) | **0.75** (`mh_dash.py:543`) | same filter, recomputed |
| 2b | `dash_web.py:73` | `_read_db` (inline) | 20 (`dash_web.py:73`) | **0.75** (`dash_web.py:73`) | by `coin`, not by setup |
| 3 | `autonomous_live.py:508` | `strategy_evidence` | 50 aggregate / 20 symbol (`autonomous_live.py:525,536`) | 0.6667 (`autonomous_live.py:526,529,537`) | `mh_trades`, excludes the 3 trader setups |
| 4 | `continuous_optimizer.py:175` | `promotion_gate` | 20 / 50 (`continuous_optimizer.py:177,179`) | not win-rate based | expectancy + drawdown |
| 5 | `grid_trader.py:389` | `grid_gate_status` | 30 cycles (`grid_trader.py:407`) | 0.55 profitable rate (`grid_trader.py:423`) | `grid_trades` |
| 6 | `mh_coin_review.py:320` | replay authorization | 5 paths (`mh_coin_review.py:35`) | **0.40** (`mh_coin_review.py:37`) | excursions |

The headline defect: **the dashboard hardcodes 20 / 0.75 at `mh_dash.py:543` while
the module it reports on reads them from config.**

```
mh_dash.py:543   "eligible": n >= 20 and wr >= 0.75,
mh_dash.py:544   "reason": ("READY" if (n >= 20 and wr >= 0.75)

paper.py:384     min_wr = live.get("min_win_rate", 0.75)
paper.py:385     min_n = live.get("min_closed_trades", 20)
```

`config.yaml` currently sets `live.min_win_rate: 0.6667` and
`live.min_closed_trades: 20`. So `paper.paper_gate_status` uses **0.6667** and
`mh_dash.api_livegate` uses **0.75** for the same trader on the same data. A trader
at 70% win rate with 20 trades is **eligible** in `/api/livegate`'s backend call
path and **not eligible** in the endpoint the operator actually reads. This is a
live disagreement, not a latent one. The `0.75` in `mh_dash.py` is also a
violation of the dashboard-must-not-define-policy rule (FINDING 002), but the
existing gate's `G_dashboard_policy_literal` rule does not catch it because its
regex only matches the five `*_pct`/`*_seconds` policy keys, not `n`/`win_rate`.

Measured today, read-only from the running container:

```
mh_accounts: dynamic_scalper 7.8094/10.0  memecoin_trader 24.0/24.0
             reasoner 26.7343/24.0  scalper 19.6759/24.0  whale_trader 24.0928/24.0
mh_trades by setup: reasoner 797, rsi_oversold 461, dynamic_scalper 432,
                    momentum_breakout 381, mean_reversion 112,
                    vwap_reversion 29, whale_trader 7
```

`whale_trader` has 7 closed trades. Gate 1 requires 20, gate 3's symbol-level
minimum is 20, gate 6 needs 5 paths at 0.40 win rate. Three different verdicts
from three different rules on the same seven trades.

`dash_web.py` is a **seventh** copy and is the only one I initially missed; my
first detector pass did not flag it because it does not use the word "win_rate"
in the same construct. It is **not supervised** (`grep -c dash_web
deploy/multihedge.supervisord.conf` returns 0), so it is a latent fourth opinion
rather than a live one. It also groups by `coin` (`dash_web.py:66`) where
`paper.py:392-399` groups by `setup`, so even with the same thresholds it would
not produce the same numbers.

**Severity: Critical.** The dashboard's eligibility verdict contradicts the
engine's, and the dashboard is what a human reads before enabling live.

---

## 3. D3 — Position sizing decided independently in five places

Five modules each pick their own fraction of free balance. No shared owner.

| Site | Value | Basis |
|---|---|---|
| `mh_reasoner.py:32` (`DEFAULT_PARAMS`) | 0.50 | `POSITION_FRACTION` |
| `mh_whale_trader.py:41` (`DEFAULT_PARAMS`) | 0.40 | `POSITION_FRACTION` |
| `mh_memecoin_trader.py:28` | 0.20 | module constant |
| `dynamic_shadow_scalper.py:46` | 0.15 | module constant |
| `ops/5year_backtest.py` | 0.3 | backtest-only, arguably legitimate |
| `multihedge.py:141` | reads `cfg.reasoner.POSITION_FRACTION` | mirrors reasoner |
| `multihedge.py:55` | `p.get("position_fraction", 0.20)` | **different default** |

`multihedge.py` is the sharpest case: line 55 defaults to `0.20` and line 141
reads the reasoner's `POSITION_FRACTION`, so the same process has two position
fractions depending on which code path sizes a trade.

`survival_policy.py:17-18` adds two more, on a constitutional footing:

```
survival_policy.py:17   MAX_TOTAL_POSITION_FRACTION = Decimal("0.05")
survival_policy.py:18   MAX_RISK_POSITION_FRACTION = Decimal("0.20")
```

So the reasoner sizes at 50% of free balance while a survival policy in the same
tree caps a risk position at 20% of treasury. Whether that is a genuine conflict
depends on whether "free balance" and "treasury" are the same base — **I could not
verify that** and am flagging it rather than asserting it.

**Severity: High.** No single answer to "what fraction of the wallet does one trade
get", which is the first question a risk review asks.

---

## 4. D4 — Four chat transports and three JSON-salvage parsers

**Transports** (each builds its own request, own headers, own status handling):

| # | Site | Endpoint | Model source | Timeout |
|---|---|---|---|---|
| 1 | `agent_architecture.py:68-78` | `https://openrouter.ai/api/v1` (`:17`) | `OPENROUTER_MODEL` const (`:18`) | 30s |
| 2 | `autonomous_live.py:284` | `https://openrouter.ai/api/v1/chat/completions` (`:28`) | config, **pinned-checked** (`:268-270`) | — |
| 3 | `mh_ui.py:490-503` | `https://openrouter.ai/api/v1` | `OPENROUTER_MODEL` const | 45s |
| 4 | `mh_news.py:144-164` | `urllib`, not httpx; OpenRouter is a *fallback* (`:174-177`) | `MH_NEWS_MODEL` env default (`:150`) | `TIMEOUT` |

`autonomous_live.py:268-270` is the only one that verifies the model matches its
pin and fails closed. The other three trust a module constant. The `X-Title`
header differs per caller, so OpenRouter-side attribution is fragmented.

**JSON salvage** — the same brace-scanning recovery implemented three times:

```
agent_architecture.py:81-93    _extract_json
autonomous_live.py:170-180     (inline, raises DecisionDenied)
mh_ui.py:623-633               _extract_json_object
```

The three differ in error type (`ValueError` vs `DecisionDenied`), in the error
message, and in exception handling: `autonomous_live.py:178` catches only
`ValueError` while the other two catch `(ValueError, TypeError)`. A response that
raises `TypeError` is denied in one path and crashes in another. That asymmetry is
exactly the kind of thing that makes an intermittent failure hard to reproduce.

**Severity: Medium.** Consolidating this removes four places to patch a provider
change, and removes one place where a `TypeError` is not fail-closed.

---

## 5. D5 — `track_whales.py` re-declares the asset universe that config owns

`track_whales.py:47-51` hardcodes the mint list, directly under a comment that
claims config is the single source of truth:

```
track_whales.py:46   # Tradeable mints from config.yaml (single source of truth for signals)
track_whales.py:47   COIN_MINTS = {
track_whales.py:48       "So11111111111111111111111111111111111111112": "SOL",
track_whales.py:49       "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN": "JUP",
track_whales.py:50       "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs": "ETH",
```

Verified by reading `config.yaml`: the three mints are **currently identical** to
`config.yaml` `coins:`. So this is not a live wrong answer today. It is a
comment that lies about its own mechanism.

The failure mode is a silent one, at `track_whales.py:100`:

```
symbol = COIN_MINTS.get(mint, mint)
```

Add a fourth coin to `config.yaml` and `track_whales` will not see it. Worse, the
fallback returns the raw mint string as the symbol, so downstream rows get a
44-character "symbol" rather than a ticker. No exception. `track_whales` is a
supervised process (`deploy/multihedge.supervisord.conf:97-98`).

Separately, the whale wallet list is duplicated between `track_whales.py:38-44`
and `mh_whale_trader.py:32-38`, with the trader copy labelled
`# 5 VERIFIED fomo.family wallets (mirrors track_whales.py)`. I verified the two
address sets are byte-identical (5 addresses, no difference in either direction).
The tracker version carries `handle` and `rank`; the trader copy throws both away.
A wallet added to the tracker will not be tradeable by the trader.

**Severity: Medium-High.** The mechanism is honest-looking and fails silently.

---

## 6. Cross-check against the existing gate — one correction to make

`docs/atlas/00_MASTER_INDEX.md:33` and the brief both state the gate reports
**74** violations. Executed now against HEAD `35f922a`:

```
$ python3 tools/check_single_truth.py --quiet; echo $?
  31 A_cross_module_global_mutation
  27 A_db_path_construction
   9 A_db_env_var_outside_resolver
   8 B_direct_config_reader
   1 G_dashboard_policy_literal
  = 76
1
```

So **74 is stale; the real current count is 76.** The baseline
(`ATLAS_SCAN.json.baseline.git_sha` = `cd70d62`) is behind HEAD (`35f922a`), and
`mh_dash.py:30` plus the `audit/` snapshot tree have moved. Two rules changed
count without anyone updating the index: `B_direct_config_reader` shows 8 here
against 11 in the index, while `A_db_path_construction` shows 27 against a
documented 27 with a much larger "133 sites" figure in FINDING 001 that the gate
does not reproduce (the gate's regex is narrower than the scanner's).

Two separate things are being conflated in the index: the **scanner** counts
(ATLAS_SCAN.json, 133 db sites) and the **gate** counts (check_single_truth.py,
27). They are different regexes over the same tree. Worth stating explicitly so
nobody reconciles 74 against 133 and concludes one of them is lying.

**Gate coverage gap relevant to this seat:** `G_dashboard_policy_literal` only
matches `(trail_distance_pct|take_profit_pct|stop_loss_pct|trail_arm_pct|max_hold_seconds)`.
It cannot see `mh_dash.py:543`'s hardcoded `20` / `0.75` eligibility thresholds,
which is the more dangerous copy because it decides live promotion.

---

## 7. What I could NOT verify

Stated plainly, per the project standard:

1. **Whether the 5 position fractions conflict with `survival_policy.py:17-18`.**
   Depends on whether "free balance" and "treasury" share a base. Not resolved.
2. **Whether `mh_dash.py:543` is actually reachable in production.** The endpoint
   exists at `mh_dash.py:526` and `dash` is supervised
   (`deploy/multihedge.supervisord.conf:65-67`), but I did not issue a live HTTP
   request to `/api/livegate`, so I have not observed the 0.75 verdict rendering.
   The code path is verified; the rendered output is not.
3. **Whether the `mh_trades` schema divergence has bitten yet.** Three modules
   declare `mh_trades` independently — `paper.py:98` (with `cost_usd`),
   `dynamic_shadow_scalper.py:67` (without), `mh_reasoner.py:143` (without, and
   all columns nullable). Production has 14 columns including `cost_usd`, so
   `paper.py`'s declaration won. I did not test what happens on a fresh DB where
   `mh_reasoner` connects first.
4. **Exit-evaluation cadence.** Unchanged from FINDING 007: still no instrumented
   exit-check timestamp, so the 20%-in-30-minutes question stays unanswerable.
