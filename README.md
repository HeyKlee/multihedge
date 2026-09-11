# MultiHedge — Multi-Coin Solana Paper Trading App

A **separate application** (sibling to AutoHedge) that trades **any coin on the Solana
network** using the **same wallet architecture** — but **paper-first**: it stays in
virtual money until paper proves a consistent 3:1 / 7:1 win/loss over a meaningful
sample, then and only then can it touch real funds.

Sits at `D:/AI/HERMES/multihedge/`. Fully self-contained except it reuses
AutoHedge's generic `chain.py` Solana wrapper for live execution.

## What makes it different from AutoHedge

| | AutoHedge | MultiHedge |
|---|---|---|
| Universe | SOL/USDC hardcoded | **Any SPL token** configured |
| Prices | hardcoded symbol feed | **By-mint Jupiter** (generic) + fallbacks |
| Strategy | one rotation for SOL | **Per-coin rotation** |
| Ledger | paper + live in one | **paper (virtual) + gated live** |
| App | AutoHedge | **Separate `multihedge/` app** |

## Coin universe (config-driven)

Add/remove coins by editing `config.yaml`. Each needs a symbol + Solana SPL
mint. Pricing is by-mint via Jupiter (works for literally any token), so you're
not limited to the pre-listed symbols:

```yaml
coins:
  - symbol: "SOL"
    mint: "So11111111111111111111111111111111111111112"
```

## Strategy: per-coin fresh rotation

Each coin runs its **own** strategy rotation across fresh setup families:
- `momentum_breakout` — rides breakouts above recent highs / below lows
- `mean_reversion` — buys sharp dips, sells sharp spikes
- `rsi_oversold` — RSI(2) deep-oversold/overbought captures
- `vwap_reversion` — fades large drift from rolling VWAP

Each (coin, setup) is tracked separately with points + Beta posterior, so a
breakout coin and a reversion coin each discover and ride their own best edge.
Failing setups fade and get disabled. (Same rotation concept as AutoHedge's
strat_select, but generalised per-coin.)

## Paper engine (real fills, NO wallet write)

- Every coin gets a virtual account (default $1000 starting equity).
- Signals are priced with **real Jupiter quotes** + simulated slippage — realistic
  fills tracking slippage/route, but **never** submitted to a wallet.
- Position sizing = fraction of paper equity (default 20%), dust filter.
- Trade management: **take-profit +2.5%, stop-loss -1.5%, 1h max-hold**, and a
  **trailing stop** that arms once up +1.2% and trails 0.6% behind the peak
  (protects a winning trade from riding back to the time-out).
- All trades logged to `multihedge.db` (`mh_trades`), equity per coin in
  `mh_accounts`.

## Live gate (Kelly's rule, enforced)

Real wallet is **untouched** until paper proves it. `live_bridge.py` is the ONLY
module that can write on-chain, and it refuses unless:

1. network is mainnet (or devnet test) **and** `live_mode=1` set manually
2. wallet balance > safe SOL reserve
3. the **paper gate passes**: the coin shows a **3:1 or 7:1 win/loss** (≥75% win
   rate) over ≥20 closed trades — *enforced, not just displayed*
4. a `confirm_live.flag` exists for per-trade confirmation

If the gate fails, `execute_swap` raises and nothing is written. See the
self-test which verifies it refuses before the gate and allows after.

## Files

```
multihedge/
  config.yaml        <- coin universe, paper params, live gate limits
  pricefeed.py        <- by-mint Jupiter (any token) + CoinGecko/Binance fallback
  strategy.py         <- per-coin setup families + rotation
  paper.py            <- virtual multi-coin ledger, real Jupiter fills, gate report
  live_bridge.py      <- GATED real-wallet executor (only writer)
  multihedge.py       <- orchestration loop + CLI
  _selftest.py        <- deterministic unit tests (no network)
  _integtest.py       <- full run_tick loop test (open->close)
  multihedge.db       <- created on first run
```

## Usage (use AutoHedge's venv -- has yaml/httpx/solana)

```bash
cd D:/AI/HERMES/multihedge
D:/AI/HERMES/autohedge/.venv/Scripts/python.exe multihedge.py tick     # one pass
D:/AI/HERMES/autohedge/.venv/Scripts/python.exe multihedge.py status  # equity + live gate
D:/AI/HERMES/autohedge/.venv/Scripts/python.exe multihedge.py history # closed trades
D:/AI/HERMES/autohedge/.venv/Scripts/python.exe multihedge.py loom    # continuous loop
```

## When to go live

Run `loom` long enough to accumulate paper trades (the gate needs ≥20 per coin
with ≥75% win rate). `status` reports per-coin `live_gate` eligibility. Only when
a coin reports `eligible: true` AND you've set `live_mode=1` in the AutoHedge
config AND created `confirm_live.flag` will `live_bridge.execute_swap()` actually
submit what to the real wallet. Until then, the real wallet is never touched.