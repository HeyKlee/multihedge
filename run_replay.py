import tempfile, yaml, json, sqlite3, os
from pathlib import Path
from ops.sampled_price_replay import replay

import runtime_paths  # ATLAS Rule A: sole runtime path authority
with tempfile.TemporaryDirectory() as td:
    out = Path(td) / 'run'
    # ATLAS Rule A: was a CWD-relative host-only path; it breaks the moment the
    # database moves to its own mount. One resolver, so this cannot rot silently.
    src = runtime_paths.production_db()
    con = sqlite3.connect(f'file:{src}?mode=ro', uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA query_only=ON')
    rows = [dict(r) for r in con.execute('SELECT observed_ts, mint, latest_usd, return_5m_pct, return_1h_pct, buy_volume_5m_usd, sell_volume_5m_usd FROM mh_shadow_entry_observations ORDER BY observed_ts, mint')]
    con.close()
    cfg = {'coins': []}  # use paper defaults from dynamic_shadow_scalper
    result = replay(rows, out, cfg, per_side_bps=40.0)
    print(json.dumps(result, indent=2))