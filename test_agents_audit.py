import unittest,tempfile,json,sqlite3,time
from pathlib import Path
from unittest.mock import patch
import agent_architecture as a

class AgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.p=patch.object(a,'DB_PATH',Path(self.tmp.name)/'agents.db');self.p.start()
    def tearDown(self):self.p.stop();self.tmp.cleanup()
    def test_fenced_json_is_preserved(self):
        self.assertEqual(json.loads(a._extract_json('```json\n{"confidence":0.8}\n```')),{'confidence':.8})
    def test_logging_bootstraps_schema(self):
        a.log_decision('analyst','SOL','test','{}','{}')
        with sqlite3.connect(a.DB_PATH) as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM agent_decision_log').fetchone()[0],1)
    def test_no_empty_model_response_claimed_as_success(self):
        with patch.object(a,'_openrouter_chat',return_value={'choices':[{'message':{'content':None}}]}):
            with self.assertRaises(ValueError):a.AnalystAgent().run('SOL',{}, {})
    def test_trader_rejects_invalid_model_scores(self):
        with self.assertRaises(ValueError):a.TraderAgent().run('SOL',{'bull_case_score':float('nan'),'bear_case_score':0,'confidence':1,'debate_outcome':'bull'},{'POSITION_FRACTION':.3})
    def test_pipeline_records_all_roles_and_advisory_mode(self):
        analyst={'bias_read':'bullish','news_signal':.5,'price_deviation_pct':.1,'recommendation':'buy'}
        research={'bull_case_score':.8,'bear_case_score':.2,'debate_outcome':'bull','confidence':.8}
        with patch.object(a,'_openrouter_chat',side_effect=[{'choices':[{'message':{'content':json.dumps(x)}}]} for x in (analyst,research)]):
            result=a.run_pipeline('SOL',{'deviation_pct':.1,'trend':'up'},{'bias_label':'UP','bias_score':.7},{'POSITION_FRACTION':.3})
        self.assertEqual(result['mode'],'advisory')
        with sqlite3.connect(a.DB_PATH) as c:roles={r[0] for r in c.execute('SELECT agent_role FROM agent_decision_log')}
        self.assertEqual(roles,{'analyst','researcher_bull','researcher_bear','trader','risk','portfolio'})

if __name__=='__main__':unittest.main()
