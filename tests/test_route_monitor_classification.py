"""Deterministic test: a throttle must NEVER be counted as a missing route.

This is a regression test for a real defect. The first version of the route
monitor recorded HTTP 429 as reason "no route", which produced a phantom
finding that the universe was unrouteable above $1. Throttling says nothing
about liquidity, so the two must be counted in disjoint buckets.

No network. No database. Pure classification logic.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ops"))
import route_monitor as rm  # noqa: E402


class TestThrottleIsNotLiquidity(unittest.TestCase):
    def test_throttle_reasons_are_infrastructure(self):
        for reason in ("rate_limited:http_429", "fetch_failed:timeout",
                       "no_decimals", "no_mid", "no_route:no_mid"):
            self.assertTrue(rm._is_infra(reason),
                            f"{reason!r} must not count against liquidity")

    def test_genuine_no_route_is_liquidity(self):
        for reason in ("no_route:no_route", "no_route:bad_payload",
                       "no_route:zero_amount",
                       "buy:no_route:no_route/sell:no_route:no_route"):
            self.assertFalse(rm._is_infra(reason),
                             f"{reason!r} is a real liquidity fact and must count")

    def test_ok_is_not_a_failure(self):
        self.assertFalse(rm._is_infra("ok"))

    def test_none_reason_is_infra_not_a_crash(self):
        # A None reason must not be silently tallied as a missing route.
        self.assertTrue(rm._is_infra(None))

    def test_classify_raises_no_route_for_missing_amounts(self):
        with self.assertRaises(rm.NoRoute):
            rm._classify({"inAmount": "1"})

    def test_classify_raises_no_route_for_zero(self):
        with self.assertRaises(rm.NoRoute):
            rm._classify({"inAmount": "0", "outAmount": "0"})

    def test_classify_raises_rate_limited_for_429(self):
        with self.assertRaises(rm.RateLimited):
            rm._classify({"__http_error__": 429})

    def test_classify_raises_fetch_failed_for_500(self):
        with self.assertRaises(rm.FetchFailed):
            rm._classify({"__http_error__": 500})

    def test_classify_returns_amounts_for_a_real_route(self):
        self.assertEqual(
            rm._classify({"inAmount": "1000000", "outAmount": "2000"}),
            (1000000.0, 2000.0))

    def test_no_route_and_rate_limited_are_distinct_types(self):
        # The whole point: these must not be conflated by any caller.
        self.assertFalse(issubclass(rm.NoRoute, rm.RateLimited))
        self.assertFalse(issubclass(rm.RateLimited, rm.NoRoute))


class TestDustQuoteIsNotFriction(unittest.TestCase):
    """A near-empty route must never be recorded as a cost measurement.

    Real observed defect: 6GmAFSYs4gk3 produced buy +99.900% / sell -99.900%,
    i.e. a -100.000% round trip, from a nominally positive but worthless
    outAmount. That poisoned the friction sample with an impossible value.
    """

    def test_dust_quote_is_rejected(self):
        mid = 0.0004
        # 0.1% of mid, the shape a dust outAmount produces.
        ok, why = rm._slips_are_plausible(mid, mid * 0.001, mid * 0.001)
        self.assertFalse(ok)
        self.assertIn("implausible", why)

    def test_realistic_friction_passes(self):
        mid = 0.0004
        ok, why = rm._slips_are_plausible(mid, mid * 0.999, mid * 1.002)
        self.assertTrue(ok, f"realistic friction wrongly rejected: {why}")
        self.assertEqual(why, "")

    def test_exactly_at_bound_is_accepted(self):
        # Boundary semantics matter: MAX is exclusive, so equal is allowed.
        mid = 1.0
        px = mid * (1 - rm.MAX_PLAUSIBLE_SLIP_PCT / 100.0)
        ok, _ = rm._slips_are_plausible(mid, px, mid)
        self.assertTrue(ok)

    def test_just_past_bound_is_rejected(self):
        mid = 1.0
        px = mid * (1 - (rm.MAX_PLAUSIBLE_SLIP_PCT + 0.01) / 100.0)
        ok, why = rm._slips_are_plausible(mid, px, mid)
        self.assertFalse(ok)
        self.assertIn("implausible", why)

    def test_buy_only_dust_is_caught(self):
        ok, why = rm._slips_are_plausible(1.0, 1.0, 0.0001)
        self.assertFalse(ok)
        self.assertIn("sell", why)

    def test_missing_mid_is_rejected_not_crashed(self):
        ok, why = rm._slips_are_plausible(None, 1.0, 1.0)
        self.assertFalse(ok)
        self.assertEqual(why, "no_mid")

    def test_zero_price_is_rejected(self):
        ok, why = rm._slips_are_plausible(1.0, 0.0, 1.0)
        self.assertFalse(ok)
        self.assertEqual(why, "no_buy_px")

    def test_dust_reason_counts_as_liquidity_not_infra(self):
        # An empty pool IS a liquidity fact, so it must be tallied as a
        # missing route rather than excused as throttling.
        self.assertFalse(rm._is_infra("no_route:implausible_buy_99.9pct"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
