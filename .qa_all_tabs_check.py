#!/usr/bin/env python3
"""Verify all nine dashboard tabs and widget-level data from production DB."""
import json, urllib.request

BASE = "http://127.0.0.1:9053"

def api(path):
    try:
        with urllib.request.urlopen(BASE + path, timeout=10) as r:
            return r.status, json.loads(r.read())
    except Exception as e:
        return 0, {"error": str(e)}

results = {}

# TAB 1: Overview
print("=== TAB: Overview ===")
s, d = api("/api/summary")
print(f"  /api/summary: HTTP {s}")
if s == 200:
    print(f"    active_live={d.get('active_live','?')} active_paper={d.get('active_paper','?')} trading_coins={d.get('trading_coins','?')}")
    results["overview_summary"] = {"status": "ok" if s == 200 else "error", "active_live": d.get('active_live'), "active_paper": d.get('active_paper')}
else:
    results["overview_summary"] = {"status": "error"}

# /api/trades (trade feed on overview)
s, d = api("/api/trades?limit=5")
if s == 200:
    trade_count = len(d)
    print(f"  /api/trades: {trade_count} recent trades")
    results["overview_trades"] = {"status": "ok", "count": trade_count}
else:
    results["overview_trades"] = {"status": "error"}

# /api/positions (live positions on overview)
s, d = api("/api/positions")
if s == 200:
    pos_count = len(d)
    print(f"  /api/positions: {pos_count} positions")
    results["overview_positions"] = {"status": "ok", "count": pos_count}
else:
    results["overview_positions"] = {"status": "error"}

# /api/market (market tick widget)
s, d = api("/api/market")
if s == 200:
    coins = [x.get("symbol","?") for x in d[:5]]
    stale = sum(1 for x in d if x.get("stale"))
    print(f"  /api/market: {len(d)} coins tracked, {stale} stale, sample: {coins}")
    results["overview_market"] = {"status": "ok", "count": len(d), "stale": stale}
else:
    results["overview_market"] = {"status": "error"}

# TAB 2: Xora-Survival
print("\n=== TAB: Xora-Survival ===")
s, d = api("/api/survival")
print(f"  /api/survival: HTTP {s}")
if s == 200:
    treasury = d.get("treasury_nzd","?")
    at_risk = d.get("at_risk","?")
    cycles = len(d.get("cycles",[]))
    wallet = d.get("wallet",{})
    print(f"    treasury_nzd={treasury} at_risk={at_risk} cycles={cycles}")
    print(f"    wallet type={type(wallet).__name__}")
    results["survival"] = {"status": "ok", "treasury": treasury, "at_risk": at_risk, "cycles": cycles}
else:
    results["survival"] = {"status": "error"}

# /api/livegate (Xora-Survival live gate status)
s, d = api("/api/livegate")
if s == 200:
    gate_keys = list(d.keys())
    print(f"  /api/livegate: gates={gate_keys}")
    results["livegate"] = {"status": "ok", "gates": gate_keys}
else:
    results["livegate"] = {"status": "error"}

# TAB 3: Reasoner
print("\n=== TAB: Reasoner ===")
s, d = api("/api/reasoner")
if s == 200:
    bias = d.get("bias","?")
    params = d.get("params",{})
    accts = d.get("accounts",[])
    print(f"  /api/reasoner: bias={bias} params_keys={list(params.keys())} accounts={len(accts)}")
    results["reasoner"] = {"status": "ok", "bias": bias, "params_keys": list(params.keys())}
else:
    results["reasoner"] = {"status": "error"}

# TAB 4: Whale
print("\n=== TAB: Whale ===")
s, d = api("/api/whales")
if s == 200:
    w_count = len(d) if isinstance(d, list) else "N/A"
    print(f"  /api/whales: {w_count} entries")
    results["whale"] = {"status": "ok", "count": len(d) if isinstance(d, list) else 0}
else:
    results["whale"] = {"status": "error"}

# TAB 5: Memecoin
print("\n=== TAB: Memecoin ===")
s, d = api("/api/memecoin")
if s == 200:
    m_count = len(d) if isinstance(d, list) else "N/A"
    print(f"  /api/memecoin: {m_count} entries")
    results["memecoin"] = {"status": "ok", "count": len(d) if isinstance(d, list) else 0}
else:
    results["memecoin"] = {"status": "error"}

# TAB 6: Grid
print("\n=== TAB: Grid ===")
s, d = api("/api/grid")
if s == 200:
    g_count = len(d) if isinstance(d, list) else "N/A"
    print(f"  /api/grid: {g_count} entries")
    results["grid"] = {"status": "ok", "count": len(d) if isinstance(d, list) else 0}
else:
    results["grid"] = {"status": "error"}

# TAB 7: Trading Research (system/edge curve)
print("\n=== TAB: Research/Edge ===")
s, d = api("/api/edge_curve")
if s == 200:
    print(f"  /api/edge_curve: {len(d)} points" if isinstance(d, list) else f"  edge_curve: non-list ({type(d).__name__})")
    results["research_edge"] = {"status": "ok"}
else:
    results["research_edge"] = {"status": "error"}

s, d = api("/api/strategies")
if s == 200:
    strat_count = len(d) if isinstance(d, list) else "N/A"
    print(f"  /api/strategies: {strat_count} strategies")
    results["research_strategies"] = {"status": "ok", "count": len(d) if isinstance(d, list) else 0}
else:
    results["research_strategies"] = {"status": "error"}

# TAB 8: Requests / Actions
print("\n=== TAB: Requests/Actions ===")
s, d = api("/api/actions")
if s == 200:
    action_count = len(d) if isinstance(d, list) else "N/A"
    print(f"  /api/actions: {action_count} actions")
    results["actions"] = {"status": "ok", "count": len(d) if isinstance(d, list) else 0}
else:
    results["actions"] = {"status": "error"}

s, d = api("/api/requests")
if s == 200:
    req_count = len(d) if isinstance(d, list) else "N/A"
    print(f"  /api/requests: {req_count} requests")
    results["requests"] = {"status": "ok", "count": len(d) if isinstance(d, list) else 0}
else:
    results["requests"] = {"status": "error"}

# TAB 9: Council / Reports
print("\n=== TAB: Council ===")
s, d = api("/api/council/latest")
if s == 200:
    print(f"  /api/council/latest: present ({type(d).__name__})")
    results["council"] = {"status": "ok"}
else:
    results["council"] = {"status": "error"}

# Widget issues endpoint
print("\n=== Widget Issues ===")
s, d = api("/api/widget-issues")
if s == 200:
    print(f"  /api/widget-issues: {len(d)} open issues")
    for i in d:
        print(f"    id={i.get('id','?')} tab={i.get('tab','?')} widget={i.get('widget','?')} status={i.get('status','?')} desc={i.get('description','?')}")
    results["widget_issues"] = {"status": "ok", "open_count": len(d)}
else:
    results["widget_issues"] = {"status": "error"}

print("\n" + "="*60)
print("VERIFICATION SUMMARY")
print("="*60)
all_ok = all(r.get("status") == "ok" for r in results.values())
print(f"All endpoints OK: {all_ok}")
print(f"Total endpoints checked: {len(results)}")
for name, r in results.items():
    print(f"  {name}: {r['status']} { {k:v for k,v in r.items() if k!='status'} }")

# Output full results JSON
with open("/home/kelly/multihedge/.qa_api_results.json", "w") as f:
    json.dump(results, f, indent=2)
print("\nFull results written to .qa_api_results.json")