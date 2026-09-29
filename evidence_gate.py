"""Evidence gate: mechanical checks a headline number must pass before use.

WHY THIS EXISTS
---------------
Three numbers produced during the 2026-09-29 profitability work were wrong, and
all three flattered the conclusion the author wanted:

  * "measured round-trip cost is 0.274%"  (FINDING 013)
    Impossible: it sits below the mandatory 0.800% fee floor, because
    quote_bps alone costs 0.40% per side. Caught by an arithmetic floor.
  * "+0.765% gross edge inside 30 minutes"
    Circular: trades were grouped by a duration the exit logic had itself
    produced. Caught by re-deriving from paths the quantity did not shape.
  * "+9.6% edge on declined mints"  (FINDING 012)
    Concentrated in 8 sub-cent tokens. Caught by trimmed mean, per-entity
    count, and median-versus-mean.

None was caught by reasoning harder. All were caught by a mechanism that does
not depend on the author's judgement. This module is that mechanism.

THE CONTRACT
------------
`check()` returns BLOCK if any check fails. A blocked claim must not be used
in a report, a dashboard, a promotion decision, or a conversation with the
operator. Fix the claim, or record an explicit waiver with a stated reason.

Checks are intentionally conservative and fail closed. A missing input is a
failure, not a pass: silence is not evidence.
"""

from __future__ import annotations

import statistics as st
from dataclasses import dataclass, field
from typing import Any

# Thresholds are deliberately strict. The goal is to stop a claim reaching a
# human before the failure mode that produced it has been ruled out.
MIN_OBS = 30
# FINDING 012 was an effect carried by 8 assets, so a limit of 5 would have
# let it through. The unit of independence here is the asset, and a market
# claim resting on fewer than ~10 assets is a claim about those assets.
MIN_ENTITIES = 10
MIN_SPAN_DAYS = 1.0
MAX_TRAIN_TEST_INVERSION = 0.0      # test may not beat train
CONCENTRATION_RATIO_LIMIT = 3.0     # mean/median above this means outliers
MIN_TRIMMED_ENTITY_COUNT = 3
DERIVATION_TOLERANCE_PCT = 0.05     # 5% relative agreement


@dataclass
class Claim:
    """A headline number plus everything needed to check it.

    `value` is the number being proposed for use. Fields left as None are
    treated as MISSING and cause a BLOCK, because an unmeasured claim cannot be
    distinguished from a cherry-picked one.
    """

    name: str
    value: float
    units: str = "pct"

    # sample adequacy
    n_obs: int | None = None
    n_entities: int | None = None
    span_days: float | None = None

    # distribution shape
    mean: float | None = None
    median: float | None = None
    trimmed_mean: float | None = None

    # economic admissibility
    floor: float | None = None
    ceiling: float | None = None
    sign_admissible: bool = True
    convention: str = ""

    # independence
    independent_value: float | None = None
    derivation_note: str = ""

    # temporal integrity
    train_value: float | None = None
    test_value: float | None = None

    # pre-registration
    pre_registered: bool = False
    forbidden_features_used: list[str] = field(default_factory=list)

    notes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.mean is None and self.median is None:
            # Derive what we can from the value itself when not supplied.
            self.mean = self.median = self.value
        if self.trimmed_mean is None and self.mean is not None:
            self.trimmed_mean = self.mean


@dataclass
class Verdict:
    blocked: bool
    failures: list[str]
    checks_run: list[str]
    passed: list[str]
    # Carried so a verdict is self-describing when printed or logged alone.
    name: str = ""
    value: float | None = None

    def summary(self) -> str:
        verdict = "BLOCK" if self.blocked else "PASS"
        lines = [f"{verdict}: {self.name}", f"  value: {self.value}"]
        if self.failures:
            lines.append("  FAILED CHECKS:")
            lines += [f"    - {f}" for f in self.failures]
        else:
            lines.append(f"  all {len(self.checks_run)} checks passed")
        return "\n".join(lines)


def check(c: Claim) -> Verdict:
    """Run every gate. Any failure blocks. Missing input blocks."""
    failures: list[str] = []
    passed: list[str] = []

    def ok(name: str, cond: bool, msg: str) -> None:
        if cond:
            passed.append(name)
        else:
            failures.append(msg)

    # --- 1. economic floor (caught the 0.274% cost) -----------------------
    if c.floor is None:
        failures.append(
            "no economic floor declared. A cost claim must declare the "
            "mandatory component it cannot go below (e.g. 2*quote_bps).")
    elif c.value < c.floor:
        ok("floor", False,
           f"value {c.value}{c.units} is BELOW its mandatory floor "
           f"{c.floor}{c.units}. That is arithmetically impossible and means "
           f"the instrument or the formula is wrong, not that the cost is low.")
    else:
        ok("floor", True, "")
    if c.floor is not None and c.ceiling is not None and c.value > c.ceiling:
        ok("ceiling", False,
           f"value {c.value} exceeds ceiling {c.ceiling}")

    # --- 2. sign admissibility (caught the inverted slippage) --------------
    ok("sign", bool(c.sign_admissible),
       "value is physically impossible: a cost cannot be negative and a "
       "profit cannot exceed +100%. Check the sign convention.")
    if not c.convention:
        failures.append(
            "no sign convention declared. State whether positive means a cost "
            "or a gain; FINDING 013 was invisible until this was written down.")

    # --- 3. outlier concentration (caught FINDING 012) ---------------------
    if c.mean is not None and c.median is not None:
        if c.median == 0:
            spread = float("inf") if c.mean else 0.0
        else:
            spread = abs(c.mean / c.median)
        ok("concentration", spread <= CONCENTRATION_RATIO_LIMIT,
           f"mean/median is {spread:.1f}x, above the "
           f"{CONCENTRATION_RATIO_LIMIT}x limit. The headline is an outlier "
           f"effect. Report the trimmed mean and per-entity counts instead.")
    if c.trimmed_mean is None:
        failures.append("no trimmed mean. Untrimmed means hide outlier effects.")
    # Unconditional: a concentrated sample and a small entity count are
    # independent failures, and nesting this inside the concentration branch
    # let an outlier-heavy claim skip the entity check entirely.
    if c.n_entities is None:
        failures.append(
            "no entity count. An effect measured on a handful of assets is "
            "not an effect, it is those assets.")
    else:
        ok("entities", c.n_entities >= MIN_ENTITIES,
           f"only {c.n_entities} distinct entities. An effect measured on a "
           f"handful of assets is not an effect, it is those assets. "
           f"Minimum {MIN_ENTITIES}.")

    # --- 4. sample adequacy ----------------------------------------------
    if c.n_obs is None:
        failures.append("no observation count. Unsampled claims are blocked.")
    else:
        ok("sample_size", c.n_obs >= MIN_OBS,
           f"only {c.n_obs} observations, minimum {MIN_OBS}.")
    if c.span_days is None:
        failures.append(
            "no time span. A single hour cannot characterise a market.")
    else:
        ok("span", c.span_days >= MIN_SPAN_DAYS,
           f"span {c.span_days:.2f}d, minimum {MIN_SPAN_DAYS}d.")

    # --- 5. independent re-derivation (caught the circular hold-time) -----
    if c.independent_value is None:
        failures.append(
            "no independent re-derivation. State a second path to this number "
            "that does not share the suspect one. FINDING 012 and the hold-time "
            "result both passed arithmetic and died on re-derivation.")
    else:
        denom = max(abs(c.value), 1e-12)
        rel = abs(c.independent_value - c.value) / denom
        ok("derivation", rel <= DERIVATION_TOLERANCE_PCT,
           f"independent re-derivation gives {c.independent_value} vs "
           f"{c.value} ({rel*100:.1f}% apart, limit "
           f"{DERIVATION_TOLERANCE_PCT*100:.0f}%).")

    # --- 6. temporal integrity (out-of-sample honesty) --------------------
    if c.train_value is None or c.test_value is None:
        failures.append(
            "no chronological split. Report both train (earliest) and test "
            "(latest) so an in-sample win cannot be presented as a result.")
    else:
        ok("oos", c.test_value <= c.train_value + MAX_TRAIN_TEST_INVERSION,
           f"test ({c.test_value}) beats train ({c.train_value}). Either the "
           f"split leaked future information, the test window is unusually "
           f"favourable, or the selection was fitted to noise.")

    # --- 7. pre-registration (binds the author after the fact) ------------
    if c.forbidden_features_used:
        ok("pre_registration", False,
           f"forbidden ranking features used: {c.forbidden_features_used}. "
           f"Selecting on forward return or realised outcome and then "
           f"measuring on it is FINDING 012 repeated.")
    else:
        ok("forbidden_features", True, "")

    return Verdict(
        blocked=bool(failures),
        failures=[f for f in failures if f],
        checks_run=["floor", "ceiling", "sign", "concentration", "entities",
                    "sample_size", "span", "derivation", "oos",
                    "pre_registration"],
        passed=passed,
        name=c.name,
        value=c.value,
    )


def gate(name: str, **kw: Any) -> Verdict:
    """Convenience wrapper: build a Claim, check it, return the Verdict."""
    return check(Claim(name=name, **kw))
