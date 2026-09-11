#!/usr/bin/env python3
"""Stage 2: map curator_raw.jsonl into Qwen3-style SFT instruction/completion pairs.

Target derivation (defensible, hindsight-based training targets):
  - entry       = the executed entry price
  - tp_pct      = take-profit return; take half the realized favorable excursion (MFE)
                  so the rule is achievable and survives noise (TP = MFE * 0.5).
  - sl_pct      = stop-loss return; place the stop just beyond the worst adverse
                  excursion (SL = MAE * 1.2) so it is not stopped out by noise.
  - exit        = realized exit action, mapped to curated guidance.
  - confidence  = bounded, setup-agnostic confidence from |realized_pct|.
Inputs are pre-open features ONLY; targets use hindsight. Train/val split is
time-ordered (no random) to avoid temporal leakage between split edges.
"""
import json, os, math

RAW = "/home/kelly/multihedge/dataset/curator_raw.jsonl"
OUT_TRAIN = "/home/kelly/multihedge/dataset/train.jsonl"
OUT_VAL = "/home/kelly/multihedge/dataset/val.jsonl"

rows = [json.loads(l) for l in open(RAW) if l.strip()]

SYSTEM = (
    "You are the MultiHedge entry/exit curator. Given a coin, setup, side, and "
    "pre-entry market context, recommend a concrete entry, take-profit return, "
    "stop-loss return, and exit action as strict JSON. Use the given entry price "
    "as-is. Return JSON only, no prose."
)

def build_completion(r):
    side = r["side"]
    entry = r["entry_px"]
    mfe = r["label_mfe"]
    mae = r["label_mae"]

    # Noise floor = 3x intra-window vol estimate, so targets sit above noise.
    noise_floor = max(3.0 * r["feat_vol"], 0.002)
    # TP = half the favorable excursion, floored above noise so it is achievable
    # and not inside spread noise.
    tp_pct = max(mfe * 0.5, noise_floor) if mfe > 0 else noise_floor
    # SL = just beyond worst adverse excursion, widened to sit beyond noise.
    sl_pct = mae * 1.2 if mae < -noise_floor else -noise_floor

    if r["label_action"] == "cut_loss":
        exit_act = "close_position"
        reason = "realized loss; cut to preserve capital"
    elif r["exit_reason"] == "take_profit":
        exit_act = "take_profit"
        reason = "target reached; bank the win"
    else:
        exit_act = "trail_stop"
        reason = "let winner run with a trailing stop"

    conf = round(min(0.95, 0.5 + abs(r["realized_pct"]) * 8.0), 4)

    decision = {
        "coin": r["coin"],
        "setup": r["setup"],
        "side": side,
        "entry": round(entry, 6),
        "tp_pct": round(tp_pct, 6),
        "sl_pct": round(sl_pct, 6),
        "exit": exit_act,
        "confidence": conf,
        "reason": reason,
        "trade_id": r["trade_id"],
    }
    return json.dumps(decision)

def build_user(r):
    frsi = r["feat_rsi"]
    return (
        f"Coin: {r['coin']} | Setup: {r['setup']} | Side: {r['side']} | "
        f"Entry: {r['entry_px']} | Dev5: {r['feat_dev5']:.4f} | Dev10: {r['feat_dev10']:.4f} | "
        f"Vol: {r['feat_vol']:.6f} | RSI(14): {frsi if frsi is not None else 'n/a'} | "
        f"Mom1h: {r['feat_mom1h']:.4f}"
    )

records = []
for r in rows:
    user = build_user(r)
    completion = build_completion(r)
    records.append({
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": user},
            {"role": "assistant", "content": completion},
        ]
    })

# time-ordered split: use raw trade_id ordering (already chronological)
n = len(records)
n_val = max(1, int(round(n * 0.15)))
val = records[-n_val:]
train = records[:-n_val]

with open(OUT_TRAIN, "w") as f:
    for rec in train:
        f.write(json.dumps(rec) + "\n")
with open(OUT_VAL, "w") as f:
    for rec in val:
        f.write(json.dumps(rec) + "\n")

print(f"total pairs: {n}  train: {len(train)}  val: {len(val)}")
print(f"wrote {OUT_TRAIN} and {OUT_VAL}")

# sanity: show 3 examples
for ex in records[:3]:
    for m in ex["messages"]:
        print(f"  [{m['role']}] {m['content'][:120]}")
    print("  ---")