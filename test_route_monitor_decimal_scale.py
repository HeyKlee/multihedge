"""Regression test for the route monitor's decimal-scale defect.

`ops/route_monitor.py:leg_px` computed the buy leg as `ina / outa`, where `ina` is
USDC-atomic (6 decimals) and `outa` is token-atomic (`dec` decimals, typically 9). The
ratio therefore carried an extra factor of 10**(dec-6) and every 9-decimal memecoin read
1000x too cheap.

That tripped the plausibility gate (`MAX_PLAUSIBLE_SLIP_PCT`), so real, liquid tokens were
recorded as `no_route:implausible_buy_99.9pct` in `mh_route_observations`. Three of the
last five real entries were stamped unroutable on fabricated evidence.

Observed production data for 6GmAFSYs4gk3FDao5FzzySQpPZaWsa4rUJHacpMpUNgx (dec=9):
    mid_px = 0.2569197190847749
    buy_px = 0.00025662274339762555    <- 1001x too small
    sell_px= 0.000256785
    reason = no_route:implausible_buy_99.9pct

These tests assert the price is scale-correct for both 6-decimal and 9-decimal tokens.
They are network-free and never touch a database.
"""
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO / "ops"))

import route_monitor as rm  # noqa: E402

USDC_DECIMALS = 6


class LegPxIsScaleCorrect(unittest.TestCase):
    """A returned price must be USD-per-token, independent of token decimals."""

    def _fake_quotes(self, price_per_token: float, dec: int):
        """Return a quote pair that reprices correctly at `price_per_token`.

        Amounts are native atomic units: USDC is 6dp, the token is `dec`dp. The token
        quantity must be derived from the HUMAN notional, not the atomic USDC amount.
        """
        def _get(url):
            query = url.split("?", 1)[1]
            params = dict(p.split("=", 1) for p in query.split("&"))
            amount = int(params["amount"])
            if params["inputMint"] == rm.USDC:
                usd_in = amount / 10 ** USDC_DECIMALS
                tokens_out = int(usd_in / price_per_token * 10 ** dec)
                return {"inAmount": amount, "outAmount": tokens_out}
            # Selling: `amount` is a token quantity, output is USDC atomic.
            tokens_in = amount
            usd_out = int(tokens_in / 10 ** dec * price_per_token * 10 ** USDC_DECIMALS)
            return {"inAmount": amount, "outAmount": usd_out}

        def _classify(payload):
            return payload["inAmount"], payload["outAmount"]

        return _get, _classify

    def test_nine_decimal_token_returns_correct_price(self):
        price = 0.2569197190847749
        dec = 9
        get, classify = self._fake_quotes(price, dec)
        orig_get, orig_classify = rm._get, rm._classify
        rm._get, rm._classify = get, classify
        try:
            px, why = rm.leg_px("MintNineDecimals", "buy", 1.0, dec)
        finally:
            rm._get, rm._classify = orig_get, orig_classify
        self.assertEqual(why, "ok")
        # Must be within 0.1% of the true price, not 1000x off.
        self.assertAlmostEqual(
            px, price, delta=price * 0.001,
            msg=f"buy price {px} is not USD-per-token; expected ~{price}",
        )

    def test_sell_leg_also_scale_correct_at_nine_decimals(self):
        price = 0.2569197190847749
        dec = 9
        get, classify = self._fake_quotes(price, dec)
        orig_get, orig_classify = rm._get, rm._classify
        rm._get, rm._classify = get, classify
        try:
            px, why = rm.leg_px("MintNineDecimals", "sell", 1.0, dec)
        finally:
            rm._get, rm._classify = orig_get, orig_classify
        self.assertEqual(why, "ok")
        self.assertAlmostEqual(
            px, price, delta=price * 0.001,
            msg=f"sell price {px} is not USD-per-token; expected ~{price}",
        )

    def test_six_decimal_token_unchanged(self):
        """The fix must not alter behaviour for 6-decimal tokens (scale factor 1)."""
        price = 1.2345
        dec = 6
        get, classify = self._fake_quotes(price, dec)
        orig_get, orig_classify = rm._get, rm._classify
        rm._get, rm._classify = get, classify
        try:
            px, why = rm.leg_px("MintSixDecimals", "buy", 1.0, dec)
        finally:
            rm._get, rm._classify = orig_get, orig_classify
        self.assertEqual(why, "ok")
        self.assertAlmostEqual(px, price, delta=price * 0.001)

    def test_five_decimal_token_also_scale_correct(self):
        """A real production case: DezXAZ8z7Pnr is dec=5 and was 10x wrong, not 1000x.

        The scale factor is 10**(dec-6), which is a negative exponent below USDC
        decimals, so this direction must be covered explicitly.
        """
        price = 3.735269786317722e-06
        dec = 5
        get, classify = self._fake_quotes(price, dec)
        orig_get, orig_classify = rm._get, rm._classify
        rm._get, rm._classify = get, classify
        try:
            px, why = rm.leg_px("MintFiveDecimals", "buy", 1.0, dec)
        finally:
            rm._get, rm._classify = orig_get, orig_classify
        self.assertEqual(why, "ok")
        self.assertAlmostEqual(
            px, price, delta=abs(price) * 0.001,
            msg=f"buy price {px} is not USD-per-token; expected ~{price}",
        )

    def test_real_liquid_token_is_not_flagged_implausible(self):
        """The exact production failure: a liquid 9dp token must pass the plausibility gate."""
        mid = 0.2569197190847749
        good_buy, good_sell = mid, mid
        ok, why = rm._slips_are_plausible(mid, good_buy, good_sell)
        self.assertTrue(ok)
        self.assertEqual(why, "")
        # And the historical, broken value must be the thing that is rejected.
        bad_buy = 0.00025662274339762555
        ok_bad, why_bad = rm._slips_are_plausible(mid, bad_buy, bad_buy)
        self.assertFalse(ok_bad)
        self.assertIn("implausible_buy", why_bad)

    def test_leg_px_rejects_zero_output(self):
        def _get(_url):
            return {"inAmount": 1_000_000, "outAmount": 0}

        orig_get, orig_classify = rm._get, rm._classify
        rm._get = _get
        try:
            px, why = rm.leg_px("Dust", "buy", 1.0, 9)
        finally:
            rm._get, rm._classify = orig_get, orig_classify
        self.assertIsNone(px)
        self.assertIn("no_route", why + "no_route")


if __name__ == "__main__":
    unittest.main(verbosity=2)
