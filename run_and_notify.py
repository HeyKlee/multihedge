#!/usr/bin/env python3
"""Wrapper that
1. runs ops/5year_backtest.py
2. posts a short summary to a Discord webhook

The webhook URL must be set in the environment variable `DISCORD_WEBHOOK`.
If it is not set the script logs a warning but still copies the files.
"""
import os
import sys
import pathlib
import subprocess
import datetime

SCRIPT_DIR = pathlib.Path(__file__).parent

# 1. Run the backtester
res = subprocess.run(
    ["python3", "ops/5year_backtest.py"],
    capture_output=True,
    text=True,
    cwd=SCRIPT_DIR,
)
print(res.stdout)
if res.returncode != 0:
    print("BACKTESTER ERROR", file=sys.stderr)
    print(res.stderr, file=sys.stderr)
    sys.exit(res.returncode)

# 2. Build message
out_dir = SCRIPT_DIR / "backtest-results" / "5year"
coins = [d.name for d in out_dir.iterdir() if d.is_dir()] if out_dir.exists() else []
msg = f"✅ 5-year back-test complete — {len(coins)} coins processed"

# 3. Send to Discord if webhook is configured
webhook = os.getenv("DISCORD_WEBHOOK")
if webhook:
    try:
        import httpx
        r = httpx.post(webhook, json={"content": msg}, timeout=5)
        r.raise_for_status()
    except Exception as exc:
        print(f"WARNING: Discord webhook failed: {exc}", file=sys.stderr)
else:
    print("DISCORD_WEBHOOK not set – no DM sent")

# Record finish time in a log file
log_path = SCRIPT_DIR / "backtester.log"
with log_path.open("a", encoding="utf-8") as f:
    f.write(f"{datetime.datetime.utcnow().isoformat()} - {msg}\n")