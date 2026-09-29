#!/usr/bin/env python3
"""Does the evidence gate catch the three errors actually made on 2026-09-29?

This is the acceptance test for the gate. A gate that has never been shown to
reject a real, known-bad number is a gate that might be decorative, so each
case below is the ACTUAL claim that was made during the profitability work,
reconstructed with the data available at the time it was made.

Each case asserts the gate BLOCKS, and asserts on the specific failure that
must fire, so the test fails if the mechanism stops catching its own bug.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from evidence_gate import Claim, check  # noqa: E402


class GateCatchesTheCostClaimTests(unittest.TestCase):
    """The 0.274% round-trip cost (FINDING 013).

    The claim: measured friction is 0.274%, versus a canonical 1.800%. That
    looked like a 6x overcharge and was the basis for proposing a lower charge.
    It was impossible: quote_bps alone is 0.40% per side, so 0.80% round trip
    is mandatory before any slippage at all.
    """

    def test_gate_blocks_cost_below_the_mandatory_fee_floor(self):
        v = check(Claim(
            name="measured round-trip cost",
            value=0.274,
            floor=0.80,              # 2 * quote_bps(0.40%) per side
            convention="positive = cost",
            n_obs=249, n_entities=35, span_days=2.3,
            mean=0.419, median=0.274, trimmed_mean=0.30,
            independent_value=0.30,
            train_value=0.28, test_value=0.25,
        ))
        self.assertTrue(v.blocked, "the gate let an impossible cost through")
        self.assertTrue(any("floor" in f.lower() for f in v.failures),
                        f"floor check did not fire: {v.failures}")

    def test_floor_failure_is_explicit_about_arithmetic(self):
        v = check(Claim(
            name="cost", value=0.05, floor=0.80, convention="cost",
            n_obs=249, n_entities=35, span_days=2.3,
            mean=0.4, median=0.4, trimmed_mean=0.4,
            independent_value=0.4, train_value=0.4, test_value=0.4,
        ))
        floor_fails = [f for f in v.failures if "floor" in f.lower()]
        self.assertTrue(floor_fails)
        self.assertIn("impossible", floor_fails[0].lower(),
                      "the failure must say the number is impossible, not "
                      "merely that it is low")


class GateCatchesTheCircularClaimTests(unittest.TestCase):
    """The +0.765% gross edge inside 30 minutes.

    The claim: trades closed inside 30 minutes had +0.765% mean gross, with a
    bootstrap 95% CI of [+0.021%, +1.544%]. It was circular: trades were
    grouped by a duration that the exit logic had itself produced, so winners
    looked short because winners exit fast. A proper re-derivation under rules
    that did not choose the duration gave 0 of 140 configurations profitable.
    """

    def test_gate_blocks_a_claim_with_no_independent_derivation(self):
        v = check(Claim(
            name="gross edge for trades held under 30m",
            value=0.765,
            floor=None, ceiling=None,
            convention="positive = gain",
            n_obs=284, n_entities=35, span_days=18.0,
            mean=0.765, median=0.482, trimmed_mean=0.70,
            independent_value=None,        # <- the omission
            train_value=0.80, test_value=0.74,
        ))
        self.assertTrue(v.blocked)
        self.assertTrue(any("re-deriv" in f.lower() for f in v.failures),
                        f"re-derivation check did not fire: {v.failures}")

    def test_gate_blocks_when_derivation_contradicts_the_claim(self):
        v = check(Claim(
            name="hold-time edge",
            value=0.765,
            floor=None, convention="positive = gain",
            n_obs=284, n_entities=35, span_days=18.0,
            mean=0.765, median=0.482, trimmed_mean=0.70,
            independent_value=-0.05,        # what the replay actually found
            train_value=0.80, test_value=0.74,
        ))
        self.assertTrue(v.blocked)
        self.assertTrue(any("independent re-derivation" in f.lower()
                            for f in v.failures))


class GateCatchesTheOutlierClaimTests(unittest.TestCase):
    """The +9.6% edge on declined mints (FINDING 012).

    The claim: mints the gate declined returned +10.6% on the 1h horizon vs
    +0.52% for mints it bought. The raw bootstrap was positive. Trimmed of 61
    observations above +50%, the declined group returned -0.123%, worse than
    bought, and the movers were 8 sub-cent tokens.
    """

    def test_gate_blocks_outlier_concentration(self):
        v = check(Claim(
            name="declined-mint 1h edge",
            value=9.592,
            floor=None, convention="positive = gain",
            n_obs=594, n_entities=28, span_days=9.0,
            mean=9.592, median=2.889, trimmed_mean=-0.123,
            independent_value=9.5,          # bootstrap agrees on the RAW mean
            train_value=9.6, test_value=9.5,
        ))
        self.assertTrue(v.blocked)
        self.assertTrue(any("mean/median" in f for f in v.failures),
                        f"concentration check did not fire: {v.failures}")

    def test_trimmed_mean_contradicting_the_headline_is_caught(self):
        """The trimmed mean is the honest number; the headline is not."""
        v = check(Claim(
            name="declined-mint 1h edge",
            value=9.592,
            floor=None, convention="positive = gain",
            n_obs=594, n_entities=28, span_days=9.0,
            mean=9.592, median=2.889, trimmed_mean=-0.123,
            independent_value=-0.123,      # re-derivation gives the truth
            train_value=9.6, test_value=9.5,
        ))
        self.assertTrue(v.blocked)
        self.assertTrue(any("independent" in f.lower() for f in v.failures))

    def test_few_entities_blocks_even_with_a_huge_sample(self):
        """594 observations across 8 assets is not 594 observations."""
        v = check(Claim(
            name="edge from eight sub-cent tokens",
            value=12.444,
            floor=None, convention="positive = gain",
            n_obs=594, n_entities=8, span_days=9.0,
            mean=12.444, median=2.605, trimmed_mean=2.0,
            independent_value=12.0,
            train_value=12.0, test_value=12.0,
        ))
        self.assertTrue(v.blocked)
        self.assertTrue(any("distinct entities" in f for f in v.failures),
                        f"entity check did not fire: {v.failures}")


class GateFailsClosedTests(unittest.TestCase):
    """Silence is not evidence. An unmeasured claim is blocked."""

    def test_claim_with_no_data_at_all_is_blocked(self):
        v = check(Claim(name="something feels profitable", value=5.0))
        self.assertTrue(v.blocked)
        self.assertGreaterEqual(len(v.failures), 5,
                                "a bare number must trip every check")

    def test_single_hour_sample_is_blocked(self):
        v = check(Claim(
            name="one-hour sample", value=0.5, floor=None,
            convention="gain",
            n_obs=500, n_entities=10, span_days=0.04,
            mean=0.5, median=0.5, trimmed_mean=0.5,
            independent_value=0.5, train_value=0.5, test_value=0.4,
        ))
        self.assertTrue(v.blocked)
        self.assertTrue(any("span" in f.lower() for f in v.failures))

    def test_missing_oos_split_is_blocked(self):
        v = check(Claim(
            name="no holdout", value=0.5, floor=None, convention="gain",
            n_obs=100, n_entities=10, span_days=3.0,
            mean=0.5, median=0.5, trimmed_mean=0.5,
            independent_value=0.5,
        ))
        self.assertTrue(v.blocked)
        self.assertTrue(any("chronological" in f.lower() for f in v.failures))

    def test_inverted_train_test_is_flagged(self):
        v = check(Claim(
            name="inverted split", value=0.5, floor=None, convention="gain",
            n_obs=100, n_entities=10, span_days=3.0,
            mean=0.5, median=0.5, trimmed_mean=0.5,
            independent_value=0.5,
            train_value=-1.0, test_value=+2.0,
        ))
        self.assertTrue(v.blocked)
        self.assertTrue(any("beats train" in f for f in v.failures))


class GatePreRegistrationTests(unittest.TestCase):
    """The binding control for the entry concentration analysis."""

    def test_forbidden_ranking_feature_blocks(self):
        v = check(Claim(
            name="top-decile setups by forward return",
            value=2.0, floor=None, convention="gain",
            n_obs=200, n_entities=20, span_days=10.0,
            mean=2.0, median=2.0, trimmed_mean=2.0,
            independent_value=2.0, train_value=2.0, test_value=1.8,
            pre_registered=True,
            forbidden_features_used=["return_5m_pct", "realized_pct"],
        ))
        self.assertTrue(v.blocked)
        self.assertTrue(any("forbidden ranking features" in f
                            for f in v.failures),
                        f"pre-registration check did not fire: {v.failures}")


class GateAdmitsGoodEvidenceTests(unittest.TestCase):
    """The gate must not be a rubber stamp in the other direction either."""

    def test_a_well_evidenced_claim_passes(self):
        """A defensible cost claim, above the floor and properly evidenced.

        The first draft of this fixture used 0.176%, which the gate correctly
        rejected for sitting below the 0.80% mandatory fee floor. That was the
        gate doing its job on a value I had wanted to believe, which is the
        whole reason it exists. The figure here is a plausible measured cost
        that clears the floor.
        """
        v = check(Claim(
            name="post-fix round-trip cost, two-sided, 7 days",
            value=1.05, floor=0.80, convention="positive = cost",
            n_obs=1200, n_entities=20, span_days=7.4,
            mean=1.18, median=1.02, trimmed_mean=1.09,
            independent_value=1.04,
            train_value=1.09, test_value=1.02,
        ))
        self.assertFalse(v.blocked, f"good evidence was blocked: {v.failures}")


if __name__ == "__main__":
    unittest.main()
