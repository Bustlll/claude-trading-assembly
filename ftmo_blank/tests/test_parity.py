"""TRAINING vs LIVE: the same trading logic, or a listed reason why not (tools/parity_check.py).

Fails on any drift that is not in KNOWN_DRIFT, and on a KNOWN_DRIFT entry that is no longer drifting --
so a change to one path that is not made (or consciously listed) on the other cannot slip through."""
import sys
import unittest
from pathlib import Path

HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOME / "tools"))
import parity_check as PC  # noqa: E402


class ParityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = PC.run_all()
        cls.v = PC.verdict(cls.rows)

    def test_no_check_failed_to_run(self):
        self.assertEqual([r for r in self.rows if r["check"].endswith(".error")], [])

    def test_the_move_screen_and_the_window_are_identical(self):
        same = {r["check"] for r in self.rows if r["status"] == "SAME"}
        for c in ("scan.candidates", "scan.threshold_pct", "scan.lookback_m5", "scan.max_per_event", "window"):
            self.assertIn(c, same)

    def test_no_new_drift(self):
        self.assertEqual(self.v["new_drift"], [], "training and live diverged: align them or list it in KNOWN_DRIFT")

    def test_known_drift_list_is_current(self):
        self.assertEqual(self.v["known_drift_now_same"], [], "these are aligned now: remove them from KNOWN_DRIFT")


if __name__ == "__main__":
    unittest.main()
