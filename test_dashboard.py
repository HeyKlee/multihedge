import unittest,tempfile,sqlite3,time
from pathlib import Path
from unittest.mock import patch
import paper,mh_dash as dash,grid_trader,pricefeed

class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.db=Path(self.tmp.name)/'dash.db'
        self.ps=[patch.object(m,'DB_PATH',self.db) for m in (paper,dash,grid_trader,pricefeed)]
        self.ps += [patch.object(pricefeed,'nzd_per_usd',return_value=1.67)]
        for p in self.ps:p.start()
        for t in paper.TRADERS:paper.ensure_account(t,24)
        with sqlite3.connect(self.db) as c:
            for table in ('mh_reasoner_positions','mh_whale_positions','mh_memecoin_positions'):
                c.execute(f'CREATE TABLE {table}(id INTEGER PRIMARY KEY,symbol TEXT,side TEXT,entry REAL,qty REAL,ts REAL)')
                c.execute(f'INSERT INTO {table} VALUES(1,?,?,?,?,?)',('SOL','LONG',100,.01,time.time()))
            c.execute('CREATE TABLE mh_pxhist(coin TEXT,ts REAL,px REAL)')
            c.execute('INSERT INTO mh_pxhist VALUES(?,?,?)',('SOL',time.time(),100))
        paper.open_position('SOL',100,.01,'LONG','mean_reversion')
    def tearDown(self):
        for p in reversed(self.ps):p.stop()
        self.tmp.cleanup()
    def test_all_position_counts(self):
        d=dash.api_summary();self.assertEqual(d['totals']['open_positions'],4)
        self.assertEqual(d['coins'][0]['open_positions'],4)
    def test_reasoner_wallet_is_only_reasoner(self):
        self.assertEqual([x['trader'] for x in dash._reasoner()['accounts']],['reasoner'])
    def test_market_no_provider_calls(self):
        with patch.object(pricefeed,'live_price',side_effect=AssertionError('network forbidden')) as live:
            data=dash.api_market()
        self.assertFalse(live.called);self.assertIn('stale',data[0])
    def test_edge_uses_wallet_percent_and_close_timestamp(self):
        with sqlite3.connect(self.db) as c:c.execute('INSERT INTO mh_trades(coin,setup,side,open_ts,close_ts,entry_px,exit_px,qty,realized_pct,realized_usd,exit_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?)',('SOL','mean_reversion','LONG',1,2,100,110,.1,.1,1,'take_profit'))
        self.assertAlmostEqual(dash.api_edge_curve()['scalper'][0]['cum'],100/24)
        self.assertEqual(dash.api_edge_curve()['scalper'][0]['ts'],2)
    def test_grid_exits_case_matches_storage(self):
        grid_trader.seed_wallet(grid_trader.cfg_grid())
        with sqlite3.connect(self.db) as c:c.execute("INSERT INTO grid_trades(ts,side,level_px,qty,usd,cycle_id,realized_usd) VALUES(1,'SELL',100,.1,10,1,1)")
        self.assertEqual(len(dash.api_exit_reason_series('grid')),1)
    def test_survival_exit_params_preserve_half_percent_precision(self):
        self.assertIn("SERIOUS.stop_loss_pct*100).toFixed(1)", dash._html())
    def test_survival_status_exposes_trailing_policy(self):
        status=dash._survival_risk_status(self.db)
        self.assertEqual(status['MEME']['trail_arm_pct'],.02)
        self.assertEqual(status['MEME']['trail_distance_pct'],.01)
        self.assertIn('<th>Trail arm</th><th>Trail distance</th>',dash._html())

    def test_dashboard_uses_grouped_sidebar_information_architecture(self):
        html=dash._html()
        self.assertIn('class="app-shell"',html)
        self.assertIn('class="sidebar"',html)
        self.assertIn('Command',html)
        self.assertIn('Traders',html)
        self.assertIn('Autonomous',html)
        for tab in ('overview','market','gate','strategies','reasoner','whales','memecoin','grid','survival'):
            self.assertIn(f'data-tab="{tab}"',html)

    def test_dashboard_defaults_to_overview_and_has_dynamic_page_context(self):
        html=dash._html()
        self.assertIn('data-tab="overview" class="active"',html)
        self.assertIn("let TAB='overview'",html)
        self.assertIn('id="pageTitle"',html)
        self.assertIn('id="pageSubtitle"',html)
        self.assertIn('const NAV_META=',html)

    def test_dashboard_preserves_visible_trading_provenance(self):
        html=dash._html()
        self.assertIn('PAPER SIMULATION',html)
        self.assertIn('real on-chain fills',html)
        self.assertIn("kind==='live'?'LIVE':'PAPER'",html)

if __name__=='__main__':unittest.main()
