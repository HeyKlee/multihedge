import unittest,tempfile,time,sqlite3
from pathlib import Path
from unittest.mock import patch
import multihedge as mh
import paper,strategy,pricefeed,agent_architecture as agents

class MainAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.db=Path(self.tmp.name)/'main.db'
        self.ps=[patch.object(m,'DB_PATH',self.db) for m in (paper,strategy,pricefeed,agents)]
        for p in self.ps:p.start()
        self.cfg={'coins':[{'symbol':'SOL','mint':'So11111111111111111111111111111111111111112'}], 'paper':{'position_fraction':.3,'min_trade_value_usd':1},'strategies':[{'name':'mean_reversion','enabled':False}]}
        paper.ensure_account('scalper',24)
    def tearDown(self):
        for p in reversed(self.ps):p.stop()
        self.tmp.cleanup()
    def test_disabled_setups_never_selected(self):
        with patch.object(pricefeed,'live_price',return_value=100),patch.object(agents,'submit_pipeline',create=True),patch.object(agents.AnalystAgent,'run',return_value={}),patch.object(agents.ResearcherAgent,'run',return_value={}),patch.object(agents.TraderAgent,'run',return_value={}):
            result=mh.run_tick(self.cfg)
        self.assertFalse(any('setup' in r for r in result),result)
    def test_exits_happen_before_agent_submission_and_no_sync_llm(self):
        paper.open_position('SOL',100,.1,'LONG','mean_reversion')
        observed=[]
        def submit(*args,**kw):observed.append(len(paper.open_positions()))
        with patch.object(pricefeed,'live_price',return_value=103),patch.object(agents,'submit_pipeline',create=True,side_effect=submit),patch.object(agents.AnalystAgent,'run',side_effect=AssertionError('sync call')) as sync:
            result=mh.run_tick(self.cfg)
        self.assertFalse(sync.called)
        self.assertEqual(observed,[0])
        self.assertTrue(any(r['action']=='close' for r in result))

if __name__=='__main__':unittest.main()
