#!/usr/bin/env python3
"""Score logged JEV calls against the price that actually followed.

Reads deploy/data/agent_logs/jev_calls.jsonl (written by autonomous_live.py),
and for every call whose horizon has elapsed looks up the entry price and the
price at entry+horizon from the live price history, then records whether the
call was right.

Hit rules:
  BUY   right when forward return > 0
  SELL  right when forward return < 0
  HOLD  right when |forward return| stayed inside the band (default 0.25 pct),
        i.e. the model correctly called "no actionable move"

Prices are read through the writer container so the scorecard sees the live WAL
state rather than the last host-side checkpoint.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path("/home/kelly/multihedge")
DATA = ROOT / "deploy" / "data"
LOGS = DATA / "agent_logs"
CALLS = Path(os.getenv("MULTIHEDGE_JEV_CALL_LOG", str(LOGS / "jev_calls.jsonl")))
SCORED = Path(os.getenv("MULTIHEDGE_JEV_SCORECARD", str(LOGS / "jev_scorecard.jsonl")))
HOLD_BAND_PCT = float(os.getenv("MULTIHEDGE_JEV_HOLD_BAND_PCT", "0.25"))
GRACE_S = 30


def score_call(choice: str, entry: float, exit_price: float, hold_band_pct: float) -> tuple[float, bool]:
    """Return (forward return pct, hit) for one call under the stated rules."""
    fwd_pct = (exit_price - entry) / entry * 100.0
    if choice == "BUY":
        hit = fwd_pct > 0
    elif choice == "SELL":
        hit = fwd_pct < 0
    else:
        hit = abs(fwd_pct) <= hold_band_pct
    return fwd_pct, bool(hit)


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def fetch_prices(pending: list[dict]) -> list[dict]:
    """Resolve entry/exit prices for each pending call via the container DB."""
    request = json.dumps([{"symbol": c["symbol"], "ts": c["ts"],
                           "horizon_s": c["horizon_s"]} for c in pending])
    script = (
        "import json,sqlite3,sys\n"
        "req=json.load(sys.stdin)\n"
        "con=sqlite3.connect('file:/app/multihedge.db?mode=ro',uri=True)\n"
        "out=[]\n"
        "for r in req:\n"
        "    def px(t, c=r['symbol']):\n"
        "        row=con.execute('SELECT px FROM mh_pxhist WHERE coin=? AND ts<=? "
        "ORDER BY ts DESC LIMIT 1',(c,t)).fetchone()\n"
        "        return row[0] if row else None\n"
        "    out.append({'symbol':r['symbol'],'ts':r['ts'],'entry':px(r['ts']),"
        "'exit':px(r['ts']+r['horizon_s'])})\n"
        "print(json.dumps(out))\n"
    )
    proc = subprocess.run(
        ["docker", "exec", "-i", "multihedge", "python3", "-c", script],
        input=request, capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"price lookup failed: {proc.stderr.strip()[:200]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def main() -> int:
    now = __import__("time").time()
    calls = load_jsonl(CALLS)
    if not calls:
        print(json.dumps({"state": "NO_CALLS", "logged": 0}))
        return 0
    scored_keys = {(r["ts"], r["symbol"]) for r in load_jsonl(SCORED)}
    pending = [c for c in calls
               if (c["ts"], c["symbol"]) not in scored_keys
               and now >= c["ts"] + c.get("horizon_s", 300) + GRACE_S]
    if not pending:
        print(json.dumps({"state": "NOTHING_DUE", "logged": len(calls),
                          "scored": len(scored_keys)}))
        return 0
    prices = fetch_prices(pending)
    added = 0
    with SCORED.open("a", encoding="utf-8") as handle:
        for call, price in zip(pending, prices):
            entry, exit_ = price.get("entry"), price.get("exit")
            if not entry or not exit_ or entry <= 0:
                continue
            choice = call.get("choice", "HOLD")
            fwd_pct, hit = score_call(choice, entry, exit_, HOLD_BAND_PCT)
            record = {
                "ts": call["ts"], "symbol": call["symbol"], "choice": choice,
                "probability": call.get("probability"),
                "confidence": call.get("confidence"),
                "horizon_s": call.get("horizon_s", 300),
                "forward_pct": round(fwd_pct, 4), "hit": bool(hit),
                "hold_band_pct": HOLD_BAND_PCT, "model": call.get("model"),
            }
            handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
            added += 1
    rows = load_jsonl(SCORED)
    summary = {}
    for row in rows:
        bucket = summary.setdefault(row["choice"], {"n": 0, "hits": 0})
        bucket["n"] += 1
        bucket["hits"] += 1 if row["hit"] else 0
    for bucket in summary.values():
        bucket["hit_rate_pct"] = round(100.0 * bucket["hits"] / bucket["n"], 1)
    directional = [r for r in rows if r["choice"] in ("BUY", "SELL")]
    print(json.dumps({
        "state": "SCORED", "added": added, "scored_total": len(rows),
        "by_action": summary,
        "directional_n": len(directional),
        "directional_hit_rate_pct": (round(100.0 * sum(1 for r in directional if r["hit"])
                                          / len(directional), 1) if directional else None),
        "hold_band_pct": HOLD_BAND_PCT,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
