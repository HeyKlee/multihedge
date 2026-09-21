#!/usr/bin/env python3
"""TDRed — volatility gate tests, RED phase first.

Run: python3 -m unittest -q test_volatility_gate.py
"""
import math
import unittest
from pathlib import Path

from dynamic_shadow_scalper import _entry_signal


class VolatilityGateTests(unittest.TestCase):
    """Tests for 1h volatility-based entry gate.

    These tests expect _entry_signal to accept a ``volatility_filter_enabled``
    kwarg. They will FAIL before the gate is implemented and PASS after the
    minimal implementation.
    """

    def test_volatility_gate_disabled_preserves_signal(self):
        """Gate disabled → no new filtering, signal passes as before."""
        row = {
            "market": {"return_5m_pct": 0.02, "return_1h_pct": 0.05,
                       "return_4h_pct": None, "buy_volume_5m_usd": 10000,
                       "sell_volume_5m_usd": 5000},
            "mint": "test_mint",
        }
        result = _entry_signal(row, trend_filter_enabled=True,
                               volatility_filter_enabled=False)
        self.assertTrue(result)

    def test_volatility_gate_enabled_block_when_no_history(self):
        """Gate enabled, fewer than 6 prior prices → block entry."""
        row = {
            "market": {"return_5m_pct": 0.02, "return_1h_pct": 0.05,
                       "return_4h_pct": None, "buy_volume_5m_usd": 10000,
                       "sell_volume_5m_usd": 5000},
            "mint": "test_mint",
        }
        result = _entry_signal(row, trend_filter_enabled=True,
                               volatility_filter_enabled=True,
                               volatility_min_samples=6)
        # No history → blocks
        self.assertFalse(result)

    def test_volatility_gate_enabled_low_vol_pct_allows(self):
        """Gate enabled, 1h vol below threshold → does not block."""
        row = {
            "market": {"return_5m_pct": 0.02, "return_1h_pct": 0.05,
                       "return_4h_pct": None, "buy_volume_5m_usd": 10000,
                       "sell_volume_5m_usd": 5000},
            "mint": "test_mint",
        }
        result = _entry_signal(row, trend_filter_enabled=True,
                               volatility_filter_enabled=True,
                               volatility_min_samples=6,
                               volatility_max_pct=0.10)
        # Low synthetic vol → passes
        self.assertTrue(result)

    def test_volatility_gate_enabled_high_vol_pct_blocks(self):
        """Gate enabled, 1h vol above threshold → blocks entry."""
        row = {
            "market": {"return_5m_pct": 0.02, "return_1h_pct": 0.05,
                       "return_4h_pct": None, "buy_volume_5m_usd": 10000,
                       "sell_volume_5m_usd": 5000},
            "mint": "test_mint",
        }
        result = _entry_signal(row, trend_filter_enabled=True,
                               volatility_filter_enabled=True,
                               volatility_min_samples=6,
                               volatility_max_pct=0.01)
        # High synthetic vol → blocks
        self.assertFalse(result)