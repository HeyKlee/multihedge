"""Entry-cost regressions. All HTTP and databases are isolated test fixtures."""
import copy
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import solana_token_universe as stu
from test_solana_token_universe import CFG, NOW, MINT_A, MINT_B, token


def config():
    cfg = copy.deepcopy(CFG)
    cfg['live']['autonomous']['dynamic_universe']['maximum_friction_cost_pct'] = 0.50
    return cfg


def quote(inp, out, amount, received, impact='0.001'):
    return stu.FakeResponse(200, {'inputMint': inp, 'outputMint': out,
        'inAmount': str(amount), 'outAmount': str(received),
        'priceImpactPct': impact})


class EntryFrictionTests(unittest.TestCase):
    def test_discovery_blocks_expensive_coin_without_losing_cheap_coin(self):
        get = Mock(side_effect=[
            stu.FakeResponse(200, [token(MINT_A, 'CHILLHOUSE'), token(MINT_B, 'STONK')]),
            stu.FakeResponse(200, []), stu.FakeResponse(200, []),
            stu.FakeResponse(200, {'warnings': {MINT_A: [], MINT_B: []}}),
            quote(stu.USDC_MINT, MINT_A, 1000000, 1000000000),
            quote(MINT_A, stu.USDC_MINT, 1000000000, 981200),
            quote(stu.USDC_MINT, MINT_B, 1000000, 1000000000),
            quote(MINT_B, stu.USDC_MINT, 1000000000, 999700),
        ])
        rows = stu.discover_candidates(config(), api_key='test', now=NOW, get=get, include_rejected=True)
        eligible = [r['ticker'] for r in rows if r['entry_eligible']]
        self.assertEqual(eligible, ['STONK'])
        blocked = next(r for r in rows if r['ticker'] == 'CHILLHOUSE')
        self.assertEqual(blocked['friction']['status'], 'BLOCKED')
        self.assertAlmostEqual(blocked['friction']['round_trip_loss_pct'], 1.88)
        self.assertEqual(blocked['friction']['threshold_pct'], .50)

    def test_default_discovery_hides_rejected_entries_from_advisor(self):
        from unittest.mock import patch
        get = Mock(side_effect=[stu.FakeResponse(200,[token()]),stu.FakeResponse(200,[]),
            stu.FakeResponse(200,[]),stu.FakeResponse(200,{'warnings':{MINT_A:[]}})])
        with patch('entry_friction.check_entry_friction', return_value={
                'status':'BLOCKED','reason':'round_trip_cost_above_limit'}):
            self.assertEqual(stu.discover_candidates(config(),api_key='test',now=NOW,get=get), [])

    def test_invalid_quotes_and_configuration_fail_closed(self):
        from entry_friction import check_entry_friction
        for value in [float('nan'), float('inf'), True, -1, None, '0.5']:
            cfg = config()
            cfg['live']['autonomous']['dynamic_universe']['maximum_friction_cost_pct'] = value
            with self.subTest(config=value):
                self.assertEqual(check_entry_friction(MINT_A, cfg, api_key='test',
                    now=NOW, get=Mock())['status'], 'BLOCKED')
        for field, bad in [('outAmount', True), ('outAmount', '1.5'),
                           ('priceImpactPct', 'nan'), ('priceImpactPct', '-0.1'),
                           ('inputMint', MINT_B), ('inAmount', '2')]:
            buy = quote(stu.USDC_MINT, MINT_A, 1000000, 1000000000)
            buy._payload[field] = bad
            get = Mock(side_effect=[buy, quote(MINT_A, stu.USDC_MINT, 1000000000, 999000)])
            with self.subTest(field=field, bad=bad):
                self.assertEqual(check_entry_friction(MINT_A, config(), api_key='test',
                    now=NOW, get=get)['status'], 'BLOCKED')
        get = Mock(side_effect=[quote(stu.USDC_MINT, MINT_A, 1000000, 1000000000),
                               quote(MINT_A, stu.USDC_MINT, 1000000000, 1000010)])
        self.assertEqual(check_entry_friction(MINT_A, config(), api_key='test',
            now=NOW, get=get)['status'], 'BLOCKED')

    def test_cache_is_exact_size_fresh_and_never_revives_failed_old_quote(self):
        from entry_friction import check_entry_friction
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'evidence.db'
            get = Mock(side_effect=[quote(stu.USDC_MINT, MINT_A, 1000000, 1000000000),
                                   quote(MINT_A, stu.USDC_MINT, 1000000000, 999000)])
            first = check_entry_friction(MINT_A, config(), api_key='test', get=get, now=NOW, db_path=db)
            self.assertEqual(first['status'], 'ALLOWED')
            cached_get = Mock(side_effect=AssertionError('fresh cache must not call provider'))
            second = check_entry_friction(MINT_A, config(), api_key='test', get=cached_get,
                                          now=NOW+60, db_path=db)
            self.assertEqual(second['status'], 'ALLOWED')
            self.assertEqual(second['checked_at'], first['checked_at'])
            failed_get = Mock(return_value=stu.FakeResponse(403, {}))
            other_size = check_entry_friction(MINT_A, config(), api_key='test', amount_atomic=5000000,
                                             get=failed_get, now=NOW+60, db_path=db)
            self.assertEqual(other_size['status'], 'BLOCKED')
            stale = check_entry_friction(MINT_A, config(), api_key='test', get=failed_get,
                                        now=NOW+301, db_path=db)
            self.assertEqual(stale['status'], 'BLOCKED')
            after = check_entry_friction(MINT_A, config(), api_key='test', get=cached_get,
                                        now=NOW+302, db_path=db)
            self.assertEqual(after['status'], 'BLOCKED')

    def test_rejected_coin_cannot_open_but_its_stop_loss_still_closes(self):
        import dynamic_shadow_scalper as ds
        from test_dynamic_shadow_scalper import candidate
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'paper.db'
            row = candidate()
            self.assertEqual(ds.tick(db, [row], now=NOW)['opened'], 1)
            blocked = candidate(price=.0008)
            blocked['entry_eligible'] = False
            blocked['friction'] = {'status': 'BLOCKED', 'reason': 'round_trip_cost_above_limit'}
            result = ds.tick(db, [blocked], now=NOW+60, cfg=config())
            self.assertEqual(result['closed'], 1)
            self.assertEqual(result['reasons'], {'stop_loss': 1})
            # After cooldown, a bullish price snapshot is still not entry permission.
            blocked = candidate()
            blocked['entry_eligible'] = False
            result = ds.tick(db, [blocked], now=NOW+4000, cfg=config())
            self.assertEqual(result['opened'], 0)

    def test_compounded_entry_quotes_actual_size_outside_writer_lock(self):
        import dynamic_shadow_scalper as ds
        from test_dynamic_shadow_scalper import candidate
        from unittest.mock import patch
        from entry_friction import check_entry_friction
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'paper.db'
            row = candidate()
            row['friction'] = check_entry_friction(MINT_A, config(), api_key='test', now=NOW,
                get=Mock(side_effect=[quote(stu.USDC_MINT, MINT_A, 1000000, 1000000000),
                                     quote(MINT_A, stu.USDC_MINT, 1000000000, 999000)]))
            # End-to-end with a proven exact-mint cohort; no synthetic live evidence.
            ds.tick(db, [], now=NOW)
            with sqlite3.connect(db) as con:
                con.execute('UPDATE mh_accounts SET equity_usd=24 WHERE trader=?', (ds.SETUP,))
                for i in range(5):
                    con.execute('INSERT INTO mh_trades(coin,symbol,setup,side,open_ts,close_ts,entry_px,exit_px,qty,realized_pct,realized_usd,cost_usd,exit_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                                (MINT_A,'PEPE','dynamic_scalper','LONG',NOW-600-i,NOW-500-i,1.,1.1,1.,.1,.1,0.,'take_profit'))
            seen = []
            def checker(mint, amount):
                # This fails with database locked if network work holds the write txn.
                with sqlite3.connect(db, timeout=.05) as con:
                    con.execute('BEGIN IMMEDIATE')
                    con.rollback()
                seen.append((mint, amount))
                return {'status':'ALLOWED','mint':mint,'amount_atomic':amount,
                        'round_trip_loss_pct':.1,'threshold_pct':.5,'checked_at':NOW}
            result = ds.tick(db, [row], now=NOW, cfg=config(), entry_cost_checker=checker)
            self.assertEqual(result['opened'], 1)
            self.assertEqual(seen, [(MINT_A, int(24 * ds.POSITION_FRACTION * 1000000))])
            with sqlite3.connect(db) as con:
                self.assertAlmostEqual(con.execute('SELECT qty*entry_usd FROM mh_dynamic_scalp_positions').fetchone()[0],24*ds.POSITION_FRACTION)

    def test_signer_rechecks_known_buy_but_never_cost_blocks_sell(self):
        from live_signer_worker import enrich_dynamic_intent
        from test_execution_policy import valid_intent
        from unittest.mock import patch
        from execution_policy import PolicyDenied
        from dataclasses import replace
        cfg = config()
        intent = valid_intent()
        cfg['coins'] = [{'symbol':'JUP','mint':intent.output_mint,'decimals':6}]
        with patch('entry_friction.check_entry_friction', return_value={
                'status':'BLOCKED','reason':'round_trip_cost_above_limit'}) as check:
            with self.assertRaisesRegex(PolicyDenied, 'friction'):
                enrich_dynamic_intent(cfg, intent, api_key='test',rpc_url='unused',now=NOW)
            self.assertEqual(check.call_args.kwargs['amount_atomic'], intent.amount_atomic)
        sell = replace(intent, side='SELL', input_mint=intent.output_mint, output_mint=stu.USDC_MINT)
        with patch('entry_friction.check_entry_friction', side_effect=AssertionError('SELL must not check entry friction')):
            self.assertEqual(enrich_dynamic_intent(cfg,sell,api_key='test',rpc_url='unused',now=NOW), cfg)

    def test_proof_binds_mint_size_age_and_threshold(self):
        from entry_friction import proof_allows
        proof = {'status':'ALLOWED','mint':MINT_A,'amount_atomic':1000000,
                 'round_trip_loss_pct':.50,'threshold_pct':.50,'checked_at':NOW}
        self.assertTrue(proof_allows(proof,MINT_A,1000000,config(),NOW))
        for p in [{**proof,'mint':MINT_B},{**proof,'amount_atomic':5000000},
                  {**proof,'round_trip_loss_pct':.500001},
                  {**proof,'checked_at':NOW+1},{**proof,'checked_at':NOW-301}]:
            self.assertFalse(proof_allows(p,MINT_A,1000000,config(),NOW))

    def test_missing_proof_cannot_bypass_entry_gate(self):
        import dynamic_shadow_scalper as ds
        from test_dynamic_shadow_scalper import candidate
        with tempfile.TemporaryDirectory() as tmp:
            result = ds.tick(Path(tmp)/'paper.db', [candidate()], now=NOW, cfg=config())
            self.assertEqual(result['opened'], 0)


if __name__ == '__main__':
    unittest.main()
