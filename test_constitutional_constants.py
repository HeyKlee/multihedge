"""Single-owner contract for the constitutional treasury constants.

Project ATLAS FINDING 010. `AGENTS.md` declares `survival_policy.py` the sole owner of the
immutable NZ$60 protected floor and NZ$40 death threshold. `execution_policy.py` defined its
own `PROTECTED_FLOOR_NZD = Decimal("60.00")` literal instead of importing it, so the
pre-trade execution gate — the last check before a signed transaction — enforced a copy.

The two agreed at the time, so nothing was observably wrong. That is the failure mode this
project treats as a defect: two sources for one fact, where a future recalibration updates
the documented owner and leaves the order gate enforcing a stale value with no error.

These tests fail on duplication and pass once there is exactly one definition.
"""
import ast
import re
import unittest
from decimal import Decimal
from pathlib import Path

REPO = Path(__file__).resolve().parent

DECLARED_OWNER = "survival_policy.py"
CONSUMERS = ("execution_policy.py", "live_bridge.py", "live_inventory.py", "mh_dash.py")

# Constitutional constants that must have exactly one definition site.
CONSTITUTIONAL_CONSTANTS = (
    "PROTECTED_FLOOR_NZD",
    "DEATH_THRESHOLD_NZD",
)


def definition_sites(constant: str) -> list[tuple[str, int]]:
    """Every module that ASSIGNS the constant, i.e. owns a literal definition."""
    sites: list[tuple[str, int]] = []
    for path in sorted(REPO.glob("*.py")):
        if path.name.startswith("test_"):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
        except (SyntaxError, OSError):
            continue
        for node in ast.walk(tree):
            targets = []
            line = getattr(node, "lineno", 0)
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id == constant:
                    sites.append((path.name, line))
    return sites


class ConstitutionalConstantsHaveOneOwner(unittest.TestCase):
    def test_floor_and_threshold_are_defined_exactly_once(self):
        for constant in CONSTITUTIONAL_CONSTANTS:
            with self.subTest(constant=constant):
                sites = definition_sites(constant)
                self.assertEqual(
                    len(sites), 1,
                    f"{constant} is defined in {len(sites)} places: {sites}. "
                    f"AGENTS.md names {DECLARED_OWNER} the sole owner; a second literal is a "
                    "duplicate source of truth on a safety-critical constant.",
                )

    def test_the_single_owner_is_the_declared_owner(self):
        for constant in CONSTITUTIONAL_CONSTANTS:
            sites = definition_sites(constant)
            if sites:
                self.assertEqual(
                    [f for f, _ in sites], [DECLARED_OWNER],
                    f"{constant} is defined in {sites}, not in the declared owner "
                    f"{DECLARED_OWNER}",
                )

    def test_no_consumer_redefines_the_floor_as_a_literal(self):
        """A consumer must import the value, never restate it."""
        offenders = []
        for name in CONSUMERS:
            path = REPO / name
            if not path.is_file():
                continue
            for i, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                stripped = line.strip()
                if not stripped.startswith(("PROTECTED_FLOOR_NZD", "DEATH_THRESHOLD_NZD")):
                    continue
                # An assignment is a redefinition; a bare import is fine.
                if "=" in stripped and "import" not in stripped:
                    offenders.append(f"{name}:{i}: {stripped[:90]}")
        self.assertEqual(
            offenders, [],
            "a consumer restates a constitutional constant instead of importing it:\n  "
            + "\n  ".join(offenders),
        )

    def test_execution_policy_agrees_with_the_owner_by_construction(self):
        """The point of the fix: equality must follow from the import, not coincidence."""
        import execution_policy
        import survival_policy
        self.assertIs(
            execution_policy.PROTECTED_FLOOR_NZD, survival_policy.PROTECTED_FLOOR_NZD,
            "execution_policy must share the identical Decimal object from survival_policy; "
            "a separate Decimal('60.00') is a second source even when numerically equal",
        )
        self.assertEqual(
            execution_policy.PROTECTED_FLOOR_NZD, Decimal("60.00"),
            "the shared floor value itself changed; that requires an explicit, approved "
            "policy decision, not a code edit",
        )
        self.assertEqual(survival_policy.DEATH_THRESHOLD_NZD, Decimal("40.00"))

    def test_declared_owner_is_actually_imported_by_the_gate(self):
        """Guards against the floor becoming disconnected from the enforcement path."""
        text = (REPO / "execution_policy.py").read_text(encoding="utf-8", errors="replace")
        self.assertRegex(
            text, r"from\s+survival_policy\s+import|import\s+survival_policy",
            "execution_policy no longer imports survival_policy, so the documented owner of "
            "the protected floor is disconnected from the pre-trade gate",
        )


if __name__ == "__main__":
    unittest.main()
