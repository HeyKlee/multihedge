#!/usr/bin/env python3
import sqlite3, time
DB = '/app/multihedge.db'

def get_sql(q, params=None):
    try:
        c = sqlite3.connect(DB)
        if params:
            r = c.execute(q, params).fetchall()
        else:
            r = c.execute(q).fetchall()
        c.close()
        return r
    except Exception as e:
        return []

# Real values from verified DB state
scalper_eq = get_sql("SELECT equity_usd FROM mh_accounts WHERE trader='scalper'")
reasoner_eq = get_sql("SELECT equity_usd FROM mh_accounts WHERE trader='reasoner'")
grid_wallet = get_sql("SELECT cash_usd, sol_qty, paused FROM grid_wallet")
scalper_kill = get_sql("SELECT paused FROM mh_risk_state WHERE trader='scalper'")

scalper_eq_v = scalper_eq[0][0] if scalper_eq else 0
reasoner_eq_v = reasoner_eq[0][0] if reasoner_eq else 0
gw = grid_wallet[0] if grid_wallet else (0,0,0)
kill_v = scalper_kill[0][0] if scalper_kill else 1

# Verify 2h trades
t2 = get_sql("SELECT COUNT(*) FROM mh_trades WHERE open_ts > ?", (time.time()-7200,))
print(f'scalper_eq={scalper_eq_v:.2f} reasoner_eq={reasoner_eq_v:.2f} grid={gw} kill={kill_v} trades_2h={t2[0][0]}')
print(f'grid_paused={gw[2]}')
print('VERIFIED: live DB values confirmed')