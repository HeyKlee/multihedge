"""Prevent documentation from restating policy numbers (Project ATLAS FINDING 007).

`AGENTS.md` is this project's constitution and stated MEME policy of +20% TP / -10% SL /
900s, while the running code executes +1.5% / -1.0% / 1800s — a 13x divergence on take
profit. Documentation that repeats a number becomes a rival source of truth, which is
precisely what this project exists to eliminate.

The durable fix is structural: the document should point at the resolver, not restate the
value. These tests assert that, so the contradiction cannot silently return.
"""
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent

# The one module allowed to own Xora-Survival exit parameters (AGENTS.md "Important
# boundaries": live_inventory.py is the shared source; do not add independent constants).
CANONICAL_EXIT_OWNER = "live_inventory.py"
CANONICAL_OWNER_NAME = "domain/policy/resolver.py"

# Policy magnitudes that must never be restated as prose numbers outside the owner.
POLICY_NUMBER_RE = re.compile(
    r"(\+?|-)?\s*(\d+(?:\.\d+)?)\s*%\s*(take profit|take-profit|stop loss|stop-loss|"
    r"max[- ]hold|max_hold)",
    re.I,
)
# A max-hold stated as a duration in prose ("900 second", "six-hour").
DURATION_RE = re.compile(r"(\d{2,5})\s*(second|sec|minute|min|hour)s?\b", re.I)


class ConstitutionDoesNotRestatePolicy(unittest.TestCase):
    def test_agents_md_has_no_policy_percent_literals(self):
        text = (REPO / "AGENTS.md").read_text(encoding="utf-8", errors="replace")
        offenders = []
        for i, line in enumerate(text.splitlines(), 1):
            if not line.strip().lstrip("-*").strip():
                continue
            m = POLICY_NUMBER_RE.search(line)
            if m:
                offenders.append(f"AGENTS.md:{i}: {line.strip()[:120]}")
        self.assertEqual(
            offenders, [],
            "AGENTS.md restates a policy number in prose, which creates a rival source of "
            "truth (FINDING 007). State the owner and point at the resolver instead:\n  "
            + "\n  ".join(offenders),
        )

    def test_agents_md_has_no_policy_duration_literals(self):
        text = (REPO / "AGENTS.md").read_text(encoding="utf-8", errors="replace")
        offenders = []
        for i, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if not stripped:
                continue
            # Deployment/telemetry durations are not trading policy; only flag lines that
            # are explicitly about max-hold.
            if "max-hold" not in stripped.lower() and "max hold" not in stripped.lower():
                continue
            m = DURATION_RE.search(stripped)
            if m:
                offenders.append(f"AGENTS.md:{i}: {stripped[:120]}")
        self.assertEqual(
            offenders, [],
            "AGENTS.md restates the max-hold duration in prose (FINDING 007):\n  "
            + "\n  ".join(offenders),
        )


class ExitParametersHaveOneOwner(unittest.TestCase):
    """Exit thresholds must be defined once, in the canonical owner module."""

    EXIT_CONSTANTS = (
        "MEME_TAKE_PROFIT_PCT", "MEME_STOP_LOSS_PCT", "MEME_MAX_HOLD_SECONDS",
        "MEME_TRAIL_ARM_PCT", "MEME_TRAIL_DISTANCE_PCT",
        "SERIOUS_TAKE_PROFIT_PCT", "SERIOUS_STOP_LOSS_PCT", "SERIOUS_MAX_HOLD_SECONDS",
    )

    def test_exit_constants_defined_only_in_the_canonical_owner(self):
        owners = {CANONICAL_EXIT_OWNER, CANONICAL_OWNER_NAME}
        offenders = []
        for path in sorted(REPO.glob("*.py")):
            if path.name in owners or path.name.startswith("test_"):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for i, line in enumerate(text.splitlines(), 1):
                for const in self.EXIT_CONSTANTS:
                    # Definition sites only: "NAME = <number>".
                    if re.search(rf"^\s*{const}\s*=\s*-?\d", line):
                        offenders.append(f"{path.name}:{i}: {const} = {line.strip()[:80]}")
        self.assertEqual(
            offenders, [],
            "Xora-Survival exit constants are defined outside the canonical owner "
            f"({CANONICAL_EXIT_OWNER}); each must be defined exactly once (AGENTS.md "
            f"'Important boundaries', FINDING 004/007):\n  " + "\n  ".join(offenders),
        )

    def test_canonical_owner_still_defines_them(self):
        """Guard against the test passing simply because the owner emptied the file."""
        text = (REPO / CANONICAL_EXIT_OWNER).read_text(encoding="utf-8", errors="replace")
        missing = [c for c in self.EXIT_CONSTANTS if not re.search(rf"^\s*{c}\s*=", text, re.M)]
        self.assertEqual(
            missing, [],
            f"{CANONICAL_EXIT_OWNER} no longer defines {missing}; the exit owner moved or "
            "was deleted without updating the registry.",
        )


if __name__ == "__main__":
    unittest.main()
