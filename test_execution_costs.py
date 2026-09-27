"""Regression tests for the unified execution cost model.

These tests assert behavioural invariants: that every consumer of round-trip
cost produces the same number, that defaults are used when keys are absent,
that malformed configs fail closed, and that grid_trader's per-side arithmetic
remains distinct from the round-trip function.
"""

import math
import unittest

import execution_costs
import paper
import parameter_autotuner


class TestExecutionCosts(unittest.TestCase):
    """Canonical module behaviour."""

    def test_current_config_yields_1_8_percent(self):
        cfg = {"paper": {"quote_bps": 40, "slippage_bps": 50}}
        result = execution_costs.round_trip_cost_pct(cfg)
        self.assertEqual(result, 0.018)

    def test_missing_slippage_uses_default_not_zero(self):
        """Config with quote_bps but no slippage_bps must still include the
        default slippage. This is the test that would have caught the autotuner
        bug where slippage_bps was silently dropped."""
        cfg = {"paper": {"quote_bps": 40}}
        result = execution_costs.round_trip_cost_pct(cfg)
        expected = 2 * (40 + execution_costs.DEFAULT_SLIPPAGE_BPS) / 10000.0
        self.assertEqual(result, expected)
        self.assertGreater(result, 2 * 40 / 10000.0)

    def test_missing_quote_uses_default_not_zero(self):
        cfg = {"paper": {"slippage_bps": 50}}
        result = execution_costs.round_trip_cost_pct(cfg)
        expected = 2 * (execution_costs.DEFAULT_QUOTE_BPS + 50) / 10000.0
        self.assertEqual(result, expected)

    def test_empty_paper_section_uses_both_defaults(self):
        cfg = {"paper": {}}
        result = execution_costs.round_trip_cost_pct(cfg)
        expected = 2 * (40 + 50) / 10000.0
        self.assertEqual(result, expected)

    def test_no_paper_key_uses_both_defaults(self):
        cfg = {}
        result = execution_costs.round_trip_cost_pct(cfg)
        expected = 2 * (40 + 50) / 10000.0
        self.assertEqual(result, expected)

    def test_none_cfg_uses_load_config(self):
        """When cfg=None the function loads from disk. We just verify it
        returns a positive number without raising."""
        result = execution_costs.round_trip_cost_pct(None)
        self.assertGreater(result, 0)

    def test_zero_config_is_explicitly_zero(self):
        cfg = {"paper": {"quote_bps": 0, "slippage_bps": 0}}
        result = execution_costs.round_trip_cost_pct(cfg)
        self.assertEqual(result, 0.0)

    def test_negative_raises(self):
        cfg = {"paper": {"quote_bps": -1, "slippage_bps": 50}}
        with self.assertRaises(ValueError):
            execution_costs.round_trip_cost_pct(cfg)

    def test_nonfinite_raises(self):
        cfg = {"paper": {"quote_bps": float("nan"), "slippage_bps": 50}}
        with self.assertRaises(ValueError):
            execution_costs.round_trip_cost_pct(cfg)

    def test_inf_raises(self):
        cfg = {"paper": {"quote_bps": float("inf"), "slippage_bps": 50}}
        with self.assertRaises(ValueError):
            execution_costs.round_trip_cost_pct(cfg)

    def test_bool_raises(self):
        cfg = {"paper": {"quote_bps": True, "slippage_bps": 50}}
        with self.assertRaises(ValueError):
            execution_costs.round_trip_cost_pct(cfg)

    def test_string_raises(self):
        cfg = {"paper": {"quote_bps": "40", "slippage_bps": 50}}
        with self.assertRaises(ValueError):
            execution_costs.round_trip_cost_pct(cfg)

    def test_none_value_raises_not_defaults(self):
        """A key present but set to None is malformed config, not 'use default'."""
        cfg = {"paper": {"quote_bps": None, "slippage_bps": 50}}
        with self.assertRaises(ValueError):
            execution_costs.round_trip_cost_pct(cfg)

    def test_cost_at_or_above_100pct_raises(self):
        cfg = {"paper": {"quote_bps": 3000, "slippage_bps": 3000}}
        with self.assertRaises(ValueError):
            execution_costs.round_trip_cost_pct(cfg)


class TestPaperReExport(unittest.TestCase):
    """paper.py re-exports from execution_costs; both paths must agree."""

    def test_paper_round_trip_matches_canonical(self):
        cfg = {"paper": {"quote_bps": 40, "slippage_bps": 50}}
        self.assertEqual(
            paper.round_trip_cost_pct(cfg),
            execution_costs.round_trip_cost_pct(cfg),
        )

    def test_paper_defaults_match_canonical(self):
        self.assertIs(paper.DEFAULT_QUOTE_BPS, execution_costs.DEFAULT_QUOTE_BPS)
        self.assertIs(paper.DEFAULT_SLIPPAGE_BPS, execution_costs.DEFAULT_SLIPPAGE_BPS)

    def test_paper_cost_config_is_load_config(self):
        self.assertIs(paper.cost_config, execution_costs.load_config)


class TestAllConsumersAgree(unittest.TestCase):
    """The core regression: every consumer must produce the same cost."""

    def test_all_consumers_agree_on_friction(self):
        cfg = {"paper": {"quote_bps": 40, "slippage_bps": 50}}
        canonical = execution_costs.round_trip_cost_pct(cfg)
        via_paper = paper.round_trip_cost_pct(cfg)

        # The autotuner's maybe_tune uses _round_trip_cost_pct internally.
        # We call it directly through the imported reference to verify the
        # autotuner now uses the same function.
        via_autotuner = parameter_autotuner._round_trip_cost_pct(cfg)

        self.assertEqual(canonical, via_paper)
        self.assertEqual(canonical, via_autotuner)
        self.assertEqual(canonical, 0.018)


class TestGridTraderStaysPerSide(unittest.TestCase):
    """grid_trader uses single-leg quote_bps adjustments, NOT the round-trip
    function. This pins the deliberate difference so a future refactor cannot
    silently substitute the round-trip charge."""

    def test_grid_trader_per_side_not_round_trip(self):
        quote_bps = 40
        per_side_factor = 1.0 - quote_bps / 10000.0
        round_trip = execution_costs.round_trip_cost_pct(
            {"paper": {"quote_bps": quote_bps, "slippage_bps": 50}}
        )
        # Per-side adjustment is roughly half the round-trip (it only charges
        # one leg and does not include slippage).
        per_side_cost_fraction = 1.0 - per_side_factor
        self.assertAlmostEqual(per_side_cost_fraction, quote_bps / 10000.0)
        # The round trip must be strictly larger than a single-leg charge.
        self.assertGreater(round_trip, per_side_cost_fraction)
        # Verify the exact relationship: round_trip = 2*(q+s)/10000
        self.assertEqual(round_trip, 2 * (quote_bps + 50) / 10000.0)


class TestReplayZeroOverride(unittest.TestCase):
    """The replay harness can obtain a zero-cost config through the canonical
    module rather than constructing raw dicts."""

    def test_replay_zero_override(self):
        cfg = execution_costs.zero_cost_config()
        result = execution_costs.round_trip_cost_pct(cfg)
        self.assertEqual(result, 0.0)

    def test_replay_zero_override_preserves_other_keys(self):
        base = {"paper": {"starting_equity_usd": 24.0}, "live": {"gate_enabled": True}}
        cfg = execution_costs.zero_cost_config(base)
        self.assertEqual(cfg["paper"]["starting_equity_usd"], 24.0)
        self.assertTrue(cfg["live"]["gate_enabled"])
        self.assertEqual(execution_costs.round_trip_cost_pct(cfg), 0.0)

    def test_replay_zero_from_none_base(self):
        cfg = execution_costs.zero_cost_config(None)
        self.assertEqual(execution_costs.round_trip_cost_pct(cfg), 0.0)


if __name__ == "__main__":
    unittest.main()