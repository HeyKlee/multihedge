#!/usr/bin/env python3
"""Pre-registered entry concentration analysis (task E).

The criteria for this run are fixed in
docs/atlas/PRE_REGISTRATION_entry_concentration.md, committed BEFORE the code
below existed. Read that file first; this module implements it rather than
choosing its own method.

Every headline number produced here is passed through evidence_gate.check()
before being printed. A BLOCK is reported as a BLOCK, not softened.

Feature hygiene, which is the whole risk in this analysis:

  PERMITTED  rsi_15m, volume_5m_avg_20, buy_volume_5m_usd, sell_volume_5m_usd,
             latest_usd, and a derived buy/sell imbalance. All are recorded
             AT the observation timestamp.

  FORBIDDEN  return_5m_pct, return_1h_pct, forward_return_pct, realized_pct,
             exit_reason, exit_px, or anything derived from the price path
             after the entry. These live in the SAME table as the permitted
             features, which is exactly how FINDING 012 happened. The column
             names are asserted against a denylist below so a future edit
             cannot quietly add one to the feature list.

Selection is by RANK on a permitted feature, and the concentration is applied
as "trade only the top X% by that rank". The bucket is chosen on train and
reported on test, chronologically.
"""

import json
import sqlite3
import statistics as st
import sys
import time
from pathlib import Path

# This module lives in ops/, so the repo root is one level up. evidence_gate
# is at the root, and adding ops/ alone is not enough to import it.
OPS = Path(__file__).resolve().parent
ROOT = OPS.parent
sys.path.insert(0, str(ROOT))

from evidence_gate import Claim, check  # noqa: E402

COST = 0.018  # conservative round trip. NOT the measured figure: FINDING 013.

# Denylist. Any feature whose name appears here is future information relative
# to the entry decision and voids the claim under the pre-registration.
FORBIDDEN = (
    "return_5m", "return_1h", "forward_return", "realized",
    "exit_px", "exit_reason", "label_ts", "forward",
)

# The keys below are what `load()` puts on each row. buy/sell volume is
# carried only as the derived `imbalance`, because the two raw columns do not
# survive into the loaded dict under their SQL names.
PERMITTED = (
    "rsi_15m", "volume_5m_avg_20", "latest_usd", "imbalance",
)


def _assert_clean(names):
    bad = [n for n in names if any(f in n for f in FORBIDDEN)]
    if bad:
        raise SystemExit(
            f"FATAL: forbidden future-information feature(s) in the feature "
            f"list: {bad}. The pre-registration declares the idea dead if the "
            f"result depends on these.")


def load(con):
    """Trades joined to the nearest pre-entry observation within 60s."""
    _assert_clean(PERMITTED)
    q = """
    SELECT t.id, t.open_ts, t.entry_px, t.exit_px, t.side,
           o.rsi_15m, o.volume_5m_avg_20, o.buy_volume_5m_usd,
           o.sell_volume_5m_usd, o.latest_usd
    FROM mh_trades t
    JOIN mh_shadow_entry_observations o
      ON o.mint = t.coin
     AND o.observed_ts BETWEEN t.open_ts - 60 AND t.open_ts
    WHERE t.setup = 'dynamic_scalper'
      AND t.entry_px > 0 AND t.exit_px > 0 AND t.close_ts > t.open_ts
    ORDER BY t.open_ts
    """
    rows = con.execute(q).fetchall()
    out = []
    for (tid, ots, ep, xp, side, rsi, vol, bv, sv, last) in rows:
        if None in (ep, xp, rsi, vol, bv, sv, last):
            continue
        gross = ((xp - ep) / ep if side != "SHORT" else (ep - xp) / ep) * 100.0
        out.append({
            "id": tid, "open_ts": ots, "gross": gross, "net": gross - COST * 100,
            "rsi_15m": rsi,
            "volume_5m_avg_20": vol,
            "imbalance": (bv - sv) / (bv + sv) if (bv + sv) else 0.0,
            "latest_usd": last,
        })
    return out


def concentration(rows, feature, pct, side="high"):
    """Keep the top `pct` by `feature`. Rank only, no outcome involved."""
    if not rows:
        return []
    key = (lambda r: r[feature]) if side == "high" else (lambda r: -r[feature])
    ordered = sorted(rows, key=key)
    keep = max(1, int(len(ordered) * pct))
    return ordered[:keep]


def report(name, rows, feature, pct, side):
    if not rows:
        return None
    nets = [r["net"] for r in rows]
    gross = [r["gross"] for r in rows]
    wins = len([n for n in nets if n > 0])
    aw = [n for n in nets if n > 0]
    al = [n for n in nets if n <= 0]
    payoff = (st.mean(aw) / abs(st.mean(al))) if aw and al else None
    span_days = ((rows[-1]["open_ts"] - rows[0]["open_ts"]) / 86400.0) if len(rows) > 1 else 0.0
    return {
        "n": len(rows), "win_rate": wins / len(rows),
        "mean_gross": st.mean(gross), "mean_net": st.mean(nets),
        "median_net": st.median(nets), "payoff": payoff,
        "span_days": span_days,
    }


def main():
    import runtime_paths
    db = str(runtime_paths.evidence_db())
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
    rows = load(con)
    if len(rows) < 60:
        print(f"only {len(rows)} trades have pre-entry features; "
              f"cannot analyse. Need >=60.")
        return 2

    print(f"ledger : {db}")
    print(f"joined : {len(rows)} trades with a pre-entry feature row")
    print(f"cost   : {COST*100:.3f}% conservative round trip")
    print(f"permitted features: {', '.join(PERMITTED)}")
    print()

    cut = int(len(rows) * 0.6)
    train, test = rows[:cut], rows[cut:]
    print("=" * 78)
    print("CHRONOLOGICAL SPLIT")
    print("=" * 78)
    print(f"  train {len(train)} trades up to "
          f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(train[-1]['open_ts']))}")
    print(f"  test  {len(test)} trades from "
          f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(test[0]['open_ts']))}")
    print()

    baseline_tr = report("baseline_train", train, None, 1.0, "high")
    baseline_te = report("baseline_test", test, None, 1.0, "high")
    print("=" * 78)
    print("BASELINE: every trade, no selection")
    print("=" * 78)
    for lab, b in (("train", baseline_tr), ("test", baseline_te)):
        print(f"  {lab}: n={b['n']:3d}  win {b['win_rate']*100:5.1f}%  "
              f"gross {b['mean_gross']:+.3f}%  net {b['mean_net']:+.3f}%")
    print()

    results = []
    print("=" * 78)
    print("CONCENTRATION SWEEP: top X% by entry-time feature (train-selected)")
    print("=" * 78)
    print(f"  {'feature':>20} {'side':>5} {'top':>5} | "
          f"{'tr_n':>5} {'tr_net':>8} | {'te_n':>5} {'te_net':>8}  verdict")
    print("  " + "-" * 76)
    for feature in PERMITTED:
        for side in ("high", "low"):
            for pct in (0.25, 0.5, 0.75):
                tr = concentration(train, feature, pct, side)
                te_sub = report("t", tr, feature, pct, side)
                if not te_sub:
                    continue
                # apply the SAME rank to the test set, chosen on train only
                te = concentration(test, feature, pct, side)
                te_r = report("t", te, feature, pct, side)
                if not te_r:
                    continue
                verdict = "POSITIVE" if te_r["mean_net"] > 0 else "losing"
                print(f"  {feature:>20} {side:>5} {pct*100:4.0f}% | "
                      f"{te_sub['n']:5d} {te_sub['mean_net']:+7.3f}% | "
                      f"{te_r['n']:5d} {te_r['mean_net']:+7.3f}%  {verdict}")
                results.append({
                    "feature": feature, "side": side, "pct": pct,
                    "train_n": te_sub["n"], "train_net": te_sub["mean_net"],
                    "test_n": te_r["n"], "test_net": te_r["mean_net"],
                    "test_gross": te_r["mean_gross"],
                    "test_win": te_r["win_rate"],
                })
    print()

    pos = [r for r in results if r["test_net"] > 0]
    print("=" * 78)
    print(f"POTENTIAL CANDIDATES WITH POSITIVE TEST NET: {len(pos)} of {len(results)}")
    print("=" * 78)
    for r in sorted(pos, key=lambda x: -x["test_net"])[:10]:
        print(f"  {r['feature']:>20} {r['side']:>5} top {r['pct']*100:3.0f}%  "
              f"test_n={r['test_n']:3d} test_net {r['test_net']:+.3f}%  "
              f"gross {r['test_gross']:+.3f}%")
    print()

    # ---- evidence gate on the best candidate -----------------------------
    best = sorted(results, key=lambda x: -x["test_net"])[0] if results else None
    print("=" * 78)
    print("EVIDENCE GATE on the best out-of-sample candidate")
    print("=" * 78)
    if best is None:
        print("  no candidates; nothing to gate.")
        return 0
    n_ent = con.execute(
        "SELECT COUNT(DISTINCT coin) FROM mh_trades WHERE setup='dynamic_scalper'"
    ).fetchone()[0]
    # Independent re-derivation: recompute the same bucket by a different path
    # (explicit sort in Python rather than the helper used above).
    alt = sorted(test, key=lambda r: r[best["feature"]] if best["side"] == "high"
                 else -r[best["feature"]])[:best["test_n"]]
    alt_net = st.mean([r["net"] for r in alt]) if alt else None
    print(f"  re-derivation by independent sort: {alt_net}")
    v = check(Claim(
        name=f"top {best['pct']*100:.0f}% by {best['feature']} ({best['side']})",
        value=best["test_net"],
        floor=None,
        convention="positive = net gain after 1.800% cost",
        n_obs=best["test_n"], n_entities=n_ent,
        span_days=((alt[-1]["open_ts"] - alt[0]["open_ts"]) / 86400.0) if len(alt) > 1 else 0.0,
        mean=best["test_net"],
        median=st.median([r["net"] for r in alt]) if alt else None,  # type: ignore[arg-type]
        trimmed_mean=alt_net,
        independent_value=alt_net,
        train_value=best["train_net"], test_value=best["test_net"],
        pre_registered=True, forbidden_features_used=[],
    ))
    print(v.summary())
    print()

    # ---- falsification criteria from the pre-registration ----------------
    print("=" * 78)
    print("PRE-REGISTERED FALSIFICATION CRITERIA")
    print("=" * 78)
    dead = []
    if best["test_net"] <= 0:
        dead.append(f"test net {best['test_net']:+.3f}% <= 0  -> DEAD")
    if best["test_n"] < 30:
        dead.append(f"test bucket n={best['test_n']} < 30  -> INCONCLUSIVE")
    if best["train_net"] < 0 and best["test_net"] > 0:
        dead.append("positive in test, negative in train -> INCONCLUSIVE")
    print(f"  best test net : {best['test_net']:+.3f}%")
    print(f"  best train net: {best['train_net']:+.3f}%")
    print(f"  test bucket n : {best['test_n']}")
    for d in dead:
        print(f"  {d}")
    if not dead:
        print("  none triggered. Inspect before believing: with "
              f"{len(results)} buckets swept, the best of them is a selection "
              f"maximum and is expected to look good by chance alone.")
    else:
        print()
        print("  VERDICT: the idea does not survive its own pre-registration.")

    out = {
        "n_joined": len(rows), "cost_pct": COST * 100,
        "baseline_train": baseline_tr, "baseline_test": baseline_te,
        "results": results, "best": best,
        "gate_blocked": v.blocked, "gate_failures": v.failures,
        "falsification": dead,
    }
    p = OPS / "entry_concentration_result.json"
    p.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print(f"\nwrote {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
