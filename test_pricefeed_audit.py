import unittest, tempfile, time, sqlite3
from pathlib import Path
from unittest.mock import patch
import pricefeed as pf

SOL='So11111111111111111111111111111111111111112'
OTHER='JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN'
class PriceAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.p=patch.object(pf,'DB_PATH',Path(self.tmp.name)/'price.db'); self.p.start()
    def tearDown(self): self.p.stop(); self.tmp.cleanup()
    def test_jup_has_verified_exchange_fallback(self):
        self.assertEqual(pf.BINANCE_SYMBOLS.get('JUP'),'JUPUSDT')
    def test_v3_exact_mint_response(self):
        with patch.object(pf,'_get',return_value={OTHER:{'usdPrice':99},SOL:{'usdPrice':100}}):
            self.assertEqual(pf._jupiter_mint(SOL),100)
        self.assertTrue(pf._JUP_PRICE.endswith('/v3'))
    def test_other_mint_cannot_supply_price(self):
        with patch.object(pf,'_get',return_value={OTHER:{'price':99}}):
            self.assertIsNone(pf._jupiter_mint(SOL))
    def test_cache_is_keyed_by_mint_not_ambiguous_symbol(self):
        with patch.object(pf,'_jupiter_mint',side_effect=[100,1]):
            self.assertEqual(pf.live_price(SOL,'SAME'),100)
            self.assertEqual(pf.live_price(OTHER,'SAME'),1)
    def test_old_cache_not_relabelled_live(self):
        pf._set_cache(SOL,100)
        with sqlite3.connect(pf.DB_PATH) as c: c.execute('UPDATE price_cache SET ts=?',(time.time()-3600,))
        with patch.object(pf,'_jupiter_mint',return_value=None): self.assertIsNone(pf.live_price(SOL))
    def test_unknown_mint_cannot_fallback_to_unrelated_symbol(self):
        with patch.object(pf,'_jupiter_mint',return_value=None), patch.object(pf,'_coin_gecko',return_value=100),patch.object(pf,'_binance',return_value=100):
            self.assertIsNone(pf.live_price(OTHER,'SOL'))

if __name__=='__main__':unittest.main()
