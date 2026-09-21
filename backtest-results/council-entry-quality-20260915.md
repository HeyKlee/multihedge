# XORA-SURVIVAL Entry-Quality Council Record

## Question
What is the safest evidence-first next improvement after exit-policy replay reduced loss severity but did not achieve profitability?

## Evidence considered
- 148 common MEME price paths, 80 bps modeled round-trip cost.
- Untouched 37-path holdout, current MEME exit policy: mean net return -3.95%, win rate 43.24%, payoff ratio 0.556, compounded-return proxy -81.34%, maximum-drawdown proxy 81.34%.
- Defensive exit candidate: mean net return -1.74%, same 43.24% win rate, compounded-return proxy -53.21%, maximum-drawdown proxy 58.37%.
- Historical excursion data stores only post-entry path outcomes. It has no observations of rejected candidates, no executable entry quote, and no entry-time liquidity or market-regime label. It cannot support a counterfactual entry-filter claim.
- The new shadow-only observation ledger captures candidate price, 5m/1h returns, five-minute buy/sell volume, current entry decision and entry-opened status. It is not yet deployed, so there are zero production observations.

## Council availability
The requested OpenRouter free council was invoked but returned malformed persona payloads and timed out after five minutes. It produced no persisted verdict. The manual five-persona fallback below is therefore the decision record.

## Manual council

### Advocate
The strongest justified change is to make the observation ledger outcome-linkable. Each opened shadow position should be connected to its exact entry snapshot, then annotated only when its existing deterministic exit closes. This turns future entries into a chronological supervised dataset and allows a shadow candidate gate to test whether a proposed filter avoids losses without claiming it would have captured unseen gains.

### Skeptic
A simple momentum or volume threshold would be curve-fitting. Five-minute return, one-hour return, and volume can be correlated with price path, but the existing history cannot show how many good trades that threshold would reject. A new filter must be refused unless it beats the current entry rule on a strictly later chronological holdout, after costs, with enough accepted and rejected observations.

### Oracle
The observed holdout has 37 paths, 43.24% wins and negative expectancy under every tested exit policy. The all-trader evidence also contains historical ledger rows that do not reconcile and must remain excluded. With no candidate-level counterfactual history, the prior probability that a threshold selected from existing taken trades generalizes is low. There is no empirical support for changing entry thresholds today.

### Contrarian
The dominant cause may be market-wide regime and execution quality rather than token-level 5m/1h momentum. A model that only records token metrics could mistake a market selloff or thin liquidity for a token signal. The data must include a market regime label and actual entry quote/slippage estimate before judging a token-entry feature. If those cannot be captured reliably, entries should be reduced by a deterministic risk throttle rather than tuned for profitability.

### Arbiter
Confidence is high that the next step is data instrumentation, not a filter change. The posterior for a new hard entry threshold being beneficial is insufficient because the current data lacks rejected-candidate outcomes and regime controls. The approved paper-only hypothesis is that an entry filter using only pre-entry, finite, timestamped values may reduce post-cost loss severity while retaining sufficient trade count. It must be judged on future chronological data, not legacy outcomes.

## Approved shadow-only next change
Add an append-only outcome linkage from a closed dynamic-shadow position to its exact candidate observation. Record only values known at entry or close:

- observation key: observed_ts + mint
- exact opened position key: mint + opened_ts
- close_ts and deterministic exit_reason
- realized_pct and realized_usd only after close
- entry quote price and measured quote-cost/slippage when available
- explicit market-regime snapshot fields, only if a timestamped source is available
- data-validity status and immutable policy cohort identifier

No live execution, signer, wallet, position sizing, TP/SL, trailing stop, max hold, configuration, or entry threshold changes are approved.

## Future promotion gate for an entry-filter candidate
Do not apply any shadow entry filter until all conditions hold:

1. At least 100 valid outcome-linked candidate observations in one immutable policy cohort, with both opened and rejected records.
2. At least 30 closed, path-ready opened positions in the prospective filtered cohort.
3. Chronological split only. Choose candidate thresholds on training data and test once on a later untouched holdout of at least 30 closed positions.
4. Include round-trip costs and reject samples without finite, timestamp-valid feature values.
5. Candidate must improve holdout post-cost expectancy by at least 2 percentage points and not worsen maximum drawdown or reduce post-filter sample below 60% of the baseline holdout.
6. A separate stress split must not be negative when the candidate claims a market-regime benefit.
7. Promotion remains shadow-only until the existing independent evidence gate authorizes a separate live change.

## Explicitly rejected changes
- Lowering evidence thresholds or sample requirements.
- Switching networks, enabling live promotion, or modifying confirm_live.flag.
- Changing wallet/signer policy or USDC reserve limits.
- Changing TP, SL, trailing stop, max hold, or entry sizing based on this evidence.
- Training on unreconciled historical P&L rows or legacy policy-unlabelled excursions as if they were current-policy evidence.
