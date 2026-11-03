"""Tests for the reusable goal harness. Uses the fake claude, so no tokens are spent."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from selfcall import engine as E  # noqa: E402
from selfcall.goals import Goal, run_goal  # noqa: E402

FAKE = [sys.executable, str(ROOT / "tests" / "fake_claude.py")]


class GoalTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        import os
        os.environ["SELFCALL_FAKE_LOG"] = str(self.home / "argv.log")

    def tearDown(self):
        self._tmp.cleanup()

    def engine(self, **limits):
        return E.Engine(home=self.home, claude_cmd=FAKE, limits=limits)

    def test_goal_met_when_verifier_passes_after_rounds(self):
        eng = self.engine()

        def verify(home):
            n = eng.store.totals()[0]
            return n >= 2, f"{n} invocations so far"

        res = run_goal(eng, Goal("count", "make two invocations", verify, max_rounds=5))
        self.assertEqual(res.status, "met")
        self.assertEqual(res.rounds, 2)
        self.assertEqual(len(res.history), 2)

    def test_advance_events_become_demands_each_round(self):
        eng = self.engine()
        seen = []

        def advance(home):
            seen.append(1)
            return [("trade_closed", {"id": len(seen), "result": "loss", "r_multiple": -1.0})] if len(seen) == 1 else []

        def verify(home):
            return len(seen) >= 2, f"rounds advanced: {len(seen)}"

        res = run_goal(eng, Goal("events", "react to events", verify, advance=advance, max_rounds=5))
        self.assertEqual(res.status, "met")
        sources = [r["source"] for r in eng.store.recent(10)]
        self.assertIn("event:trade_closed", sources)
        self.assertIn("goal:events", sources)

    def test_round_cap_gives_not_met(self):
        eng = self.engine()
        res = run_goal(eng, Goal("never", "impossible", lambda h: (False, "still no"), max_rounds=2))
        self.assertEqual(res.status, "not_met")
        self.assertEqual(res.rounds, 2)

    def test_stop_file_halts_the_goal(self):
        eng = self.engine()
        eng.stop_file.write_text("stop\n")
        res = run_goal(eng, Goal("halted", "x", lambda h: (False, "no"), max_rounds=3))
        self.assertEqual(res.status, "halted")
        self.assertEqual(eng.store.totals()[0], 0)


if __name__ == "__main__":
    unittest.main()
