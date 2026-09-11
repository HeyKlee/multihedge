"""
MultiHedge NEWS REASONER (Layer 1, SLOW) — multi-coin.

Generalises AutoHedge's news_reasoner to EVERY coin in the universe. For each
configured coin it:
  1. pulls recent crypto news headlines (Bing News RSS, free, no key),
  2. asks an LLM (local or OpenRouter, failover) for a near-term UP/DOWN/FLAT
     bias + confidence + rationale,
  3. writes the verdict to the `mh_news_bias` table (per coin).

It does NOT trade. The fast multihedge loom + the slow reasoner trader read
this bias each tick to gate entries. Bias is considered stale after 15 min.

Usage:
  D:/AI/HERMES/autohedge/.venv/Scripts/python.exe mh_news.py [coin...]
     (defaults to all coins in config.yaml)
"""

import json
import os
import sqlite3
import time
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

import pricefeed
from config import COINS, CFG_PATH

CUR_DIR = Path(__file__).parent
DB_PATH = CUR_DIR / "multihedge.db"
LOG_PATH = CUR_DIR / "mh_news.log"

NEWS_MAX = 5           # top-N headlines per coin
BIAS_HOLD_SECS = 900   # bias valid 15 min; after that treat as FLAT
RETRIES = 2
TIMEOUT = 40
CONFIDENCE_FALLBACK = 0.5

# Provider order: local-friendly first, then OpenRouter (paid). Reads keys from .env.
ENV_KEYS = {
    "OPENROUTER": "OPENROUTER_API_KEY",
    "LOCAL": None,   # local model served via Open WebUI / Ollama-compatible endpoint
}


def _log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _load_env() -> dict:
    out = {}
    for p in (CUR_DIR / ".env", CUR_DIR.parent / "autohedge" / "dashboard" / ".env"):
        if p.exists():
            for line in p.read_text(errors="ignore").splitlines():
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _get(url, timeout=TIMEOUT, headers=None):
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=headers or {"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode(errors="ignore")
        except Exception:
            if attempt == 2:
                return None
            time.sleep(0.8 * (attempt + 1))
    return None


def _fetch_headlines(symbol):
    q = urllib.parse.quote(f"{symbol} crypto")
    url = f"https://www.bing.com/news/search?q={q}&format=rss"
    raw = _get(url)
    if not raw:
        return []
    import xml.etree.ElementTree as ET
    heads = []
    try:
        root = ET.fromstring(raw)
        for item in root.findall(".//item")[:NEWS_MAX]:
            t = item.findtext("title")
            if t:
                heads.append(t.strip())
    except Exception:
        pass
    return heads


def _build_messages(symbol, headlines, price):
    sys_prompt = (
        "You are a crypto news analyst. Given a coin symbol, its current USD "
        "price, and recent headlines, judge the near-term (hours) bias.\n"
        "Respond ONLY with a single JSON object, no markdown:\n"
        '{"direction": "UP"|"DOWN"|"FLAT", "confidence": 0.0-1.0, '
        '"rationale": "one sentence"}'
    )
    user = f"Symbol: {symbol}\nCurrent price: {price:.6f}\n"
    if headlines:
        user += "\nHeadlines:\n" + "\n".join(f"- {h}" for h in headlines)
    else:
        user += "\n(no headlines available)"
    return [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user},
    ]


def _parse_bias(content):
    content = (content or "").strip()
    if content.startswith("```"):
        content = content.strip("`")
        if content.lower().startswith("json"):
            content = content[4:].strip()
    s = content.find("{")
    e = content.rfind("}")
    if s >= 0 and e > s:
        content = content[s:e + 1]
    try:
        d = json.loads(content)
        direction = str(d.get("direction", "FLAT")).upper()
        if direction not in ("UP", "DOWN", "FLAT"):
            direction = "FLAT"
        conf = float(d.get("confidence", CONFIDENCE_FALLBACK))
        conf = max(0.0, min(1.0, conf))
        return {"direction": direction, "confidence": conf,
                "rationale": str(d.get("rationale", "")).strip()}
    except Exception:
        return None


def _call_provider(base_url, api_key, symbol, headlines, price):
    messages = _build_messages(symbol, headlines, price)
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": os.environ.get("MH_NEWS_MODEL", "deepseek/deepseek-v4-flash-0731"),
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": 200,
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions", data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            body = json.load(r)
        content = body["choices"][0]["message"]["content"]
        return _parse_bias(content), body
    except Exception as e:
        return None, {"error": str(e)}


def _ask_bias(symbol, headlines, price):
    env = _load_env()
    providers = []
    # Local first (Open WebUI / Ollama-compatible at 127.0.0.1:11434)
    providers.append({
        "name": "local", "base_url": "http://127.0.0.1:11434/v1", "key": None})
    # OpenRouter fallback
    if env.get("OPENROUTER_API_KEY"):
        providers.append({
            "name": "openrouter",
            "base_url": "https://openrouter.ai/api/v1", "key": env["OPENROUTER_API_KEY"]})
    for p in providers:
        res, body = _call_provider(p["base_url"], p["key"], symbol, headlines, price)
        if res:
            return res, p["name"]
    return {"direction": "FLAT", "confidence": 0.0, "rationale": "providers unavailable"}, "none"


def _ensure_table(con):
    con.execute("""
        CREATE TABLE IF NOT EXISTS mh_news_bias (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT, direction TEXT, confidence REAL,
            rationale TEXT, headlines TEXT, provider TEXT, ts REAL)
    """)


def _latest_price(symbol):
    mint = COINS.get(symbol, {}).get("mint")
    return pricefeed.live_price(mint, symbol) or 0.0


def analyze_symbol(symbol):
    price = _latest_price(symbol)
    heads = _fetch_headlines(symbol)
    bias, provider = _ask_bias(symbol, heads, price)
    bias = bias or {"direction": "FLAT", "confidence": CONFIDENCE_FALLBACK,
                    "rationale": "", "provider": provider}
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    _ensure_table(con)
    con.execute(
        "INSERT INTO mh_news_bias(symbol,direction,confidence,rationale,headlines,provider,ts) "
        "VALUES(?,?,?,?,?,?,?)",
        (symbol, bias.get("direction", "FLAT"), float(bias.get("confidence", 0.0)),
         bias.get("rationale", ""), json.dumps(heads), provider, time.time()))
    con.execute("DELETE FROM mh_news_bias WHERE symbol=? AND id NOT IN ("
                "SELECT id FROM mh_news_bias WHERE symbol=? ORDER BY id DESC LIMIT 12)",
                (symbol, symbol))
    con.commit()
    con.close()
    _log(f"[ok] {symbol} bias={bias.get('direction')} conf={float(bias.get('confidence',0)):.2f} "
         f"via {provider} [{len(heads)} headlines]")
    return bias


def latest_bias(symbol):
    """Fresh bias for a coin (FLAT if none or stale)."""
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    _ensure_table(con)
    row = con.execute("SELECT * FROM mh_news_bias WHERE symbol=? ORDER BY id DESC LIMIT 1",
                      (symbol,)).fetchone()
    con.close()
    if not row or time.time() - row["ts"] > BIAS_HOLD_SECS:
        return {"direction": "FLAT", "confidence": 0.0}
    return {"direction": row["direction"], "confidence": row["confidence"]}


def run_all():
    symbols = COINS.keys()
    out = {}
    for s in symbols:
        b = analyze_symbol(s)
        out[s] = {"direction": b.get("direction"), "confidence": b.get("confidence")}
    return out


if __name__ == "__main__":
    import sys
    targets = sys.argv[1:] or None
    if targets:
        for t in targets:
            if t in COINS:
                analyze_symbol(t)
    else:
        run_all()
    print(json.dumps(latest_bias(next(iter(COINS))) if COINS else {}, default=str))