# MultiHedge Curator Fine-Tune Dataset

Fine-tuning pairs for a tiny open-weight model (target: Qwen3-0.6B-Instruct) that
curates entry, take-profit, stop-loss, and exit decisions for the MultiHedge bot.

## Source
Built from the deployed ledger `/home/kelly/multihedge/deploy/data/multihedge.db`
(491 realized `mh_trades`) and per-coin `mh_pxhist` (SOL/ETH/JUP, 106k bars),
window Aug 24 - Sep 8. The `agent_decision_log` table was inspected and rejected
as a signal source: 62 of 112 trader decisions are `hold` at ~0.5 confidence and
the rest are low-signal `buy` rows, so it carries no usable curation signal.

## Files
- `build_curator_dataset.py` - stage 1: raw feature/label rows from DB
- `curator_raw.jsonl` - 490 rows, one per traded position (1 dropped for sparse px)
- `emit_sft.py` - stage 2: raw rows to Qwen3-style `messages` pairs
- `train.jsonl` - 416 pairs, 85%
- `val.jsonl` - 74 pairs, 15% (last 15% chronologically, no random shuffle)

## Record shape (train.jsonl)
```
{"messages": [
   {"role":"system","content":"You are the MultiHedge entry/exit curator..."},
   {"role":"user","content":"Coin: SOL | Setup: reasoner | Side: LONG | Entry: 98.24 | Dev5/Dev10/Vol/RSI/Mom1h"},
   {"role":"assistant","content":"{...strict JSON curator decision...}"}
]}
```

## Inputs (user turn, pre-entry only, no forward leak)
coin, setup, side, entry_px, dev5 (price vs 5-bar SMA), dev10 (vs 10-bar SMA),
intra-window return volatility, RSI(14), 1h momentum.

## Targets (assistant turn, hindsight-derived)
- `entry` - the executed entry (kept as-is)
- `tp_pct` - return to take profit = half the realized favorable excursion (MFE),
  floored at 3x noise vol so it is achievable and not inside spread noise
- `sl_pct` - return to stop loss = 1.2x the worst adverse excursion (MAE), widened
  to sit beyond noise
- `exit` - `take_profit` / `trail_stop` / `close_position` mapped from realized
  exit_reason and P&L
- `confidence` - bounded [0.5, 0.95] from |realized_pct|

## Cheat-check
- Inputs use only pre-open price data; hindsight feeds the target only.
- Split is time-ordered (val = last 15% of trades) so no cross-split leakage.

## Honest caveats
- These labels reproduce the bot's realized behavior, which is mostly breakeven
  (median MFE +0.47%, median MAE -0.46%). The model learns "curate like these
  outcomes," not "trade profitably." Real label quality is bounded by the bot's
  own past performance. Expect the fine-tune to match SQLite-derived curations,
  not to outperform them.
- 490 samples is small for fine-tuning; expect instability. Recommend LoRA r=16,
  2-3 epochs, LR ~1e-4, lr_scheduler_type cosine, no wandb unless desired.
- Feat vol is a coarse proxy (pstdev of 1h intra-window returns), not a true ATR.

## Next step
LoRA fine-tune on Google Colab free (Qwen3-0.6B), then export q4 GGUF via llama.cpp
for local inference on the homelab box, wired as a curator stage after the existing
analyst/researcher/trader agents per `multihedge-agent-development` skill.