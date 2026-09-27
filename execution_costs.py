"""Canonical round-trip execution cost model for MultiHedge paper trading.

Every consumer that prices friction (paper ledger, parameter autotuner, replay
harnesses) must obtain its cost from THIS module. Two independent formulas
previously existed and disagreed by more than 2x: the paper engine charged both
quote_bps and slippage_bps per leg, while the autotuner silently dropped
slippage_bps entirely. That understatement made a +1.5% take-profit look
attractive when it was actually net negative after true friction.

Both bps values are required because a Jupiter swap pays the quoted fee
(quote_bps) AND suffers price movement between quote and fill (slippage_bps).
A round trip is two legs (buy then sell), so the total is:

    2 * (quote_bps + slippage_bps) / 10000

If either value drifts or is omitted, the resulting cost silently misprices
friction and every downstream statistic (expectancy, win rate, PnL) becomes
fiction. The evidence gate would then calibrate on optimistic numbers and
promote parameters that lose money live.

Defaults:
    DEFAULT_QUOTE_BPS = 40   (Jupiter's quoted fee)
    DEFAULT_SLIPPAGE_BPS = 50 (observed fill-price deviation)

An ABSENT config key means "use the conservative default". A key that IS
present but set to None is a malformed config and raises rather than silently
falling back, because a truncated config file must not look healthy.
"""

import math
from pathlib import Path

DEFAULT_QUOTE_BPS = 40
DEFAULT_SLIPPAGE_BPS = 50

CFG_PATH = Path(__file__).parent / "config.yaml"
_COST_CFG_CACHE = {"mtime": None, "cfg": None}


def load_config():
    """Return the parsed runtime config dict, reloading when the file changes.

    This is the single YAML reader for cost keys. Every module that needs
    paper cost configuration calls this rather than opening config.yaml
    independently, so there is exactly one place where the file path, the
    encoding, and the cache invalidation logic live.
    """
    import yaml
    try:
        mtime = CFG_PATH.stat().st_mtime
    except OSError as e:
        raise ValueError(f"cannot read {CFG_PATH}: {e}") from e
    if _COST_CFG_CACHE["mtime"] != mtime:
        try:
            _COST_CFG_CACHE["cfg"] = yaml.safe_load(
                CFG_PATH.read_text(encoding="utf-8")) or {}
        except Exception as e:
            raise ValueError(f"cannot parse {CFG_PATH}: {e}") from e
        _COST_CFG_CACHE["mtime"] = mtime
    return _COST_CFG_CACHE["cfg"] or {}


def round_trip_cost_pct(cfg=None):
    """Return the round-trip trading cost as a positive fraction of notional.

    Reads ``quote_bps`` and ``slippage_bps`` from ``cfg["paper"]`` when a
    config dict is provided, falling back to the module-level defaults for
    any absent key. Both values are required because omitting slippage_bps
    understates true friction by roughly half, which caused the autotuner to
    score candidates against less than the real cost and promote parameters
    that lose money.

    Validation (fail-closed):
        - bool values are rejected (bool is an int subclass in Python).
        - Non-finite floats (NaN, Inf) are rejected.
        - Negative values are rejected.
        - A computed cost >= 1.0 (100%) is rejected.
        - A key present but set to None raises rather than using the default,
          because silent fallback would mask a truncated or malformed config.

    Args:
        cfg: Optional parsed config dict. When None, the function loads the
             runtime config via ``load_config()``. Pass an explicit dict
             (e.g. ``{"paper": {"quote_bps": 0, "slippage_bps": 0}}``) to
             override, which the replay harness uses for zero-cost runs.

    Returns:
        float: Round-trip cost as a fraction (e.g. 0.018 for 1.8%).
    """
    if cfg is None:
        cfg = load_config()
    p = {}
    if cfg:
        p = cfg.get("paper") or {}

    defaults = {"quote_bps": DEFAULT_QUOTE_BPS, "slippage_bps": DEFAULT_SLIPPAGE_BPS}
    cost_keys = ("quote_bps", "slippage_bps")
    total_bps = 0.0

    for key in cost_keys:
        # An ABSENT key means "use the conservative default". A key that is
        # present but None is a malformed config and must not be silently
        # upgraded into the default, or a truncated config would look healthy.
        if key in p:
            raw = p[key]
            if raw is None:
                raise ValueError(
                    f"paper.{key} is explicitly None; this indicates a malformed "
                    f"config. Remove the key to use the default ({defaults[key]}) "
                    f"or provide a valid number."
                )
        else:
            raw = defaults[key]

        # bool is an int subclass; reject it rather than price friction at 1 bps
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError(f"paper.{key} must be a finite number, got {raw!r}")
        val = float(raw)
        if not math.isfinite(val):
            raise ValueError(f"paper.{key} must be finite, got {raw!r}")
        if val < 0:
            raise ValueError(f"paper.{key} must not be negative, got {raw!r}")
        total_bps += val

    cost = (total_bps * 2.0) / 10_000.0
    if cost >= 1.0:
        raise ValueError(f"round-trip cost must be below 100%, got {cost!r}")
    return cost


def zero_cost_config(base_cfg=None):
    """Return a config dict that produces zero round-trip cost.

    Used by the sampled-price replay harness, which models friction
    externally and must prevent double-counting. This provides a canonical
    way to obtain a zero-cost config rather than each caller constructing
    its own raw dict.

    Args:
        base_cfg: Optional base config to copy. When None, starts empty.

    Returns:
        dict: A config dict suitable for passing to round_trip_cost_pct()
              that yields exactly 0.0.
    """
    cfg = dict(base_cfg or {})
    paper = dict(cfg.get("paper") or {})
    paper["quote_bps"] = 0
    paper["slippage_bps"] = 0
    cfg["paper"] = paper
    return cfg
