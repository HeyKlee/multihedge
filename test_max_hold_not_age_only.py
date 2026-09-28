"""Max-hold must be a missed-TP fallback, never an age-only exit.

Project ATLAS FINDING 009 (council Seat 4). AGENTS.md:65-67:

    "Max hold is a missed-TP execution fallback only. It may close after the timer only if
     the recorded peak already crossed TP but the TP sale did not complete. Age alone never
     closes a position. Stop loss remains unconditional."

`dynamic_shadow_scalper.py` already implements the guard. `paper.py` and
`mh_memecoin_trader.py` did not, so every paper position was force-closed at max hold
regardless of whether it ever approached target. The paper ledger is the evidence substrate
for the autotuner, so this systematically truncated the research base.

These tests pin the constitutional rule for both offenders.
"""
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

import paper
import mh_memecoin_trader

REPO = Path(__file__).resolve().parent


def _pos(entry: float, peak: float, opened_ago: float, side: str = "LONG") -> dict:
    """A position dict shaped like the rows paper.close_checks consumes."""
    return {
        "side": side,
        "entry_px": entry,
        "peak_px": peak,
        "open_ts": time.time() - opened_ago,
        "trail_armed": 0,
    }


class PaperMaxHoldRequiresPeakCrossedTP(unittest.TestCase):
    def test_age_alone_never_closes_a_position(self):
        """Past max hold, price flat, peak never reached TP: must HOLD, not close."""
        pos = _pos(entry=1.0, peak=1.0, opened_ago=paper.MAX_HOLD_S + 60)
        reason = paper.close_checks(pos, 1.0, {})
        self.assertIsNone(
            reason,
            f"position was force-closed after {paper.MAX_HOLD_S}s with no peak-crossed-TP "
            f"condition (reason={reason!r}); AGENTS.md:66 says age alone never closes a "
            "position",
        )

    def test_max_hold_fires_when_peak_crossed_tp(self):
        """Peak reached TP, price retraced, timer expired: the missed-TP case must close."""
        peak = paper.TP_PCT + 0.001          # peak is above target
        entry = 1.0
        pos = _pos(entry=entry, peak=entry * (1 + peak), opened_ago=paper.MAX_HOLD_S + 60)
        # Current price retraced below TP so take_profit does not fire first.
        px = entry * (1 + paper.TP_PCT * 0.5)
        reason = paper.close_checks(pos, px, {})
        self.assertEqual(
            reason, "max_hold",
            "a position whose peak already crossed TP must still be released by the "
            "max-hold timer; that is the missed-TP execution fallback the rule permits",
        )

    def test_stop_loss_remains_unconditional(self):
        """The guard must not weaken the stop: a deep loss exits immediately."""
        pos = _pos(entry=1.0, peak=1.0, opened_ago=1)
        reason = paper.close_checks(pos, 1.0 * (1 + paper.SL_PCT * 2), {})
        self.assertEqual(reason, "stop_loss")

    def test_take_profit_still_fires_normally(self):
        pos = _pos(entry=1.0, peak=1.0, opened_ago=1)
        reason = paper.close_checks(pos, 1.0 * (1 + paper.TP_PCT * 2), {})
        self.assertEqual(reason, "take_profit")

    def test_trail_stop_still_fires_normally(self):
        """Peak above the trail arm, price pulled back past the trail distance."""
        entry = 1.0
        arm_peak = entry * (1 + paper.TRAIL_ARM_PCT + 0.01)
        pos = _pos(entry=entry, peak=arm_peak, opened_ago=1)
        pos["trail_armed"] = 1
        px = arm_peak * (1 - paper.TRAIL_DIST_PCT - 0.001)
        reason = paper.close_checks(pos, px, {})
        self.assertEqual(reason, "trail_stop")


class ShortSideMaxHoldIsAlsoGuarded(unittest.TestCase):
    def test_short_position_age_alone_holds(self):
        pos = _pos(entry=1.0, peak=1.0, opened_ago=paper.MAX_HOLD_S + 60, side="SHORT")
        reason = paper.close_checks(pos, 1.0, {})
        self.assertIsNone(reason, "SHORT position force-closed on age alone")


class MemecoinTraderMaxHoldRequiresPeakCrossedTP(unittest.TestCase):
    def test_mh_memecoin_trader_does_not_close_on_age_alone(self):
        """mh_memecoin_trader.py had the same unguarded pattern at its max-hold branch."""
        source = (REPO / "mh_memecoin_trader.py").read_text(encoding="utf-8", errors="replace")
        # Locate the max-hold branch and require a peak-crossed-TP condition in it.
        import re
        offenders = []
        for m in re.finditer(r"return\s+[\"']max_hold[\"']", source):
            window = source[max(0, m.start() - 320):m.start()]
            window = window[window.rfind("if"):] if "if" in window else window
            if "peak" not in window:
                offenders.append(window.strip().splitlines()[-3:] if window else "")
        self.assertEqual(
            offenders, [],
            "mh_memecoin_trader returns 'max_hold' without a peak-crossed-TP guard:\n"
            + "\n".join(str(o) for o in offenders),
        )


if __name__ == "__main__":
    unittest.main()
