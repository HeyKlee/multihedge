# Verified paper evidence audit

Previous excursion results table withdrawn as unsupported. This report comes from an actual read-only SQLite backup and pure replay calls. Production DB was not edited.

## Exit sweep
360 TP/SL/hold policies tested, fixed default trailing, assumed 0.8% round-trip cost. No candidate has complete training-path coverage. No policy selected or promoted.

MEME: 175 trades; 216 policies; baseline 160 resolved, 15 unresolved; resolved-only mean -2.2525%, profit factor 0.6497. These partial-sample statistics do not establish full-cohort profitability.

SERIOUS: 7 trades; 144 policies; baseline 6 resolved, 1 unresolved; resolved-only mean -0.8918%, profit factor 0.6109. These partial-sample statistics do not establish full-cohort profitability.

## Volatility evidence
{
  "observations": 4220,
  "labels": 4022,
  "eligible_feature_labels": 1657,
  "eligible_original_signal_labels": 153,
  "original_signal_total": 431,
  "exclusions": {
    "fewer_than_6_prior_prices": 451,
    "coverage_less_than_30min": 184,
    "gap_exceeds_5min": 1775,
    "label_missing_or_outside_1h_tolerance": 153
  },
  "method": "Root sum of squared log returns on strictly prior mint-scoped prices in 1h, >=6 prices, >=30min coverage, <=5min gaps; labels <=65min after observation. Diagnostic proxy, not annualized volatility.",
  "train": 112,
  "test": 39,
  "train_mints": 3,
  "test_mints": 3,
  "nonoverlapping_signal_labels": 37,
  "independent_train": 26,
  "independent_holdout": 10,
  "decision": "Insufficient independent signal outcomes for an evidence-selected live-running shadow threshold; implement measurement/proposal only, enforcement disabled until qualified."
}

The 1h forward labels are not executable trade profits. Repeated overlapping labels are not independent trades. Threshold selection needs additional independent evidence; no arbitrary threshold was applied.

## Limitations
* Sampled paths cannot establish intragap ordering or executable liquidity.
* Candidate outcomes marked unresolved are never filled with historical realized returns.
* Default-policy baseline is not proof of the historically applied policy.
* Fixed trailing policy; timer alone never exits.

## Completed separate baseline replay
{
  "observations": 4220,
  "distinct_mints": 25,
  "closed_trades": 22,
  "open_positions": 10,
  "net_closed_usd": 0.13897066228820762,
  "net_closed_win_rate": 0.5909090909090909,
  "net_final_marked_equity_usd": 10.254206836987823,
  "gap_flagged_closed_trades": 16,
  "max_final_open_mark_age_seconds": 58388.055212020874,
  "promotion_allowed": false
}
CSV trade count and net P&L reconciled against report and simulation database. This baseline is different from the closed-trade exit sweep and not evidence of a successful improvement.
