import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from run_sampled_replay_full import run


class RunnerRegressionTests(unittest.TestCase):
    def test_readonly_source_and_distinct_runs_preserve_existing_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source.db'
            with sqlite3.connect(source) as con:
                con.execute('CREATE TABLE mh_shadow_entry_observations (observed_ts REAL, mint TEXT, latest_usd REAL, return_5m_pct REAL, return_1h_pct REAL, buy_volume_5m_usd REAL, sell_volume_5m_usd REAL)')
                con.executemany('INSERT INTO mh_shadow_entry_observations VALUES (?,?,?,?,?,?,?)', [(10000, 'SYNTHETIC_TEST_ONLY', 1, 2, 3, 20000, 10000), (10060, 'SYNTHETIC_TEST_ONLY', 1.25, 2, 3, 20000, 10000)])
            before = hashlib.sha256(source.read_bytes()).hexdigest()
            for name in ('first', 'second'):
                report = run(source, root / name)
                self.assertEqual(report['closed_trades'], 1)
                self.assertTrue((root / name / 'trades.csv').exists())
            marker = root / 'first' / 'keep.txt'
            marker.write_text('preserve')
            with self.assertRaises(FileExistsError):
                run(source, root / 'first')
            self.assertEqual(marker.read_text(), 'preserve')
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)


if __name__ == '__main__':
    unittest.main()
