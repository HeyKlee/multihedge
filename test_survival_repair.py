"""Regression tests for production incubator accounting and replay integrity."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import dynamic_shadow_scalper as ds
import parameter_autotuner as pa
from test_dynamic_shadow_scalper import candidate


class WalletRepairTests(unittest.TestCase):
    def test_wallet_uses_explicit_database_and_unproven_coins_stay_at_one_dollar(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'paper.db'
            candidates = []
            for i in range(20):
                row = candidate()
                row['mint'] = f'mint-{i}'
                candidates.append(row)
            # A global paper connection must not decide this wallet's capital.
            with patch('paper.ensure_account', side_effect=AssertionError('wrong database')):
                ds.tick(db, candidates, now=1000)
            with sqlite3.connect(db) as con:
                equity = con.execute('SELECT equity_usd FROM mh_accounts WHERE trader=?', (ds.SETUP,)).fetchone()[0]
                notionals = [r[0] for r in con.execute('SELECT qty*entry_usd FROM mh_dynamic_scalp_positions ORDER BY rowid')]
            self.assertAlmostEqual(equity, 10.0)
            self.assertEqual(len(notionals), 10)
            self.assertTrue(all(abs(n - ds.PAPER_NOTIONAL_USD) < 1e-9 for n in notionals))
            self.assertLessEqual(sum(notionals), equity)

    def test_missing_wallet_reconciles_history_but_unproven_new_coin_stays_at_one_dollar(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'paper.db'
            with ds._connect(db) as con:
                con.execute('INSERT INTO mh_trades(coin,symbol,setup,side,open_ts,close_ts,entry_px,exit_px,qty,realized_pct,realized_usd,exit_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)', ('old','OLD',ds.SETUP,'LONG',1,2,1,.5,4,-.5,-2,'stop_loss'))
            ds.tick(db, [candidate()], now=1000)
            with sqlite3.connect(db) as con:
                equity = con.execute('SELECT equity_usd FROM mh_accounts WHERE trader=?', (ds.SETUP,)).fetchone()[0]
                notional = con.execute('SELECT qty*entry_usd FROM mh_dynamic_scalp_positions').fetchone()[0]
            self.assertAlmostEqual(equity, 8)
            self.assertAlmostEqual(notional, ds.PAPER_NOTIONAL_USD)

    def test_only_a_coin_with_proven_profit_history_compounds_position_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'paper.db'
            with ds._connect(db) as con:
                con.execute('CREATE TABLE IF NOT EXISTS mh_accounts (trader TEXT PRIMARY KEY, equity_usd REAL NOT NULL, started_usd REAL NOT NULL)')
                con.execute('INSERT INTO mh_accounts VALUES(?,?,?)', (ds.SETUP, 20.0, ds.INITIAL_EQUITY_USD))
                for i in range(ds.MIN_COMPOUND_COIN_TRADES):
                    con.execute('INSERT INTO mh_trades(coin,symbol,setup,side,open_ts,close_ts,entry_px,exit_px,qty,realized_pct,realized_usd,exit_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                                (candidate()['mint'], 'PEPE', ds.SETUP, 'LONG', i, i + 1, 1.0, 1.1, 1.0, 0.1, 0.1, 'take_profit'))
            ds.tick(db, [candidate(price=1.0)], now=1000)
            with sqlite3.connect(db) as con:
                notional = con.execute('SELECT qty*entry_usd FROM mh_dynamic_scalp_positions').fetchone()[0]
            self.assertAlmostEqual(notional, 20.0 * ds.POSITION_FRACTION)

    def test_positive_one_off_coin_history_is_not_enough_to_compound(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'paper.db'
            with ds._connect(db) as con:
                con.execute('CREATE TABLE IF NOT EXISTS mh_accounts (trader TEXT PRIMARY KEY, equity_usd REAL NOT NULL, started_usd REAL NOT NULL)')
                con.execute('INSERT INTO mh_accounts VALUES(?,?,?)', (ds.SETUP, 20.0, ds.INITIAL_EQUITY_USD))
                con.execute('INSERT INTO mh_trades(coin,symbol,setup,side,open_ts,close_ts,entry_px,exit_px,qty,realized_pct,realized_usd,exit_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                            (candidate()['mint'], 'PEPE', ds.SETUP, 'LONG', 1, 2, 1.0, 1.5, 1.0, 0.5, 0.5, 'take_profit'))
            ds.tick(db, [candidate(price=1.0)], now=1000)
            with sqlite3.connect(db) as con:
                notional = con.execute('SELECT qty*entry_usd FROM mh_dynamic_scalp_positions').fetchone()[0]
            self.assertAlmostEqual(notional, ds.PAPER_NOTIONAL_USD)

    def test_established_coin_with_negative_history_is_still_eligible(self):
        """Quarantine disabled 2026-09-24: paper re-entry is not gated on past P&L."""
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'paper.db'
            losing = candidate(price=1.0)
            winning = candidate(price=1.0)
            winning['mint'] = 'winning-mint'
            with ds._connect(db) as con:
                for mint, pct in ((losing['mint'], -0.1), (winning['mint'], 0.1)):
                    for i in range(ds.MIN_COMPOUND_COIN_TRADES):
                        con.execute(
                            'INSERT INTO mh_trades(coin,symbol,setup,side,open_ts,close_ts,entry_px,exit_px,qty,realized_pct,realized_usd,exit_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                            (mint, mint, ds.SETUP, 'LONG', i, i + 10000, 1.0, 1.0 + pct,
                             1.0, pct, pct, 'take_profit' if pct > 0 else 'stop_loss'),
                        )
            result = ds.tick(db, [losing, winning], now=20000)
            self.assertEqual(result['opened'], 2)
            with sqlite3.connect(db) as con:
                opened = {row[0] for row in con.execute('SELECT mint FROM mh_dynamic_scalp_positions')}
            self.assertIn(losing['mint'], opened)
            self.assertIn(winning['mint'], opened)

    def test_malformed_established_coin_history_is_eligible(self):
        """Quarantine disabled 2026-09-24: malformed history no longer blocks paper entries."""
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'paper.db'
            row = candidate(price=1.0)
            with ds._connect(db) as con:
                con.execute('CREATE TABLE IF NOT EXISTS mh_accounts (trader TEXT PRIMARY KEY, equity_usd REAL NOT NULL, started_usd REAL NOT NULL)')
                con.execute('INSERT INTO mh_accounts VALUES(?,?,?)', (ds.SETUP, 10.0, ds.INITIAL_EQUITY_USD))
                for i in range(ds.MIN_COMPOUND_COIN_TRADES):
                    realized = 0.1 if i < ds.MIN_COMPOUND_COIN_TRADES - 1 else 0.2
                    con.execute(
                        'INSERT INTO mh_trades(coin,symbol,setup,side,open_ts,close_ts,entry_px,exit_px,qty,realized_pct,realized_usd,exit_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                        (row['mint'], 'PEPE', ds.SETUP, 'LONG', i, i + 1, 1.0, 1.1,
                         1.0, 0.1, realized, 'take_profit'),
                    )
            self.assertEqual(ds.tick(db, [row], now=1000)['opened'], 1)

    def test_stop_loss_cooldown_blocks_only_immediate_same_mint_reentry(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'paper.db'
            row = candidate(price=1.0)
            with ds._connect(db) as con:
                con.execute(
                    'INSERT INTO mh_trades(coin,symbol,setup,side,open_ts,close_ts,entry_px,exit_px,qty,realized_pct,realized_usd,exit_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                    (row['mint'], 'PEPE', ds.SETUP, 'LONG', 1, 1000, 1.0, 0.9,
                     1.0, -0.1, -0.1, 'stop_loss'),
                )
            self.assertEqual(ds.tick(db, [row], now=1001)['opened'], 0)
            self.assertEqual(
                ds.tick(db, [row], now=1000 + ds.STOP_LOSS_REENTRY_COOLDOWN_SECONDS)['opened'], 1,
            )


class PolicyEvidenceTests(unittest.TestCase):
    def test_new_trade_records_policy_but_legacy_position_is_not_relabelled(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'paper.db'
            ds.tick(db, [candidate()], now=1000)
            with sqlite3.connect(db) as con:
                tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertIn('mh_scalp_policy_evidence', tables)
                policy = con.execute('SELECT policy_json FROM mh_scalp_policy_evidence').fetchone()[0]
                self.assertIn('stop_loss_pct', policy)
                con.execute('DELETE FROM mh_scalp_policy_evidence')
            ds.tick(db, [candidate(price=.00101, change5=0)], now=1060)
            with sqlite3.connect(db) as con:
                self.assertEqual(con.execute('SELECT COUNT(*) FROM mh_scalp_policy_evidence').fetchone()[0], 0)


class CohortRepairTests(unittest.TestCase):
    def test_legacy_path_does_not_permanently_poison_new_policy_cohort(self):
        from test_parameter_autotuner import AutotunerTests, cfg, MINT
        fixture = AutotunerTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture._seed_mixed_paths(n=pa.MIN_SAMPLE_CLOSED + 1)
        with ds._connect(fixture.db) as con:
            rows = con.execute('SELECT mint,open_ts FROM mh_scalp_excursions ORDER BY open_ts').fetchall()
            # Retain one unlabelled legacy path; only newly observed policies qualify.
            con.executemany('INSERT INTO mh_scalp_policy_evidence VALUES(?,?,?,0)',
                            [(r[0], r[1], pa.policy_signature(pa._default_params('MEME'))) for r in rows[1:]])
            con.execute('UPDATE mh_scalp_price_samples SET price_usd=1 WHERE opened_ts=0')
        report = pa.maybe_tune(fixture.db, cfg())
        self.assertEqual(report['state'], 'TUNED', report)
        self.assertEqual(report['evaluation']['MEME']['excluded_policy'], 1)
        self.assertEqual(report['evaluation']['MEME']['policy_matched'], pa.MIN_SAMPLE_CLOSED)
        with sqlite3.connect(fixture.db) as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM mh_scalp_excursions').fetchone()[0], pa.MIN_SAMPLE_CLOSED + 1)


    def test_collection_tuning_and_paper_application_work_end_to_end(self):
        import live_inventory as li
        from test_dynamic_shadow_scalper import MINT
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'paper.db'
            # Seed 30 mixed-path excursions directly (same pattern as
            # AutotunerTests._seed_mixed_paths).
            con = sqlite3.connect(db)
            con.execute("""CREATE TABLE IF NOT EXISTS mh_scalp_excursions
                (mint TEXT,ticker TEXT,mode TEXT,entry_usd REAL,peak_usd REAL,
                 trough_usd REAL,open_ts REAL,close_ts REAL,hold_seconds REAL,
                 realized_pct REAL,exit_reason TEXT)""")
            con.execute("""CREATE TABLE IF NOT EXISTS mh_scalp_price_samples
                (mint TEXT,opened_ts REAL,sample_ts REAL,price_usd REAL,
                 PRIMARY KEY(mint,opened_ts,sample_ts))""")
            mode = li.mode_for_mint(MINT, {})
            for i in range(pa.MIN_SAMPLE_CLOSED):
                opened = 1000 + i * 1000.0
                if i < 17 or (22 <= i < 27):
                    prices = (1.0, 1.03)
                    close_ts = opened + 120.0
                else:
                    prices = (1.0, 0.985, 1.03)
                    close_ts = opened + 200.0
                con.execute(
                    "INSERT INTO mh_scalp_excursions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (MINT, MINT, mode, 1.0, max(prices), min(prices),
                     opened, close_ts, close_ts - opened,
                     prices[-1] / 1.0 - 1, "take_profit"))
                for j, price in enumerate(prices):
                    con.execute("INSERT INTO mh_scalp_price_samples VALUES(?,?,?,?)",
                                (MINT, opened, opened + j * 60.0, price))
            # Policy evidence for all 30 excursions.
            con.execute("""CREATE TABLE IF NOT EXISTS mh_scalp_policy_evidence
                (mint TEXT, opened_ts REAL, policy_json TEXT, mixed INTEGER,
                 UNIQUE(mint, opened_ts))""")
            sig = pa.policy_signature(li._default_params("MEME"))
            rows = con.execute("SELECT mint,open_ts FROM mh_scalp_excursions ORDER BY open_ts").fetchall()
            for mint, ots in rows:
                con.execute("INSERT INTO mh_scalp_policy_evidence VALUES(?,?,?,0)",
                            (mint, ots, sig))
            con.commit()
            con.close()
            result = pa.maybe_tune(db, {}, now=40000)
            self.assertEqual(result['state'], 'TUNED', result)
            paper = li.risk_params(MINT, {}, db_path=db, allow_tuned=True)
            live = li.risk_params(MINT, {}, db_path=db, allow_tuned=False)
            self.assertEqual(paper['trail_distance_pct'], 0.008)
            self.assertEqual(live['trail_distance_pct'], 0.003)
            # Paper application: tuned params (TP=0.03) close at the 1.085 tick.
            open_result = ds.tick(db, [candidate(price=1)], now=41000)
            self.assertEqual(open_result['opened'], 1)
            exit_result = ds.tick(db, [candidate(price=1.085, change5=0)], now=41060)
            self.assertEqual(exit_result['reasons'], {'take_profit': 1})
            self.assertEqual(exit_result['closed'], 1)

    def test_mixed_policy_observation_is_excluded_even_if_profitable(self):
        import live_inventory as li
        from test_dynamic_shadow_scalper import MINT
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'paper.db'
            ds.tick(db, [candidate(price=1)], now=1000)
            modified = {**li._default_params('MEME'), 'take_profit_pct': .15}
            li.set_risk_params_override(db, modified, source='test', sample_n=30)
            ds.tick(db, [candidate(price=1.25, change5=0)], now=1060)
            with sqlite3.connect(db) as con:
                self.assertEqual(con.execute('SELECT mixed FROM mh_scalp_policy_evidence').fetchone()[0], 1)
            rows = pa.load_excursions(db, 'MEME')
            self.assertEqual(pa.policy_cohort(db, rows, modified), [])
            self.assertEqual(pa.policy_cohort(db, rows, li._default_params('MEME')), [])


class ReviewerRepairTests(unittest.TestCase):
    def test_unlabelled_history_cannot_authorize_per_coin_changes(self):
        import test_coin_review as fixtures
        import mh_coin_review as cr
        fixture = fixtures.GateTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.seed_winning_paths()
        with sqlite3.connect(fixture.db) as con:
            con.execute('DELETE FROM mh_scalp_policy_evidence')
        result = cr.review_coin(fixture.db, fixtures.cfg(), fixture.good())
        self.assertFalse(result['applied'], result)
        self.assertEqual(result['reason'], 'insufficient_policy_history')

    def test_review_window_does_not_mix_another_strategy_into_incubator(self):
        import test_coin_review as fixtures
        import mh_coin_review as cr
        fixture = fixtures.CoinReviewBase()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.add_trade(fixtures.COIN_A, 'A', 1, .9, -.1, -.1, 'stop_loss')
        with sqlite3.connect(fixture.db) as con:
            con.execute("UPDATE mh_trades SET setup='reasoner'")
        self.assertEqual(cr.recent_trades(fixture.db, fixtures.COIN_A), [])


class ReplayRepairTests(unittest.TestCase):
    def test_replay_rejects_post_close_and_out_of_order_samples(self):
        params = pa._default_params('MEME')
        for samples in (
            [(0, 1), (120, 1.3)],
            [(0, 1), (50, 1.01), (40, .8)],
            [(0, 1), (30, float('inf'))],
        ):
            with self.subTest(samples=samples):
                row = {'entry_usd': 1, 'open_ts': 0, 'close_ts': 60,
                       'samples': [{'sample_ts': t, 'price_usd': p} for t, p in samples]}
                self.assertIsNone(pa._simulate_trade(row, params))

    def test_stop_gap_cannot_be_filled_at_an_unobserved_better_price(self):
        row = {'entry_usd': 1.0, 'open_ts': 0, 'close_ts': 60,
               'samples': [{'sample_ts': 0, 'price_usd': 1}, {'sample_ts': 60, 'price_usd': .8}]}
        params = {'take_profit_pct': .2, 'stop_loss_pct': -.1, 'max_hold_seconds': 900}
        outcome = pa._simulate_trade(row, params, round_trip_cost_pct=.008)
        self.assertIsNotNone(outcome)
        assert outcome is not None
        self.assertAlmostEqual(outcome, -.208)


if __name__ == '__main__':
    unittest.main()
