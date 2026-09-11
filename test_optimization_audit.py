import unittest,tempfile,sqlite3,json,time
from pathlib import Path
from unittest.mock import patch
import strategy

class OptimizationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.p=patch.object(strategy,'DB_PATH',Path(self.tmp.name)/'s.db');self.p.start()
    def tearDown(self):self.p.stop();self.tmp.cleanup()
    def test_exploration_cannot_starve_families_when_no_trades_fire(self):
        names=list(strategy.SETUP_FAMILIES)
        picks=[strategy.choose_setup('SOL') for _ in names]
        self.assertEqual(set(picks),set(names))
    def test_selection_has_persisted_application_evidence(self):
        chosen=strategy.choose_setup('SOL')
        with sqlite3.connect(strategy.DB_PATH) as c:
            tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn('mh_strategy_selection',tables)
            row=c.execute('SELECT setup,reason,selected_ts FROM mh_strategy_selection WHERE coin=?',('SOL',)).fetchone()
        self.assertEqual(row[0],chosen);self.assertTrue(row[1]);self.assertGreater(row[2],time.time()-10)
    def test_rsi_two_uses_last_two_changes(self):
        self.assertEqual(strategy._rs2([1,2,3,4,5,4,3]),0)
    def test_recorded_results_change_exploitation(self):
        for name in strategy.SETUP_FAMILIES:
            for _ in range(strategy.EXPLORE_TRADES):strategy.record_trade('SOL',name,-.01)
        for _ in range(15):strategy.record_trade('SOL','mean_reversion',.02)
        with patch.object(strategy.random,'random',return_value=1): self.assertEqual(strategy.choose_setup('SOL'),'mean_reversion')

if __name__=='__main__':unittest.main()
