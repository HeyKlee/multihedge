#!/usr/bin/env python3
"""
Fetches OpenRouter data for Rainmeter/Linux widget:
- Top 3 models <= $0.10
- Top 3 free models
- Personal token usage per model (last 24h)
- Personal spend last 24h
Writes JSON to /tmp/openrouter_widget.json (or custom path)
Requires OPENROUTER_API_KEY env var.
"""
import os
import json
import time
from datetime import datetime, timedelta
import httpx

OPENROUTER_BASE = "https://openrouter.ai/api/v1"
CACHE_FILE = os.environ.get("OR_WIDGET_CACHE", "/tmp/openrouter_widget.json")

def _headers():
    key = os.getenv("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY not set")
    return {"Authorization": f"Bearer {key}"}

def fetch_models():
    """Get all models with pricing."""
    resp = httpx.get(f"{OPENROUTER_BASE}/models", headers=_headers(), timeout=30)
    resp.raise_for_status()
    models = []
    for m in resp.json()["data"]:
        pricing = m.get("pricing", {})
        prompt = float(pricing.get("prompt", "0") or 0)
        completion = float(pricing.get("completion", "0") or 0)
        avg_price = (prompt + completion) / 2
        models.append({
            "id": m["id"],
            "name": m["name"],
            "prompt": prompt,
            "completion": completion,
            "avg_price": avg_price,
            "context_length": m.get("context_length", 0),
        })
    return models

def top_models(models, max_price=0.10, limit=3):
    """Return top models sorted by avg_price ascending, under max_price."""
    filtered = [m for m in models if m["avg_price"] <= max_price and m["avg_price"] > 0]
    filtered.sort(key=lambda x: x["avg_price"])
    return filtered[:limit]

def free_models(models, limit=3):
    """Return truly free models (prompt=0 and completion=0)."""
    free = [m for m in models if m["prompt"] == 0 and m["completion"] == 0]
    free.sort(key=lambda x: x["context_length"], reverse=True)
    return free[:limit]

def personal_usage():
    """Fetch usage analytics grouped by model for last 24h."""
    try:
        now = datetime.utcnow()
        start = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        end = now.replace(hour=0, minute=0, second=0, microsecond=0)
        payload = {
            "dimensions": ["model"],
            "granularity": "day",
            "metrics": ["total_usage", "tokens_total"],
            "time_range": {"start": start.isoformat() + "Z", "end": end.isoformat() + "Z"},
            "limit": 1000
        }
        resp = httpx.post(f"{OPENROUTER_BASE}/analytics/query",
                          headers=_headers(),
                          json=payload,
                          timeout=30)
        if resp.status_code != 200:
            return {}
        data = resp.json()
        usage = {}
        for row in data.get("data", []):
            model = row.get("model")
            if not model:
                continue
            usage[model] = {
                "tokens_total": row.get("tokens_total", 0),
                "total_usage": row.get("total_usage", 0.0),
            }
        return usage
    except Exception:
        return {}

def build_output():
    models = fetch_models()
    top = top_models(models)
    free = free_models(models)
    usage = personal_usage()
    total_spend = sum(v.get("total_usage", 0) for v in usage.values())
    return {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "top_models": [
            {
                "id": m["id"],
                "name": m["name"],
                "avg_price": m["avg_price"],
                "context_length": m["context_length"]
            } for m in top
        ],
        "free_models": [
            {
                "id": m["id"],
                "name": m["name"],
                "context_length": m["context_length"]
            } for m in free
        ],
        "personal_usage_per_model": usage,
        "personal_spend_last_24h": total_spend
    }

def main():
    output = build_output()
    with open(CACHE_FILE, "w") as f:
        json.dump(output, f, indent=2)
    print(f"Wrote {CACHE_FILE}")

if __name__ == "__main__":
    import sys
    if "--test" in sys.argv:
        # Generate sample data without API calls
        sample = {
            "timestamp": "2025-08-24T12:34:56Z",
            "top_models": [
                {"id": "openai/gpt-4o", "name": "GPT-4o", "avg_price": 0.005, "context_length": 8192},
                {"id": "anthropic/claude-3-opus", "name": "Claude 3 Opus", "avg_price": 0.015, "context_length": 8192},
                {"id": "meta-llama/llama-3-70b", "name": "Llama 3 70B", "avg_price": 0.02, "context_length": 8192},
            ],
            "free_models": [
                {"id": "qwen/qwen-2.5-72b", "name": "Qwen 2.5 72B", "context_length": 32768},
                {"id": "mistralai/mistral-large-latest", "name": "Mistral Large", "context_length": 8192},
                {"id": "cohere/command-r-plus", "name": "Command R+", "context_length": 8192},
            ],
            "personal_usage_per_model": {
                "openai/gpt-4o": {"tokens_total": 1000000, "total_usage": 12.34},
                "anthropic/claude-3-opus": {"tokens_total": 800000, "total_usage": 9.87},
                "meta-llama/llama-3-70b": {"tokens_total": 500000, "total_usage": 6.55},
            },
            "personal_spend_last_24h": 29.76,
        }
        with open(CACHE_FILE, "w") as f:
            json.dump(sample, f, indent=2)
        print(f"Test data written to {CACHE_FILE}")
    else:
        main()