"""Entry integrity, without adopting unvalidated new trading thresholds."""
import tempfile
import unittest
from pathlib import Path
import dynamic_shadow_scalper as ds
from test_dynamic_shadow_scalper import candidate


class EntryIntegrityTests(unittest.TestCase):
    def test_nonfinite_market_values_block_new_risk(self):
        for key in ('return_5m_pct', 'return_1h_pct', 'buy_volume_5m_usd', 'sell_volume_5m_usd', 'latest_usd'):
            for value in (float('nan'), float('inf'), -float('inf')):
                with self.subTest(key=key, value=value), tempfile.TemporaryDirectory() as tmp:
                    row = candidate()
                    row['market'][key] = value
                    self.assertEqual(ds.tick(Path(tmp)/'test.db', [row], now=1000)['opened'], 0)

    def test_exit_does_not_reenter_same_mint_on_same_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp)/'test.db'
            ds.tick(db, [candidate()], now=1000)
            result = ds.tick(db, [candidate(price=.0008)], now=1060)
            self.assertEqual(result['closed'], 1)
            self.assertEqual(result['opened'], 0)

    def test_momentum_and_reversal_contracts_are_distinct(self):
        self.assertTrue(ds._entry_signal(candidate(change5=1)))
        # A negative 1h return is not enough by itself. Only the explicit
        # high-buy-pressure reversal branch may enter against the 1h trend.
        self.assertFalse(ds._entry_signal(candidate(change1h=-1, buy=12000, sell=10000)))
        self.assertTrue(ds._entry_signal(candidate(change5=2, change1h=-1, buy=20000, sell=10000)))
        self.assertFalse(ds._entry_signal(candidate(buy=10000, sell=10000)))


if __name__ == '__main__':
    unittest.main()
