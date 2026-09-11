import unittest,tempfile,time,sqlite3
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch
import track_whales as w, pump_monitor as pump, mh_whale_trader as trader, mh_memecoin_trader as meme, paper
MINT='So11111111111111111111111111111111111111112'
class WhaleTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.db=Path(self.tmp.name)/'w.db'
  self.ps=[patch.object(m,'DB_PATH',self.db,create=True) for m in (w,pump,trader,meme,paper)]
  for p in self.ps:p.start()
  for t in paper.TRADERS:paper.ensure_account(t,24)
  meme._ensure_tables();trader._connect().close()
 def tearDown(self):
  for p in reversed(self.ps):p.stop()
  self.tmp.cleanup()
 def test_only_tracked_owner_increases(self):
  tx={'result':{'meta':{'err':None,'preTokenBalances':[],'postTokenBalances':[{'mint':MINT,'owner':'someone_else','uiTokenAmount':{'uiAmount':100}}]}}}
  self.assertEqual(w._extract_buys(tx,'tracked'),[])
 def test_tracked_noncore_mint_preserved(self):
  tx={'result':{'meta':{'err':None,'preTokenBalances':[],'postTokenBalances':[{'mint':MINT,'owner':'tracked','uiTokenAmount':{'uiAmount':100}}]}}}
  self.assertEqual(w._extract_buys(tx,'tracked')[0][0],MINT)
 def test_failed_transaction_never_emits(self):
  tx={'result':{'meta':{'err':{'bad':1},'postTokenBalances':[{'mint':MINT,'owner':'tracked','uiTokenAmount':{'uiAmount':100}}]}}}
  self.assertEqual(w._extract_buys(tx,'tracked'),[])
 def test_authority_fields_must_be_present(self):
  val=SimpleNamespace(data=SimpleNamespace(parsed={'type':'mint','info':{}}),owner='TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA')
  with patch.object(pump.client,'get_account_info_json_parsed',return_value=SimpleNamespace(value=val)):
   self.assertFalse(pump._is_safe_token(MINT))
 def test_whale_close_dollars_reason_and_idempotency(self):
  with sqlite3.connect(self.db) as c:c.execute("INSERT INTO mh_whale_positions VALUES(1,'SOL','LONG',.1,100,?,7,100)",(time.time(),))
  pos={'id':1,'symbol':'SOL','side':'LONG','qty':.1,'entry':100,'ts':time.time(),'entry_signal':7}
  trader._close('SOL',pos,110,'take_profit');trader._close('SOL',pos,110,'take_profit')
  self.assertAlmostEqual(paper.equity('whale_trader'),25)
  with sqlite3.connect(self.db) as c:self.assertEqual(c.execute('SELECT exit_reason FROM mh_trades').fetchone()[0],'take_profit')
 def test_memecoin_close_dollars_reason_and_idempotency(self):
  with sqlite3.connect(self.db) as c:c.execute("INSERT INTO mh_memecoin_positions(id,symbol,side,qty,entry,ts,entry_signal,peak) VALUES(1,'TOKEN','LONG',10,.1,?,7,.1)",(time.time(),))
  pos={'id':1,'symbol':'TOKEN','side':'LONG','qty':10,'entry':.1,'ts':time.time(),'entry_signal':7}
  meme._close_position(pos,.15,'take_profit');meme._close_position(pos,.15,'take_profit')
  self.assertAlmostEqual(paper.equity('memecoin_trader'),24.5)
 def test_signal_mint_and_dedupe_survive_pipeline(self):
  import sqlite3
  con=sqlite3.connect(self.db)
  con.execute('CREATE TABLE mh_news_bias(id INTEGER PRIMARY KEY,symbol TEXT,direction TEXT,confidence REAL,rationale TEXT,headlines TEXT,provider TEXT,ts REAL)')
  con.commit();con.close()
  mint='JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN'
  w.push_buy_signal('name','wallet',mint,'sig')
  with sqlite3.connect(self.db) as c:
   rows=c.execute('SELECT mint,symbol FROM mh_whale_events').fetchall()
  self.assertEqual(len(rows),1);self.assertEqual(rows[0][0],mint);self.assertEqual(rows[0][1],'JUP')
 def test_closed_signal_cannot_reopen(self):
  with sqlite3.connect(self.db) as c:
   c.execute('CREATE TABLE IF NOT EXISTS mh_consumed_signals(trader TEXT,signal_id INTEGER,PRIMARY KEY(trader,signal_id))')
   c.execute("INSERT INTO mh_consumed_signals VALUES('whale_trader',7)")
  trader._open('SOL','LONG',100,{'id':7})
  with sqlite3.connect(self.db) as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM mh_whale_positions').fetchone()[0],0)
 def test_memecoin_trail_remains_armed(self):
  self.assertEqual(meme._eval_exit({'entry':100,'peak':140,'ts':time.time()},125),'trail_stop')
if __name__=='__main__':unittest.main()
