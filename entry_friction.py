"""Quoted entry friction in percentage points, never a realized all-in fee.

Ticker/name are display only. Exact mint and atomic USDC notional identify proof.
No transaction is signed or submitted by this module.
"""
from __future__ import annotations

import json
import math
import sqlite3
import time
from contextlib import closing


MAX_QUOTE_AGE_SECONDS = 300


def _cached(db_path, mint, amount, now):
    if db_path is None:
        return None
    try:
        with closing(sqlite3.connect(str(db_path), timeout=1)) as con:
            row = con.execute('SELECT payload FROM mh_entry_friction_quotes '
                              'WHERE mint=? AND amount_atomic=?', (mint, amount)).fetchone()
        proof = json.loads(row[0]) if row else None
        if (isinstance(proof, dict) and proof.get('mint') == mint
                and proof.get('amount_atomic') == amount
                and proof.get('status') in {'ALLOWED', 'BLOCKED'}
                and math.isfinite(proof['checked_at'])
                and 0 <= now - proof['checked_at'] <= MAX_QUOTE_AGE_SECONDS):
            return proof
    except (sqlite3.Error, ValueError, TypeError, KeyError, OverflowError):
        pass  # Cache is optional; authorization still requires a fresh valid quote.
    return None


def _save(db_path, result):
    if db_path is None:
        return
    try:
        with closing(sqlite3.connect(str(db_path), timeout=1)) as con, con:
            con.execute('CREATE TABLE IF NOT EXISTS mh_entry_friction_quotes ('
                        'mint TEXT, amount_atomic INTEGER, payload TEXT NOT NULL, '
                        'PRIMARY KEY(mint,amount_atomic))')
            con.execute('INSERT OR REPLACE INTO mh_entry_friction_quotes VALUES(?,?,?)',
                        (result['mint'], result['amount_atomic'], json.dumps(result, allow_nan=False)))
    except sqlite3.Error as exc:
        result = dict(result)
        result['cache_write_failed'] = True
        result['cache_write_exception'] = type(exc).__name__



def friction_limit(cfg):
    settings = cfg.get('live', {}).get('autonomous', {}).get('dynamic_universe', {}) if cfg else {}
    if 'maximum_friction_cost_pct' not in settings:
        if settings.get('enabled') is True:
            raise ValueError('missing maximum_friction_cost_pct')
        return None
    value = settings['maximum_friction_cost_pct']
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not 0 < value <= 2:
        raise ValueError('invalid maximum_friction_cost_pct')
    return float(value)


def check_entry_friction(mint, cfg, *, api_key, amount_atomic=1_000_000,
                         get=None, now=None, db_path=None, ticker="UNKNOWN", name=None):
    from solana_token_universe import (RoundTripCostExceeded, TokenDenied,
                                       verify_round_trip)
    now = time.time() if now is None else now
    result = {'status': 'BLOCKED', 'mint': mint, 'ticker': ticker, 'name': name or ticker,
              'amount_atomic': amount_atomic,
              'checked_at': now, 'round_trip_loss_pct': None,
              'threshold_pct': None, 'reason': 'quote_unavailable'}
    try:
        result['threshold_pct'] = friction_limit(cfg)
    except ValueError:
        result['reason'] = 'invalid_friction_configuration'
        return result
    if result['threshold_pct'] is None:
        result['status'] = 'DISABLED'
        return result
    cached = _cached(db_path, mint, amount_atomic, now)
    if cached is not None and cached.get('threshold_pct') == result['threshold_pct']:
        if cached['status'] == 'BLOCKED' or proof_allows(cached, mint, amount_atomic, cfg, now):
            return cached
    started = time.monotonic()
    try:
        kwargs = {'api_key': api_key, 'amount_atomic': amount_atomic}
        import httpx
        provider_get = httpx.get if get is None else get
        def bounded_get(url, **request):
            request['timeout'] = 5
            return provider_get(url, **request)
        kwargs['get'] = bounded_get
        # Keep the existing route-safety limit independent of the tighter cost gate.
        route = verify_round_trip(mint, cfg, **kwargs)
        result.update(round_trip_loss_pct=route['round_trip_loss_pct'])
        if route['round_trip_loss_pct'] > result['threshold_pct']:
            result['reason'] = 'round_trip_cost_above_limit'
        else:
            result.update(status='ALLOWED', reason='quoted_cost_within_limit')
    except RoundTripCostExceeded as exc:
        result.update(round_trip_loss_pct=exc.cost_pct,
                      reason='round_trip_cost_above_limit')
    except TokenDenied:
        # Never leak provider responses/credentials or reuse stale success on failure.
        result['reason'] = 'route_unavailable_or_invalid'
    if time.monotonic() - started > 30:
        result.update(status='BLOCKED', reason='quote_pair_too_slow')
    _save(db_path, result)
    return result


def proof_allows(proof, mint, amount_atomic, cfg, now):
    try:
        limit = friction_limit(cfg)
        if limit is None:
            return True
        cost = proof['round_trip_loss_pct']
        ts = proof['checked_at']
        return (proof['status'] == 'ALLOWED' and proof['mint'] == mint
                and type(proof['amount_atomic']) is int and proof['amount_atomic'] == amount_atomic
                and not isinstance(cost, bool) and isinstance(cost, (int, float))
                and math.isfinite(cost) and 0 <= cost <= limit
                and not isinstance(ts, bool) and isinstance(ts, (int, float))
                and math.isfinite(ts) and 0 <= now - ts <= 300)
    except (ValueError, KeyError, TypeError):
        return False
