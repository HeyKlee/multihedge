import tempfile, uuid, json, sqlite3
from pathlib import Path
from ops.sampled_price_replay import replay
import argparse
import yaml
def run(source_path, output_path, per_side_bps=40.0):
    """Run sampled replay with given source DB and output directory.
    
    Args:
        source_path: Path to source SQLite database (must exist)
        output_path: Path for output directory (must not exist)
        per_side_bps: Cost basis in basis points
    
    Returns:
        dict: Replay report
    """
    src = Path(source_path)
    out = Path(output_path)
    
    # Validate source exists
    if not src.exists():
        raise FileNotFoundError(f'Source database not found: {src}')
    
    # Validate output doesn't exist (replay requires absent output)
    if out.exists():
        raise FileExistsError(f'Output directory already exists: {out}')
    
    con = sqlite3.connect(src.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA query_only=ON')
    rows = [dict(r) for r in con.execute('SELECT observed_ts, mint, latest_usd, return_5m_pct, return_1h_pct, buy_volume_5m_usd, sell_volume_5m_usd FROM mh_shadow_entry_observations ORDER BY observed_ts, mint')]
    con.close()
    
    # Use diagnostic cfg explicitly labeled as classification limitation
    cfg = {'coins': []}
    return replay(rows, out, cfg, per_side_bps=per_side_bps)
def main():
    parser = argparse.ArgumentParser(description='Run sampled price replay for diagnostic purposes')
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parent / 'deploy/data/multihedge.db',
                       help='Path to source SQLite database (default: deploy/data/multihedge.db)')
    parser.add_argument('--output', type=Path,
                       default=Path(f'backtest-results/sampled-price-replay-{uuid.uuid4().hex[:8]}'),
                       help='Output directory for results (default: unique timestamped directory under backtest-results)')
    args = parser.parse_args()
    
    result = run(args.source, args.output)
    print('Replay complete. Report:')
    print(json.dumps(result, indent=2))
    
    # Also compute trade-level stats from trades.csv
    import csv
    trades_file = args.output / 'trades.csv'
    if trades_file.exists():
        with trades_file.open() as f:
            reader = csv.DictReader(f)
            trades = list(reader)
        if trades:
            wins = [float(t['net_usd']) for t in trades if float(t['net_usd']) > 0]
            losses = [float(t['net_usd']) for t in trades if float(t['net_usd']) < 0]
            print(f'Number of trades: {len(trades)}')
            print(f'Wins: {len(wins)}, Losses: {len(losses)}')
            if wins:
                print(f'Average win: {sum(wins)/len(wins):.4f}')
            if losses:
                print(f'Average loss: {sum(losses)/len(losses):.4f} (negative)')
            if wins and losses:
                print(f'Profit factor: {sum(wins)/abs(sum(losses)):.2f}')
            print(f'Average net per trade: {sum(float(t["net_usd"]) for t in trades)/len(trades):.4f}')
    else:
        print('No trades.csv found.')
if __name__ == '__main__':
    main()