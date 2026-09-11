# MultiHedge Grid Trader - Design Notes

Standalone spot long-only geometric grid trader for SOL. Fully isolated from
the scalper/reasoner paper wallets: own tables (`grid_wallet`, `grid_trades`,
`grid_state`), own config block (`grid:` in config.yaml), own kill switch.
Never touches mh_accounts / mh_trades / gate stats.

## Grid mechanics
- Geometric levels: N levels spanning +/- range_pct around a center price,
  level_k = center * (1+range_pct)^(k/(N//2)).
- Each tick (~30s cadence): price crossing DOWN through a level buys at that
  level (+ quote_bps slippage from config.paper); a virtual sell is placed one
  grid step up. Price crossing UP through the sell level fills it and realizes
  cycle profit into the wallet cash.
- Dynamic Grid Reset: if price breaches a range edge by >= 25% of range_pct
  of the span AND at least 10 min have passed since the last reset, rebuild
  the grid centered on current price. Open inventory kept, valued at market.
  Hysteresis + cooldown prevent chop thrash.

## Risk guards
- Mark-to-market equity kill switch: equity = cash + sol_qty * px every tick;
  peak_equity tracked on marked equity; new buys pause at >= max_drawdown_kill
  (20%) drawdown via grid_wallet.paused. Runs even during flash-crash ticks so
  bag-accumulation drawdown trips promptly.
- Flash-crash guard: skip buys when last close < prev close * flash_crash_drop
  (0.94).
- Price staleness bound: prices older than 90s make the whole tick a no-op
  (no buys, no resets, no crash-guard evaluation).
- Wallet seeded once, idempotent: starting_cash_nzd * usd_per_nzd USD cash.

## Wiring
- `python grid_trader.py tick [px]` for one tick (live Jupiter price if no px);
  `python grid_trader.py status` for JSON. Callable as grid_tick(px, cfg) from
  any existing loop.
- dash_web.py shows a Grid trader (SOL) row: cash, SOL, peak equity, paused,
  cycles completed, realized USD.
- Self-tests: `python test_grid_trader.py` (isolated temp DB; sine-wave profit
  test, kill-switch pause test, staleness + hysteresis test).
