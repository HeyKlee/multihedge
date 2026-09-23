#!/usr/bin/env python3
"""Verify all dashboard API endpoints for widget QA task t_0a13ffc7."""
import json, sys, urllib.request

BASE = "http://127.0.0.1:9053"

def api(path):
    try:
        with urllib.request.urlopen(BASE + path, timeout=10) as r:
            return r.status, json.loads(r.read())
    except Exception as e:
        return 0, {"error": str(e)}

print("=== /api/widget-issues ===")
status, data = api("/api/widget-issues")
print(f"  HTTP {status}, issues: {len(data)}")
for i in data:
    print(f"  idd={i.get('id','?')} tab={i.get('tab','?')} widget={i.get('widget','?')} status={i.get('status','?')}")

print("\n=== /api/reasoner - params key ===")
status, data = api("/api/reasoner")
if status == 200:
    print(f"  params: {json.dumps(data.get('params','MISSING'), indent=2)}")
else:
    print(f"  ERROR: {data}")

print("\n=== /api/livegate ===")
status, data = api("/api/livegate")
print(f"  HTTP {status}, keys: {sorted(data.keys())}")

print("\n=== /api/summary ===")
status, data = api("/api/summary")
if status == 200:
    print(f"  active_live={data.get('active_live','?')} active_paper={data.get('active_paper','?')} trading_coins={data.get('trading_coins','?')}")

print("\n=== /api/positions ===")
status, data = api("/api/positions")
print(f"  HTTP {status}, positions count: {len(data) if isinstance(data,list) else 'N/A'}")

print("\n=== /api/grid ===")
status, data = api("/api/grid")
print(f"  HTTP {status}, entries: {len(data) if isinstance(data,list) else 'N/A'}")

print("\n=== /api/whales ===")
status, data = api("/api/whales")
print(f"  HTTP {status}, entries: {len(data) if isinstance(data,list) else 'N/A'}")

print("\n=== /api/memecoin ===")
status, data = api("/api/memecoin")
print(f"  HTTP {status}, entries: {len(data) if isinstance(data,list) else 'N/A'}")

print("\n=== /api/trades ===")
status, data = api("/api/trades?limit=5")
if status == 200:
    count = len(data)
    print(f"  HTTP {status}, recent trades: {count}")
    if count > 0:
        print(f"  sample: {json.dumps(data[0] if isinstance(data,list) else data, indent=2)[:300]}")

print("\n=== /api/market ===")
status, data = api("/api/market")
print(f"  HTTP {status}, sample: {json.dumps(data, indent=2)[:300] if status==200 else data}")

print("\n=== /api/survival (Xora-Survival tab) ===")
status, data = api("/api/survival")
if status == 200:
    print(f"  treasury_nzd={data.get('treasury_nzd','?')} at_risk={data.get('at_risk','?')} cycles={len(data.get('cycles',[]))} wallet={json.dumps(data.get('wallet','?'))[:200]}")

print("\n=== /api/exit_reasons ===")
status, data = api("/api/exit_reasons")
print(f"  HTTP {status}, reasons: {len(data) if isinstance(data,list) else 'N/A'}")