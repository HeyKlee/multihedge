# MultiHedge Continuous Optimisation Report

## 1. Executive verdict
No trading-policy change is safe to deploy. The authoritative scalper ledger is negative before modeled costs, the untouched test contains only 19 trades, and the recorded data cannot support unbiased counterfactual strategy selection.

## 2. Baseline
Source: `/home/kelly/multihedge/backtest-results/all-traders-20260915-230049/20260915-230049/authoritative-prechange.db` (fcc35c1e237d737087763888a952e09e04cb7fa8d0d924db12a3063ab6f95d07). Period: 2026-09-08T07:02:08.983763+00:00 to 2026-09-15T10:38:23.703717+00:00. SQLite integrity: ok.
Costs: 40 bps per side, 80 bps round trip, fixed cost $0.00.
Scalper: 366 trades, gross P&L $-0.7987, modeled costs $10.8847, net P&L $-11.6834, net return -48.68%, win rate 32.51%, expectancy $-0.0319/trade, profit factor 0.470, maximum drawdown 48.68%.

| Trader | Closed trades | Gross P&L | Modeled costs | Net P&L | Net return | Max DD |
|---|---:|---:|---:|---:|---:|---:|
| scalper | 366 | $-0.7987 | $10.8847 | $-11.6834 | -48.68% | 48.68% |
| reasoner | 550 | $-0.4755 | $31.7492 | $-32.2247 | -134.27% | 133.90% |
| whale_trader | 1 | $0.0372 | $0.0768 | $-0.0396 | -0.17% | 0.17% |
| memecoin_trader | 0 | $0.0000 | $0.0000 | $0.0000 | 0.00% | 0.00% |
| grid | 35 | $0.6205 | $0.7000 | $-0.0795 | -0.33% | 0.64% |

Data quality: 1 invalid trade row, 51 P&L formula mismatches across all traders, and 0 exact duplicate groups. 51 rows were excluded from this reconstruction because their persisted dollar P&L omits the entry-price factor (usd == qty*pct instead of qty*entry_px*pct); the figures above are therefore a smaller, cleaner sample, not a repaired ledger. Scalper account reconciliation differs by $1.2608, so reconstructed returns are evidence for rejection, not proof of a clean deployable edge. Regime, confidence calibration, quote-failure rate, spread, and latency breakdowns are unavailable because those fields are not persisted at trade level.

## 3. Diagnosed weaknesses
1. Scalper accounting omits configured quote impact from persisted trade P&L, so dashboard and strategy feedback overstate net performance. Confidence: high.
2. Exploration completion depends on closed trades, causing rarely firing setups to consume thousands of selections without reaching the six-trade floor. Confidence: high.
3. Points-only exploitation treats payoff magnitudes as identical and ignores costs, recency, and uncertainty. Confidence: high.
4. Trade-level failed quotes, provider identity, spread, latency, and executable quote impact are not persisted, preventing full execution-quality attribution. Confidence: high.

## 4. Experiments
Challenger: trade only when the setup's prior cost-adjusted mean return minus one standard error is positive, requiring at least 20 prior closed trades for that coin/setup. Search space was fixed to one hypothesis with no parameter sweep. The replay is conservative but selection-biased because unchosen setup outcomes do not exist.

## 5. Champion versus challenger
| Period | Policy | Trades | Net P&L | Expectancy | Max DD |
|---|---:|---:|---:|---:|---:|
| train | champion | 256 | $-6.2202 | $-0.0243 | 25.92% |
| train | challenger | 6 | $0.1648 | $0.0275 | 0.52% |
| validation | champion | 55 | $-2.4157 | $-0.0439 | 14.43% |
| validation | challenger | 9 | $-0.6524 | $-0.0725 | 3.99% |
| test | champion | 55 | $-3.0475 | $-0.0554 | 13.13% |
| test | challenger | 6 | $0.0350 | $0.0058 | 0.69% |

Walk-forward folds won: 0 of 4. Promotion gates: FAIL (test_sample_below_20, total_sample_below_50, negative_stress_expectancy, walk_forward_majority_not_won).

## 6. Risk assessment
Baseline maximum drawdown was 48.68%; longest losing streak was 15 trades; worst trade was $-0.2337; 95% expected shortfall was $-0.2005. Portfolio heat and correlated exposure cannot be reconstructed exactly from closed rows alone. Rollback trigger is any trading-code or parameter change, because none was accepted.

## 7. Implementation
Added a read-only weekly evaluator only. No strategy, sizing, exit, wallet, credential, confirmation flag, live gate, or runtime database was changed.

## 8. Verification
The evaluator opens the database with SQLite `mode=ro`, checks integrity, applies chronological 70/15/15 splits, calculates uncertainty-aware and stress metrics, and fails closed when sample, expectancy, walk-forward, stress, or drawdown gates fail.

## 9. Deployment state
rejected. Champion unchanged.

## 10. Weekly scorecard
Realised gross return before modeled costs: -3.33%. Cost-adjusted net return: -48.68%. The 20% strong-week outcome remains context only, not a quota or pass condition.

## 11. Next experiment
Persist executable entry and exit quote impact plus shadow recommendations and opportunity outcomes for every enabled setup. After at least 50 closed shadow opportunities overall and 20 per promoted coin/setup, rerun the same untouched-test and walk-forward gates.

VERDICT: NO SAFE IMPROVEMENT PROVEN, CHAMPION UNCHANGED
