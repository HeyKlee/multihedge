"""Test that live_inventory MEME defaults match intended paper-only config."""
import unittest
from live_inventory import (
    MEME_TAKE_PROFIT_PCT,
    MEME_STOP_LOSS_PCT,
    MEME_TRAIL_ARM_PCT,
    MEME_TRAIL_DISTANCE_PCT,
    MEME_MAX_HOLD_SECONDS,
    SERIOUS_TAKE_PROFIT_PCT,
    SERIOUS_STOP_LOSS_PCT,
    SERIOUS_TRAIL_ARM_PCT,
    SERIOUS_TRAIL_DISTANCE_PCT,
    SERIOUS_MAX_HOLD_SECONDS,
)


class LiveInventoryDefaultsTest(unittest.TestCase):
    """Verify MEME and SERIOUS defaults match config.yaml intent."""

    def test_meme_defaults_match_intended_config(self):
        """MEME defaults should be 1.5% TP, 1.5% SL, 0.5% trail arm, 0.3% trail dist, 30min max hold."""
        self.assertAlmostEqual(MEME_TAKE_PROFIT_PCT, 0.015, places=4)
        self.assertAlmostEqual(MEME_STOP_LOSS_PCT, -0.015, places=4)
        self.assertAlmostEqual(MEME_TRAIL_ARM_PCT, 0.005, places=4)
        self.assertAlmostEqual(MEME_TRAIL_DISTANCE_PCT, 0.003, places=4)
        self.assertEqual(MEME_MAX_HOLD_SECONDS, 1800)

    def test_serious_defaults_unchanged(self):
        """SERIOUS defaults should remain at current values: 1% TP, 1% SL, 0.5% trail arm, 0.3% dist, 30min hold."""
        self.assertAlmostEqual(SERIOUS_TAKE_PROFIT_PCT, 0.010, places=4)
        self.assertAlmostEqual(SERIOUS_STOP_LOSS_PCT, -0.010, places=4)
        self.assertAlmostEqual(SERIOUS_TRAIL_ARM_PCT, 0.005, places=4)
        self.assertAlmostEqual(SERIOUS_TRAIL_DISTANCE_PCT, 0.003, places=4)
        self.assertEqual(SERIOUS_MAX_HOLD_SECONDS, 1800)

    def test_legacy_aliases_match_meme(self):
        """Legacy constant names should alias to MEME defaults."""
        from live_inventory import (
            TAKE_PROFIT_PCT,
            STOP_LOSS_PCT,
            TRAIL_ARM_PCT,
            TRAIL_DISTANCE_PCT,
            MAX_HOLD_SECONDS,
        )
        self.assertEqual(TAKE_PROFIT_PCT, MEME_TAKE_PROFIT_PCT)
        self.assertEqual(STOP_LOSS_PCT, MEME_STOP_LOSS_PCT)
        self.assertEqual(TRAIL_ARM_PCT, MEME_TRAIL_ARM_PCT)
        self.assertEqual(TRAIL_DISTANCE_PCT, MEME_TRAIL_DISTANCE_PCT)
        self.assertEqual(MAX_HOLD_SECONDS, MEME_MAX_HOLD_SECONDS)


if __name__ == "__main__":
    unittest.main()