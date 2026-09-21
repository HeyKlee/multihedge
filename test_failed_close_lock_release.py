"""A failed paper close must release locks even with retained tracebacks."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import paper
import mh_reasoner


class FailedCloseLockReleaseTests(unittest.TestCase):
    def check_close(self, module):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / 'ledger.db'
            with patch.object(paper, 'DB_PATH', db), patch.object(mh_reasoner, 'DB_PATH', db):
                paper.ensure_account(paper.TRADER_SCALPER, 24)
                paper.ensure_account(paper.TRADER_REASONER, 24)
                mh_reasoner._connect().close()
                if module is paper:
                    paper.open_position('TEST', 100, 0.1, 'LONG', 'test')
                    pos = paper.open_positions()[0]
                    table = 'mh_positions'
                    call = lambda: paper.close_position(pos, 110, 'take_profit')
                else:
                    with sqlite3.connect(db) as con:
                        con.execute("INSERT INTO mh_reasoner_positions(symbol,side,qty,entry,ts,peak) VALUES('TEST','LONG',0.1,100,1,100)")
                    pos = {'id': 1}
                    table = 'mh_reasoner_positions'
                    call = lambda: mh_reasoner._close('TEST', pos, 110, 'take_profit')
                reader = sqlite3.connect(db)
                reader.execute('BEGIN')
                reader.execute('SELECT * FROM mh_accounts').fetchall()
                writer = sqlite3.connect(db, timeout=0.03)
                writer.row_factory = sqlite3.Row
                retained = None
                try:
                    with patch.object(module, '_connect', return_value=writer):
                        try:
                            call()
                        except sqlite3.OperationalError as exc:
                            retained = exc  # Keep traceback alive, as loggers can.
                    self.assertIsNotNone(retained, 'commit must fail while reader holds lock')
                    self.assertIn('locked', str(retained))
                    probe = sqlite3.connect(db, timeout=0.03)
                    try:
                        self.assertEqual(probe.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0], 1)
                        self.assertEqual(probe.execute('SELECT COUNT(*) FROM mh_trades').fetchone()[0], 0)
                        self.assertEqual(probe.execute('SELECT SUM(equity_usd) FROM mh_accounts').fetchone()[0], 48)
                    finally:
                        probe.close()
                finally:
                    writer.close()
                    reader.close()
                # A retry closes once, after the competing read transaction ends.
                with patch.object(paper.strat, 'record_trade'):
                    call()
                    call()
                with sqlite3.connect(db) as probe:
                    self.assertEqual(probe.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0], 0)
                    self.assertEqual(probe.execute('SELECT COUNT(*) FROM mh_trades').fetchone()[0], 1)
                    self.assertEqual(probe.execute('SELECT SUM(equity_usd) FROM mh_accounts').fetchone()[0], 49)

    def test_scalper_failed_commit_releases_lock_and_preserves_retry(self):
        self.check_close(paper)

    def test_reasoner_failed_commit_releases_lock_and_preserves_retry(self):
        self.check_close(mh_reasoner)
