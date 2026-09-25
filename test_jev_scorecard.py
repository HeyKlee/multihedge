import importlib.util
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "jev_scorecard", Path(__file__).resolve().parent / "ops" / "jev_scorecard.py")
assert SPEC is not None and SPEC.loader is not None
jev_scorecard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(jev_scorecard)


class JevScorecardTests(unittest.TestCase):
    def test_buy_is_right_only_on_an_up_move(self):
        self.assertTrue(jev_scorecard.score_call("BUY", 100.0, 101.0, 0.25)[1])
        self.assertFalse(jev_scorecard.score_call("BUY", 100.0, 99.0, 0.25)[1])

    def test_sell_is_right_only_on_a_down_move(self):
        self.assertTrue(jev_scorecard.score_call("SELL", 100.0, 99.0, 0.25)[1])
        self.assertFalse(jev_scorecard.score_call("SELL", 100.0, 101.0, 0.25)[1])

    def test_hold_is_right_inside_the_band_and_wrong_outside_it(self):
        self.assertTrue(jev_scorecard.score_call("HOLD", 100.0, 100.1, 0.25)[1])
        self.assertFalse(jev_scorecard.score_call("HOLD", 100.0, 101.0, 0.25)[1])
        self.assertTrue(jev_scorecard.score_call("HOLD", 100.0, 99.8, 0.25)[1])

    def test_forward_return_is_reported_in_percent(self):
        fwd_pct, _ = jev_scorecard.score_call("BUY", 200.0, 202.0, 0.25)
        self.assertAlmostEqual(fwd_pct, 1.0)

    def test_flat_market_is_a_correct_hold(self):
        self.assertTrue(jev_scorecard.score_call("HOLD", 100.0, 100.0, 0.25)[1])


if __name__ == "__main__":
    unittest.main()
