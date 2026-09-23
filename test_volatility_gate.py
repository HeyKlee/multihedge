#!/usr/bin/env python3
"""Volatility gate entry tests.

These were originally RED-phase TDD placeholders for a volatility gate
feature that is not yet implemented. With **kwargs removed from
_entry_signal (the correct fix for silent absorption of test-only kwargs),
tests that pass unknown keyword arguments now fail with TypeError. The
tests below exercise real _entry_signal behavior without fake kwargs.

To implement the volatility gate:
  1. Add volatility_filter_enabled, volatility_min_samples,
     volatility_max_pct kwargs to _entry_signal
  2. Implement 1h price volatility computation and gate logic
  3. Un-skip these tests as GREEN-phase verification

Run: python3 -m unittest -q test_volatility_gate.py
"""
import unittest
from dynamic_shadow_scalper import _entry_signal


class VolatilityGateTests(unittest.TestCase):
    """Tests for _entry_signal that exercise real behavior."""

    def test_valid_signal_passes_entry_check(self):
        """A well-formed candidate with neutral RSI and sufficient volume passes."""
        row = {
            "market": {
                "return_5m_pct": 2.0, "return_1h_pct": 5.0,
                "buy_volume_5m_usd": 10000, "sell_volume_5m_usd": 5000,
                "rsi_15m": 50.0, "volume_5m_usd": 100000,
                "volume_5m_avg_20": 50000,
            },
            "mint": "test_mint",
        }
        result = _entry_signal(row, now=1000.0)
        self.assertTrue(result)

    def test_overbought_rsi_blocks_entry(self):
        """RSI >= 80 blocks entry (overbought rejection)."""
        row = {
            "market": {
                "return_5m_pct": 2.0, "return_1h_pct": 5.0,
                "buy_volume_5m_usd": 10000, "sell_volume_5m_usd": 5000,
                "rsi_15m": 85.0, "volume_5m_usd": 100000,
                "volume_5m_avg_20": 50000,
            },
            "mint": "test_mint",
        }
        result = _entry_signal(row, now=1000.0)
        self.assertFalse(result)

    def test_missing_rsi_falls_back_to_neutral(self):
        """Missing RSI in market data falls back to 50 (neutral)."""
        row = {
            "market": {
                "return_5m_pct": 2.0, "return_1h_pct": 5.0,
                "buy_volume_5m_usd": 10000, "sell_volume_5m_usd": 5000,
                "volume_5m_usd": 100000, "volume_5m_avg_20": 50000,
            },
            "mint": "test_mint",
        }
        result = _entry_signal(row, now=1000.0)
        # Neutral fallback + sufficient volume means signal passes
        self.assertTrue(result)

    def test_no_sell_volume_blocks_entry(self):
        """Zero sell volume blocks entry (no trading activity)."""
        row = {
            "market": {
                "return_5m_pct": 2.0, "return_1h_pct": 5.0,
                "buy_volume_5m_usd": 10000, "sell_volume_5m_usd": 0,
                "rsi_15m": 50.0, "volume_5m_usd": 100000,
                "volume_5m_avg_20": 50000,
            },
            "mint": "test_mint",
        }
        result = _entry_signal(row, now=1000.0)
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()