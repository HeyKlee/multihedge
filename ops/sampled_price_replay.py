"""Diagnostic sampled-price replay of the actual dynamic scalper, research only.

No live discovery, signer, tuning or production writes. Costs are an accounting
sensitivity, not fed back into the unchanged paper kernel's position sizing.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import itertools
import json
import math
from pathlib import Path
import sqlite3
import statistics

import pricefeed  # for price history seeding

FIELDS = ('latest_usd', 'return_5m_pct', 'return_1h_pct',
          'buy_volume_5m_usd', 'sell_volume_5m_usd')


def dump(path, value):
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False))


def csv_dump(path, rows):
    if not rows:
        Path(path).write_text('')
        return
    with Path(path).open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def validated(rows):
    if not rows:
        raise ValueError('no observations')
    result, seen = [], set()
    for raw in rows:
        row = dict(raw)
        mint = row.get('mint')
        if not isinstance(mint, str) or not mint:
            raise ValueError('missing mint')
        for key in ('observed_ts',) + FIELDS:
            if isinstance(row.get(key), bool):
                raise ValueError('invalid numeric observation')
            row[key] = float(row[key])
            if not math.isfinite(row[key]):
                raise ValueError('nonfinite observation')
        if row['latest_usd'] <= 0 or row['observed_ts'] < 0:
            raise ValueError('invalid price or timestamp')
        if min(row['buy_volume_5m_usd'], row['sell_volume_5m_usd']) < 0:
            raise ValueError('negative volume')
        key = (row['observed_ts'], mint)
        if key in seen:
            raise ValueError('duplicate observation key')
        seen.add(key)
        result.append(row)
    return sorted(result, key=lambda r: (r['observed_ts'], r['mint']))


def replay(rows, output, cfg, per_side_bps=40.0, max_gap_seconds=300.0):
    from dynamic_shadow_scalper import tick, INITIAL_EQUITY_USD
    rows = validated(rows)
    if not math.isfinite(per_side_bps) or not 0 <= per_side_bps <= 1000:
        raise ValueError('invalid costs')
    if not math.isfinite(max_gap_seconds) or max_gap_seconds <= 0:
        raise ValueError('invalid gap limit')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    db = output / 'simulation.db'
    dump(output / 'observations.json', rows)
    dump(output / 'config.json', cfg)
    rate = per_side_bps / 10000
    starting = INITIAL_EQUITY_USD
    prices, times, gaps, curves = {}, {}, {}, []
    first_ts = rows[0]['observed_ts']
    # Pre-seed pricefeed for each mint so compute_volume_avg_20 and
    # compute_rsi_14 return real values instead of fallback placeholders.
    # 20 baseline observations at 50% of the first real observation's
    # volume establish a baseline; the first real observation then appears
    # as a volume surge passing the 1.2x entry signal check.
    for mint in {r['mint'] for r in rows}:
        first = next(r for r in rows if r['mint'] == mint)
        vol = first['buy_volume_5m_usd'] + first['sell_volume_5m_usd']
        baseline_vol = vol * 0.5
        for i in range(20):
            ts = first['observed_ts'] - (20 - i) * 300
            pricefeed._update_price_history(mint, ts, first['latest_usd'],
                                            baseline_vol * 0.5, baseline_vol * 0.5)
    # Only assets observable at the first timestamp enter this benchmark.
    initial = {r['mint']: r['latest_usd'] for r in rows if r['observed_ts'] == first_ts}
    bh_qty = {m: starting / len(initial) / p for m, p in initial.items()}
    peak = starting
    worst_dd = 0.0
    for ts, batch in itertools.groupby(rows, key=lambda r: r['observed_ts']):
        batch = list(batch)
        candidates = []
        for row in batch:
            mint = row['mint']
            if mint in times:
                gaps.setdefault(mint, []).append((times[mint], ts, ts-times[mint]))
            times[mint], prices[mint] = ts, row['latest_usd']
            # Update pricefeed with each real observation so compute_rsi_14
            # and compute_volume_avg_20 can return real values from the
            # pre-seeded baseline plus this observation.
            pricefeed._update_price_history(mint, ts, row['latest_usd'],
                                            row['buy_volume_5m_usd'], row['sell_volume_5m_usd'])
            vol_avg = pricefeed.compute_volume_avg_20(mint)
            market = {k: row[k] for k in FIELDS}
            market['rsi_15m'] = 50.0
            market['volume_5m_usd'] = row['buy_volume_5m_usd'] + row['sell_volume_5m_usd']
            market['volume_5m_avg_20'] = (
                vol_avg if vol_avg is not None
                else row['buy_volume_5m_usd'] + row['sell_volume_5m_usd']
            )
            candidates.append({'mint': mint, 'ticker': 'REPLAY', 'decimals': 0,
                               'market': market})
        tick(db, candidates, now=ts, cfg=cfg)
        with sqlite3.connect(db) as con:
            con.row_factory = sqlite3.Row
            trades = [dict(r) for r in con.execute('SELECT * FROM mh_trades ORDER BY close_ts,id')]
            positions = [dict(r) for r in con.execute('SELECT * FROM mh_dynamic_scalp_positions ORDER BY mint')]
            account = con.execute("SELECT equity_usd FROM mh_accounts WHERE trader='dynamic_scalper'").fetchone()[0]
        gross = account + sum(p['qty']*(prices[p['mint']]-p['entry_usd']) for p in positions)
        costs = rate * (sum(t['qty']*(t['entry_px']+t['exit_px']) for t in trades)
                        + sum(p['qty']*p['entry_usd'] for p in positions))
        liquidation = rate * sum(p['qty']*prices[p['mint']] for p in positions)
        net = gross-costs
        peak = max(peak, net)
        worst_dd = max(worst_dd, (peak-net)/peak)
        bh_gross = sum(q*prices[m] for m, q in bh_qty.items())
        curves.append({'ts': ts, 'gross_marked_equity_usd': gross,
                       'net_marked_equity_usd': net, 'paid_costs_usd': costs,
                       'estimated_open_liquidation_cost_usd': liquidation,
                       'open_positions': len(positions), 'closed_trades': len(trades),
                       'max_open_mark_age_seconds': max([ts-times[p['mint']] for p in positions] or [0]),
                       'buy_hold_gross_usd': bh_gross,
                       'buy_hold_net_liquidation_usd': bh_gross-rate*(starting+bh_gross)})
    for t in trades:
        t['modeled_cost_usd'] = rate*t['qty']*(t['entry_px']+t['exit_px'])
        t['net_usd'] = t['realized_usd']-t['modeled_cost_usd']
        t['max_path_gap_seconds'] = max([g for a,b,g in gaps.get(t['coin'], [])
                                       if a >= t['open_ts'] and b <= t['close_ts']] or [0])
        t['gap_flagged'] = t['max_path_gap_seconds'] > max_gap_seconds
    for p in positions:
        p['last_price'] = prices[p['mint']]
        p['last_price_ts'] = times[p['mint']]
        p['mark_age_seconds'] = rows[-1]['observed_ts']-times[p['mint']]
        p['unrealized_usd'] = p['qty']*(p['last_price']-p['entry_usd'])
    all_gaps = [g for group in gaps.values() for a,b,g in group]
    per_mint = []
    for mint in sorted(prices):
        chosen = [t for t in trades if t['coin'] == mint]
        per_mint.append({'mint': mint, 'closed_trades': len(chosen),
                         'gross_realized_usd': sum(t['realized_usd'] for t in chosen),
                         'net_closed_usd': sum(t['net_usd'] for t in chosen),
                         'gap_flagged_trades': sum(t['gap_flagged'] for t in chosen)})
    report = {
        'state': 'DIAGNOSTIC_SAMPLED_REPLAY_ONLY', 'strategy': 'dynamic_scalper',
        'promotion_allowed': False, 'observations': len(rows), 'distinct_mints': len(prices),
        'start_ts': first_ts, 'end_ts': rows[-1]['observed_ts'],
        'starting_equity_usd': starting, 'closed_trades': len(trades),
        'open_positions': len(positions), 'gross_realized_usd': sum(t['realized_usd'] for t in trades),
        'net_closed_usd': sum(t['net_usd'] for t in trades),
        'net_closed_win_rate': sum(t['net_usd']>0 for t in trades)/len(trades) if trades else None,
        'gross_final_marked_equity_usd': gross, 'modeled_paid_costs_usd': costs,
        'net_final_marked_equity_usd': net, 'net_liquidation_estimate_usd': net-liquidation,
        'sampled_net_max_drawdown': worst_dd,
        'buy_hold_net_liquidation_usd': curves[-1]['buy_hold_net_liquidation_usd'],
        'buy_hold_initial_mints': list(initial), 'per_side_bps': per_side_bps,
        'gap_limit_seconds': max_gap_seconds, 'median_gap_seconds': statistics.median(all_gaps) if all_gaps else None,
        'max_gap_seconds': max(all_gaps) if all_gaps else None,
        'gap_flagged_closed_trades': sum(t['gap_flagged'] for t in trades),
        'max_final_open_mark_age_seconds': curves[-1]['max_open_mark_age_seconds'],
        'observations_sha256': hashlib.sha256((output/'observations.json').read_bytes()).hexdigest(),
        'limitations': [
            'Fresh $10 paper account and empty prehistory: not a production-state reconstruction.',
            'Only recorded candidates replayed; selection bias and missing prices remain.',
            'Sampled-price fills at decision time; no intrabar prices, latency or executable quotes.',
            'Gap-flagged trades remain in diagnostic totals; none qualify for promotion.',
            'Costs applied after simulation; unchanged kernel sizes and quarantines on gross paper P&L.',
            'Open positions marked at last known prices; stale marks and liquidation estimates are not fills.',
            'Decimals=0 is a storage placeholder unused in fractional sizing; metadata admission not replayed.',
            'Current config classification and default exits frozen; no historical tuned overrides or autotuning.',
            'Buy-and-hold uses equal weights in first-timestamp candidates only, a descriptive benchmark.',
            'No holdout validation or parameter search; this is one baseline diagnostic, not proof of edge.'
        ]}
    csv_dump(output/'trades.csv', trades)
    csv_dump(output/'equity_curve.csv', curves)
    csv_dump(output/'open_positions.csv', positions)
    csv_dump(output/'per_mint.csv', per_mint)
    dump(output/'report.json', report)
    return report


def main():
    import yaml
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    with sqlite3.connect(args.source.resolve().as_uri()+'?mode=ro', uri=True, timeout=5) as con:
        con.row_factory = sqlite3.Row
        con.execute('PRAGMA query_only=ON')
        rows = [dict(r) for r in con.execute('SELECT * FROM mh_shadow_entry_observations ORDER BY observed_ts,mint')]
    result = replay(rows, args.output, yaml.safe_load(args.config.read_text()))
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
