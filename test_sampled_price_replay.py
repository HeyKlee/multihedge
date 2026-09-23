import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path


class SampledReplayTests(unittest.TestCase):
    def test_real_tick_opens_closes_and_accounts_for_costs(self):
        self.assertIsNotNone(importlib.util.find_spec('ops.sampled_price_replay'),
                             'isolated sampled replay has not been implemented')
        from ops.sampled_price_replay import replay
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = [dict(observed_ts=t, mint='SYNTHETIC_TEST_ONLY', latest_usd=p,
                         return_5m_pct=2., return_1h_pct=3.,
                         buy_volume_5m_usd=20000., sell_volume_5m_usd=10000.)
                    for t, p in [(10000., 1.), (10060., 1.25)]]
            result = replay(rows, root / 'run', {'coins': []})
            self.assertEqual(result['closed_trades'], 1)
            self.assertEqual(result['open_positions'], 0)
            self.assertAlmostEqual(result['gross_final_marked_equity_usd'], 10.25, places=2)
            self.assertAlmostEqual(result['modeled_paid_costs_usd'], .009, places=3)
            self.assertAlmostEqual(result['net_final_marked_equity_usd'], 10.241, places=2)
            self.assertTrue((root / 'run' / 'trades.csv').exists())

    def test_gaps_and_open_marks_are_explicit(self):
        from ops.sampled_price_replay import replay
        with tempfile.TemporaryDirectory() as tmp:
            rows = [dict(observed_ts=t, mint='SYNTHETIC_TEST_ONLY', latest_usd=p,
                         return_5m_pct=2., return_1h_pct=3.,
                         buy_volume_5m_usd=20000., sell_volume_5m_usd=10000.)
                    for t, p in [(10000., 1.), (10600., 1.25), (10660., 1.1)]]
            result = replay(rows, Path(tmp)/'run', {'coins': []})
            self.assertEqual(result['gap_flagged_closed_trades'], 1)
            self.assertEqual(result['open_positions'], 1)
            self.assertFalse(result['promotion_allowed'])
            self.assertGreater(result['modeled_paid_costs_usd'], .009)

    def test_invalid_input_and_existing_output_fail_closed(self):
        from ops.sampled_price_replay import replay
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)/'run'
            with self.assertRaises(ValueError):
                replay([], out, {})
            self.assertFalse(out.exists())
            row = dict(observed_ts=10000., mint='TEST', latest_usd=1.,
                       return_5m_pct=2., return_1h_pct=3.,
                       buy_volume_5m_usd=20000., sell_volume_5m_usd=10000.)
            with self.assertRaises(ValueError):
                replay([row, row], out, {})
            with self.assertRaises(ValueError):
                replay([dict(row, latest_usd=float('nan'))], out, {})
            out.mkdir()
            marker = out/'keep.txt'
            marker.write_text('preserve')
            with self.assertRaises(FileExistsError):
                replay([row], out, {})
            self.assertEqual(marker.read_text(), 'preserve')


if __name__ == '__main__':

    unittest.main()
