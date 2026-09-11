import unittest, tempfile, sqlite3
from pathlib import Path
from unittest.mock import patch
import grid_trader as grid

class GridAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.p=patch.object(grid,'DB_PATH',Path(self.tmp.name)/'grid.db'); self.p.start()
        self.cfg={'paper':{'quote_bps':40},'grid':{'grid_levels':8,'range_pct':.04}}
    def tearDown(self): self.p.stop(); self.tmp.cleanup()
    def test_closed_level_can_be_bought_again(self):
        grid.grid_tick(100,self.cfg)
        initial=grid.grid_tick(99,self.cfg)
        level=next(a['level'] for a in initial if a['action']=='buy')
        grid.grid_tick(101,self.cfg)
        acts=grid.grid_tick(99,self.cfg)
        self.assertTrue(any(a['action']=='buy' and a['level']==level for a in acts),acts)
    def test_sell_realized_matches_net_wallet_receipt(self):
        grid.grid_tick(100,self.cfg); grid.grid_tick(99,self.cfg)
        buys=grid.open_sells(); before=grid.wallet()['cash_usd']
        grid.grid_tick(101,self.cfg)
        with grid._connect() as c:
            sells=[dict(r) for r in c.execute("SELECT * FROM grid_trades WHERE side='SELL'")]
        gain=grid.wallet()['cash_usd']-before
        self.assertAlmostEqual(sum(s['usd'] for s in sells),gain)
        cost=sum(b['usd'] for b in buys if b['cycle_id'] in {s['cycle_id'] for s in sells})
        self.assertAlmostEqual(sum(s['realized_usd'] for s in sells),gain-cost)
    def test_optional_config_works(self):
        grid.grid_tick(100)
        self.assertIsInstance(grid.grid_tick(99),list)
    def test_flash_guard_uses_pre_reset_price(self):
        grid.grid_tick(100,self.cfg)
        with grid._connect() as c: c.execute('UPDATE grid_state SET last_reset_ts=0')
        acts=grid.grid_tick(90,self.cfg)
        self.assertTrue(any(a['action']=='flash_crash_skip' for a in acts),acts)

if __name__=='__main__': unittest.main()
