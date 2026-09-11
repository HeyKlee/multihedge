import unittest, tempfile, sqlite3, time
from pathlib import Path
from unittest.mock import patch
import paper, strategy, mh_reasoner

class BackendAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / 'test.db'
        self.patches = [patch.object(m, 'DB_PATH', self.db) for m in (paper, strategy, mh_reasoner)]
        for p in self.patches: p.start()
        paper.ensure_account('scalper', 24)
        paper.ensure_account('reasoner', 24)
        with mh_reasoner._connect() as c:
            c.execute('CREATE TABLE mh_pxhist (coin TEXT, ts REAL, px REAL)')
    def tearDown(self):
        for p in reversed(self.patches): p.stop()
        self.tmp.cleanup()
    def test_scalper_pnl_is_token_quantity_times_price_change(self):
        paper.open_position('SOL', 100, .1, 'LONG', 'mean_reversion')
        pos=paper.open_positions()[0]
        result=paper.close_position(pos, 110, 'take_profit')
        self.assertAlmostEqual(result['usd'], 1)
        self.assertAlmostEqual(paper.equity('scalper'), 25)
    def test_close_replay_does_not_double_credit(self):
        paper.open_position('SOL', 100, .1, 'LONG', 'mean_reversion')
        pos=paper.open_positions()[0]
        paper.close_position(pos, 110, 'take_profit')
        before=paper.equity('scalper')
        paper.close_position(pos, 110, 'take_profit')
        self.assertEqual(paper.equity('scalper'),before)
    def test_reasoner_reads_coin_price_history(self):
        with sqlite3.connect(self.db) as c:
            c.execute('INSERT INTO mh_pxhist VALUES (?,?,?)',('SOL',time.time(),110))
        self.assertEqual(mh_reasoner._momentum_ok('SOL',100,'LONG'),(False,'down'))
    def test_reasoner_does_not_open_without_confirmation_data(self):
        self.assertFalse(mh_reasoner._momentum_ok('SOL',100,'LONG')[0])
    def test_reasoner_trailing_stop_stays_armed_after_retracement(self):
        pos={'side':'LONG','entry':100,'peak':101.4,'ts':time.time()}
        with patch.object(mh_reasoner,'TRAIL_ARM',.01), patch.object(mh_reasoner,'TRAIL_DIST',.005):
            self.assertEqual(mh_reasoner.closing_reason(100.7,pos),'trail_stop')
    def test_reasoner_replay_does_not_double_credit(self):
        with patch.object(mh_reasoner,'POSITION_FRACTION',.5): mh_reasoner._open('SOL','LONG',100)
        pos=mh_reasoner._rpos('SOL')
        mh_reasoner._close('SOL',pos,110,'take_profit')
        before=paper.equity('reasoner')
        mh_reasoner._close('SOL',pos,110,'take_profit')
        self.assertEqual(paper.equity('reasoner'),before)
    def test_reasoner_ignores_whale_provider(self):
        with sqlite3.connect(self.db) as c:
            c.execute('INSERT INTO mh_news_bias(symbol,direction,confidence,provider,ts) VALUES (?,?,?,?,?)',('SOL','UP',.95,'whale_tracker',time.time()))
        self.assertEqual(mh_reasoner._latest_bias('SOL')['direction'],'FLAT')
    def test_reasoner_rejects_dust(self):
        paper.set_equity('reasoner',.5)
        mh_reasoner._open('SOL','LONG',100)
        self.assertIsNone(mh_reasoner._rpos('SOL'))
    def test_reasoner_records_effective_parameter_application(self):
        mh_reasoner.refresh_params()
        with sqlite3.connect(self.db) as c:
            tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn('mh_parameter_application',tables)
            row=c.execute("SELECT settings_json FROM mh_parameter_application WHERE trader='reasoner'").fetchone()
        import json
        self.assertEqual(json.loads(row[0])['TAKE_PROFIT'],mh_reasoner.TAKE_PROFIT)
    def test_reasoner_paused_wallet_does_not_open(self):
        paper.reset_kill_switch('reasoner')
        with sqlite3.connect(self.db) as c:c.execute("UPDATE mh_risk_state SET paused=1 WHERE trader='reasoner'")
        mh_reasoner._open('SOL','LONG',100)
        self.assertIsNone(mh_reasoner._rpos('SOL'))
    def test_size_fraction_cannot_exceed_capital(self):
        self.assertEqual(paper.size_trade('scalper',100,fraction=2),0)

if __name__=='__main__': unittest.main()
