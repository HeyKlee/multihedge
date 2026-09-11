"""
MultiHedge WEB DASHBOARD — tailnet-local read-only UI.

Serves a small HTML dashboard + JSON API over HTTP from the multihedge.db SQLite
store. Stdlib-only (http.server + sqlite3 + json) so it runs on NOVA (Windows)
and XORA (Linux/Docker) without extra dependencies.

Tailnet-local by design: binds 0.0.0.0 so it's reachable on the tailnet/LAN,
but it is read-only (never writes a wallet) and is NOT exposed publicly.

Ports:
  default 9052 (distinct from AutoHedge dashboard :9050).

Endpoints:
  GET /            -> HTML dashboard
  GET /api/status  -> JSON { paper equity per coin, open positions, live gate }
  GET /api/history -> JSON recent closed trades
"""

import json
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DB_PATH = Path(__file__).parent / "multihedge.db"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 9052


def _connect():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def _read_db():
    con = _connect()
    accounts = [dict(r) for r in con.execute(
        "SELECT * FROM mh_accounts ORDER BY coin").fetchall()]
    positions = [dict(r) for r in con.execute(
        "SELECT * FROM mh_positions ORDER BY open_ts DESC").fetchall()]
    trades = [dict(r) for r in con.execute(
        "SELECT * FROM mh_trades ORDER BY id DESC LIMIT 30").fetchall()]
    # grid trader (own tables, fully isolated from paper wallets)
    try:
        gw = con.execute("SELECT * FROM grid_wallet WHERE id=1").fetchone()
        grid_wallet = dict(gw) if gw else None
    except sqlite3.OperationalError:
        grid_wallet = None
    try:
        gs = con.execute("SELECT * FROM grid_state WHERE id=1").fetchone()
        grid_state = dict(gs) if gs else None
    except sqlite3.OperationalError:
        grid_state = None
    try:
        gcycles = con.execute(
            "SELECT COUNT(*) c, COALESCE(SUM(realized_usd),0) s FROM grid_trades "
            "WHERE side='SELL' AND cycle_id IS NOT NULL").fetchone()
        grid_cycles = {"completed": gcycles["c"], "realized_usd": round(gcycles["s"], 4)}
    except sqlite3.OperationalError:
        grid_cycles = None
    # live gate per known coin (no coin config here, derive from accounts)
    gates = {}
    for a in accounts:
        coin = a["coin"]
        t = [r for r in reversed(trades) if r["coin"] == coin]
        wins = sum(1 for x in t if x["realized_pct"] > 0)
        losses = len(t) - wins
        n = len(t)
        wr = wins / n if n else 0.0
        gates[coin] = {"n": n, "wins": wins, "losses": losses,
                       "win_rate": round(wr, 3),
                       "eligible": n >= 20 and wr >= 0.75}
    con.close()
    return accounts, positions, trades, gates, grid_wallet, grid_state, grid_cycles


def _status_json():
    accounts, positions, trades, gates, grid_wallet, grid_state, grid_cycles = _read_db()
    return {
        "accounts": accounts,
        "positions": positions,
        "recent_trades": trades,
        "live_gate": gates,
        "grid_wallet": grid_wallet,
        "grid_state": grid_state,
        "grid_cycles": grid_cycles,
    }


HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MultiHedge</title>
<style>
 body{font-family:system-ui;background:#0d1117;color:#e6edf3;margin:0;padding:24px}
 h1{font-size:20px} h2{font-size:16px;margin-top:24px;color:#8b949e}
 table{border-collapse:collapse;width:100%;margin-top:8px}
 th,td{text-align:left;padding:6px 10px;font-size:13px;border-bottom:1px solid #21262d}
 th{color:#8b949e;font-weight:600}
 .pm{font-family:monospace}.pos{color:#3fb950}.neg{color:#f85149}.gate{font-weight:700}
 .ok{color:#3fb950}.no{color:#f85149}
 .wrap{max-width:880px;margin:0 auto}
</style></head><body><div class="wrap">
<h1>MultiHedge &mdash; Multi-Coin Solana Paper Trading</h1>
<p style="color:#8b949e">Tailnet-local. Real Jupiter fills, <b>no wallet writes.</b> Data refreshes on reload.</p>
<h2>Paper accounts</h2>
<table><tr><th>Coin</th><th>Equity (USD)</th><th>Closed</th><th>Win rate</th><th>Live gate</th></tr>
%(accounts_rows)s</table>
<h2>Grid trader (SOL)</h2>
<table><tr><th>Cash (USD)</th><th>SOL</th><th>Peak equity</th><th>Paused</th><th>Cycles</th><th>Realized (USD)</th></tr>
%(grid_rows)s</table>
<h2>Open positions</h2>
<table><tr><th>Coin</th><th>Side</th><th>Setup</th><th>Entry</th><th>Qty</th><th>Peak</th><th>Trail</th></tr>
%(positions_rows)s</table>
<h2>Live gate (real wallet stays untouched until 3:1/7:1 met)</h2>
<table><tr><th>Coin</th><th>Trades</th><th>Win-rate</th><th>Eligible</th></tr>
%(gate_rows)s</table>
<h2>Recent closed trades</h2>
<table><tr><th>Coin</th><th>Side</th><th>Setup</th><th>Entry</th><th>Exit</th><th>PnL %</th><th>Reason</th></tr>
%(trade_rows)s</table>
</div></body></html>"""


def _render():
    accounts, positions, trades, gates, grid_wallet, grid_state, grid_cycles = _read_db()
    acc_rows = "".join(
        "<tr><td>{c}</td><td class=pm>{e:.2f}</td><td>{n}</td>"
        "<td class=pm>{wr:.1%}</td>"
        "<td class='gate {ok}'>{el}</td></tr>".format(
            c=a["coin"], e=a["equity_usd"],
            n=gates.get(a["coin"], {}).get("n", 0),
            wr=gates.get(a["coin"], {}).get("win_rate", 0.0),
            el="READY" if gates.get(a["coin"], {}).get("eligible") else "waiting",
            ok="ok" if gates.get(a["coin"], {}).get("eligible") else "no")
        for a in accounts)
    pos_rows = "".join(
        "<tr><td>{c}</td><td>{s}</td><td>{u}</td><td class=pm>{e}</td>"
        "<td class=pm>{q}</td><td class=pm>{p}</td><td>{t}</td></tr>".format(
            c=p["coin"], s=p["side"], u=p["setup"], e=p["entry_px"],
            q=round(p["qty"], 6), p=p.get("peak_px"), t="yes" if p.get("trail_armed") else "")
        for p in positions)
    gate_rows = "".join(
        "<tr><td>{c}</td><td>{n}</td><td class=pm>{wr:.1%}</td>"
        "<td class='gate {ok}'>{el}</td></tr>".format(
            c=k, n=v["n"], wr=v["win_rate"],
            el="YES" if v["eligible"] else "no", ok="ok" if v["eligible"] else "no")
        for k, v in gates.items())
    trade_rows = "".join(
        "<tr><td>{c}</td><td>{s}</td><td>{u}</td><td class=pm>{e}</td>"
        "<td class=pm>{x}</td><td class='pm {pn}'>{p:.1%}</td><td>{r}</td></tr>".format(
            c=t["coin"], s=t["side"], u=t["setup"], e=t["entry_px"], x=t["exit_px"],
            p=t["realized_pct"], r=t["exit_reason"],
            pn="pos" if t["realized_pct"] > 0 else "neg")
        for t in trades)
    if grid_wallet:
        grid_rows = (
            "<tr><td class=pm>{c:.2f}</td><td class=pm>{q:.4f}</td>"
            "<td class=pm>{p:.2f}</td><td class='gate {ok}'>{pa}</td>"
            "<td>{n}</td><td class=pm>{r:.4f}</td></tr>".format(
                c=grid_wallet["cash_usd"], q=grid_wallet["sol_qty"],
                p=grid_wallet["peak_equity"], pa="PAUSED" if grid_wallet["paused"] else "ok",
                ok="no" if grid_wallet["paused"] else "ok",
                n=(grid_cycles or {}).get("completed", 0),
                r=(grid_cycles or {}).get("realized_usd", 0.0)))
    else:
        grid_rows = "<tr><td colspan=6>grid wallet not seeded yet</td></tr>"
    return (HTML
            .replace("%(accounts_rows)s", acc_rows or "<tr><td colspan=5>none yet</td></tr>")
            .replace("%(positions_rows)s", pos_rows or "<tr><td colspan=7>no open positions</td></tr>")
            .replace("%(gate_rows)s", gate_rows or "<tr><td colspan=4>no coins</td></tr>")
            .replace("%(grid_rows)s", grid_rows)
            .replace("%(trade_rows)s", trade_rows or "<tr><td colspan=7>no trades yet</td></tr>"))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # quiet

    def _send(self, code, body, ctype="text/html"):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/status":
            self._send(200, json.dumps(_status_json(), default=str), "application/json")
        elif self.path == "/api/history":
            _, _, trades, _ = _read_db()
            self._send(200, json.dumps(trades, default=str), "application/json")
        else:
            self._send(200, _render())


def main():
    if not DB_PATH.exists():
        print(f"[multihedge-dash] db not found at {DB_PATH} (run the loom first).", file=sys.stderr)
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"[multihedge-dash] serving on 0.0.0.0:{PORT} (tailnet-local). Ctrl-C to stop.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("stopped.")


if __name__ == "__main__":
    main()