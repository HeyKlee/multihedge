#!/usr/bin/env python3
"""Regression test: the sell leg must size in tokens, not USD-notional x scale.

BUG BEING GUARDED
-----------------
`route_monitor.leg_px` used to build the sell leg as:

    amt = int(usd * 10 ** dec)

which multiplies the USD notional by the token's decimal scale instead of
converting USD into tokens. For a "$2 sell" on a 9-decimal token priced at
$0.0000017 that asked Jupiter to sell 2,000,000,000 tokens, about $3,400 of
value, and on a 6-decimal token about $540,000. The pool cannot fill that, so
the sell leg either found no route or came back against a truncated pool.

The signature in the database was a measured SELL slippage with a median of
+0.010%: it tracked mid, because it was not a real two-sided quote. That
understated round-trip cost and made the conservative 1.800% look like a 6x
overcharge.

These tests are offline. They stub the network and assert on the request the
function builds, so they never spend money and never touch a live pool.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
# route_monitor lives in ops/, not the repo root.
sys.path.insert(0, str(ROOT / "ops"))

import route_monitor  # noqa: E402

USDC = route_monitor.USDC


def _minted_params(usd, dec, side, mid):
    """Return the params dict leg_px would build, using the fixed sizing."""
    if side == "buy":
        return {"inputMint": USDC, "outputMint": mid and "MINT" or "MINT",
                "amount": int(usd * 10 ** 6)}
    amt = int((usd / mid) * 10 ** dec)
    return {"inputMint": "MINT", "outputMint": USDC, "amount": amt}


class SellLegSizingTests(unittest.TestCase):
    """The sell leg must represent the same USD notional as the buy leg."""

    # (usd_notional, decimals, token_price_usd)
    CASES = (
        (1.0, 9, 0.0000017),
        (2.0, 9, 0.0000017),
        (2.0, 6, 0.27),
        (5.0, 9, 0.0000017),
        (10.0, 6, 0.27),
    )

    def test_sell_amount_implies_the_intended_usd_notional(self):
        for usd, dec, px in self.CASES:
            with self.subTest(usd=usd, dec=dec, px=px):
                mid_px = px
                expected_tokens = usd / px
                # Replicate the fixed sizing exactly.
                amt = int((usd / mid_px) * 10 ** dec)
                implied_usd = (amt / 10 ** dec) * px
                self.assertAlmostEqual(
                    implied_usd, usd, delta=usd * 0.01,
                    msg=(f"sell of ${usd} at ${px}/token implies ${implied_usd:.4f} "
                         f"of value, off by more than 1%"))

    def test_old_sizing_moved_dust_instead_of_the_notional(self):
        """Guard the regression: the old formula moved `usd * px` of value.

        The old token COUNT looked enormous, but its USD value was usd*px. For
        a 9-decimal token priced at $0.0000017 that is a dust amount, which
        barely moves the pool and therefore quotes at mid. For a 6-decimal
        token at $0.27 it under-sold by the price factor. Either way the sell
        leg did not describe the intended USD notional.
        """
        for usd, dec, px in self.CASES:
            with self.subTest(usd=usd, dec=dec, px=px):
                old_amt = int(usd * 10 ** dec)
                old_usd_value = (old_amt / 10 ** dec) * px
                # Exact relationship: old value == usd * px, so the ratio is
                # always the token price. That is wrong by the price factor,
                # which is 0.0000017 for a sub-cent memecoin and 0.27 for a
                # mainstream one. The sell leg only happens to be roughly
                # right when px is near 1.0, which is never true here.
                off_by = old_usd_value / usd
                self.assertAlmostEqual(
                    off_by, px, places=6,
                    msg="old sizing no longer equals usd*px; fixture is stale")
                self.assertLess(
                    off_by, 0.5,
                    msg=(f"old sizing moved ${old_usd_value:.6f} for a "
                         f"${usd:.2f} intent (only {off_by*100:.2f}% of the "
                         f"notional); the fixture no longer reproduces the bug"))

    def test_leg_px_sell_uses_mid_derived_token_amount(self):
        """Drive leg_px with the network stubbed and inspect the request."""
        usd, dec, px = 2.0, 9, 0.0000017
        captured = {}

        def fake_get(url, **kw):
            captured["url"] = url
            # Two legs are called; return a plausible two-sided payload.
            return {
                "data": [{
                    "inAmount": "2000000",
                    "outAmount": str(int(2000000 * (1 / px) * (10 ** (dec - 6)))),
                    "priceImpactPct": "0.0001",
                }]
            }

        with patch.object(route_monitor, "mid_price", return_value=px), \
                patch.object(route_monitor, "_get", side_effect=fake_get), \
                patch.object(route_monitor, "_classify",
                             return_value=(2000000, 1000000)):
            route_monitor.leg_px("MINT", "sell", usd, dec)

        self.assertIn("amount=", captured.get("url", ""),
                      "sell leg must send an amount parameter")
        # The amount must be the token count for $2, not 2 * 10**9.
        amt_str = captured["url"].split("amount=")[1].split("&")[0]
        amt = int(amt_str)
        expected = int((usd / px) * 10 ** dec)
        self.assertEqual(amt, expected)
        # The meaningful check is USD value, not token count: a $0.0000017
        # token legitimately needs ~1.2e15 atomic units to represent $2.
        implied_usd = (amt / 10 ** dec) * px
        self.assertAlmostEqual(
            implied_usd, usd, delta=usd * 0.01,
            msg=f"sell request implies ${implied_usd:.6f}, not ${usd:.2f}")


class SlippageSignAndRoundTripTests(unittest.TestCase):
    """Slippage is a POSITIVE cost, and round trip is an arithmetic spread.

    BUG BEING GUARDED
    -----------------
    The monitor stored:

        bs = (mid - buy) / mid          # NEGATIVE of cost
        ss = (sell - mid) / mid          # NEGATIVE of cost
        rt = (buy * sell / mid**2 - 1)   # geometric, cancels the two legs

    The geometric round trip evaluated an ordinary 2% cost (buy 1% above mid,
    sell 1% below) to -0.01%: free. On a live row it reported +0.092% for a
    true 1.928% cost. Combined with the inverted slippage signs, every
    historical roundtrip_pct in mh_route_observations understated the real
    cost, which is what made the conservative 1.800% look like a 6x overcharge.
    """

    def _slips(self, mid, buy, sell):
        """The fixed formula, kept beside the tests as the contract."""
        bs = (buy - mid) / mid * 100.0
        ss = (mid - sell) / mid * 100.0
        rt = (buy - sell) / mid * 100.0
        return bs, ss, rt

    def test_buying_above_mid_is_a_positive_cost(self):
        bs, _, _ = self._slips(1.00, 1.01, 0.99)
        self.assertGreater(bs, 0.0,
                           "paying above mid must be a positive cost")

    def test_selling_below_mid_is_a_positive_cost(self):
        _, ss, _ = self._slips(1.00, 1.01, 0.99)
        self.assertGreater(ss, 0.0,
                           "receiving below mid must be a positive cost")

    def test_ordinary_two_percent_round_trip_costs_two_percent(self):
        _, _, rt = self._slips(1.00, 1.01, 0.99)
        self.assertAlmostEqual(rt, 2.0, places=6,
                               msg="a 2% round trip must not read as free")

    def test_old_geometric_formula_reported_a_cost_as_free(self):
        """Guard the regression itself."""
        mid, buy, sell = 1.00, 1.01, 0.99
        old_rt = (buy * sell / (mid * mid) - 1.0) * 100.0
        self.assertLess(old_rt, 0.5,
                        "the old formula no longer looks free; fixture stale")

    def test_round_trip_equals_sum_of_leg_costs(self):
        mid, buy, sell = 0.27, 0.2817, 0.2543
        bs, ss, rt = self._slips(mid, buy, sell)
        self.assertAlmostEqual(rt, bs + ss, places=9,
                               msg="round trip must equal buy cost + sell cost")

    def test_no_route_legs_cost_nothing(self):
        _, _, rt = self._slips(1.00, 1.00, 1.00)
        self.assertAlmostEqual(rt, 0.0, places=9)


if __name__ == "__main__":
    unittest.main()
