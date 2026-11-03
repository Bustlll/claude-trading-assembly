"""tools/blindcheat2.py: the walk that grades a trade, and the arithmetic the dashboard shows. No model calls."""
import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import blindcheat2 as B  # noqa: E402

MON = int(dt.datetime(2026, 3, 2, 10, tzinfo=dt.timezone.utc).timestamp())
utc = lambda t: dt.datetime.fromtimestamp(t, dt.timezone.utc)


def bars(path):
    return [{"t": MON + k * 300, "o": o, "h": h, "l": l, "c": c} for k, (o, h, l, c) in enumerate(path)]


class WalkTest(unittest.TestCase):
    def go(self, path, sg=1):
        return B.walk_full(bars([(100, 100, 100, 100)] + path), 0, sg, 1.0, 2.0, 0.0, utc)

    def test_target_first(self):
        w = self.go([(100, 101, 100, 101), (101, 102.5, 101, 102)])
        self.assertEqual((w["outcome"], w["R"]), ("tp", 2.0))

    def test_stop_then_target_is_a_loss_that_says_the_idea_was_right(self):
        w = self.go([(100, 100, 98.5, 99), (99, 102.5, 99, 102)])
        self.assertEqual((w["outcome"], w["R"], w["mae"]), ("sl_then_tp", -1.0, 1.5))

    def test_stop_and_never_the_target(self):
        self.assertEqual(self.go([(100, 100, 98, 98.5)])["outcome"], "sl")

    def test_a_bar_touching_both_counts_as_the_stop(self):
        w = self.go([(100, 103, 98, 100)])
        self.assertEqual((w["outcome"] in ("sl", "sl_then_tp"), w["R"]), (True, -1.0))

    def test_a_sell_mirrors(self):
        w = self.go([(100, 100, 97.5, 98)], sg=-1)
        self.assertEqual((w["outcome"], w["R"]), ("tp", 2.0))

    def test_flat_before_the_weekend(self):
        fri = int(dt.datetime(2026, 3, 6, 20, tzinfo=dt.timezone.utc).timestamp())
        b = [{"t": fri - 300, "o": 100, "h": 100, "l": 100, "c": 100}, {"t": fri, "o": 100, "h": 100.5, "l": 100, "c": 100.5},
             {"t": fri + 300, "o": 100.5, "h": 103, "l": 100.5, "c": 103}]
        w = B.walk_full(b, 0, 1, 1.0, 2.0, 0.0, utc)
        self.assertEqual((w["R"], w["outcome"]), (0.5, "tp"))   # R stopped at the flatten; the walk still says the target came


class CostsTest(unittest.TestCase):
    def test_a_swap_night_is_counted_at_new_york_17_and_the_triple_day_three_times(self):
        u = lambda d, h: dt.datetime(2026, 3, d, h, tzinfo=dt.timezone.utc)
        self.assertEqual(B.rollovers(u(2, 10), u(2, 20), 3), 0)      # Monday, before 17:00 New York (21:00 UTC)
        self.assertEqual(B.rollovers(u(2, 10), u(3, 10), 3), 1)      # across Monday's rollover
        self.assertEqual(B.rollovers(u(4, 10), u(5, 10), 3), 3)      # Wednesday's is the triple one

    def test_a_stop_too_wide_for_the_minimum_lot_is_refused(self):
        self.assertIsNone(B.size("XAUUSD", 1000.0))
        self.assertGreater(B.size("XAUUSD", 5.0), 0)


class SummaryTest(unittest.TestCase):
    def test_a_skip_counts_zero_in_the_paired_number(self):
        tmp = Path(tempfile.mkdtemp())
        keep = (B.ST, B.DECISIONS, B.STUDIES, B.ERRORS, B.STATUS, B.PAUSED, B.KILLED)
        self.addCleanup(lambda: [setattr(B, n, v) for n, v in zip(("ST", "DECISIONS", "STUDIES", "ERRORS", "STATUS", "PAUSED", "KILLED"), keep)])
        B.ST, B.DECISIONS, B.STUDIES, B.ERRORS, B.STATUS, B.PAUSED, B.KILLED = tmp, tmp / "d", tmp / "s", tmp / "e", tmp / "st", tmp / "p", tmp / "k"
        base = {"grading": B.GRADING, "phase": "exam", "round": 4, "version": 2, "symbol": "X", "i": 1, "utc": "2026-07-01 10:00"}
        with open(B.DECISIONS, "w") as f:
            f.write(json.dumps({**base, "arm": "playbook", "action": "BUY", "R": 2.0, "R_hands_off": 1.5, "mirror": -1.0, "outcome": "tp", "rr": 2, "mae": 0.1}) + "\n")
            f.write(json.dumps({**base, "arm": "default", "action": "SKIP"}) + "\n")
            f.write(json.dumps({**base, "symbol": "Y", "arm": "playbook", "action": "ERROR"}) + "\n")   # a failed call is not a moment
        s = B.summary()
        self.assertEqual(s["phases"]["exam"]["paired_R_per_moment"]["mean"], 2.0)
        self.assertEqual(s["phases"]["exam"]["playbook"]["take"], 1.0)
        self.assertEqual(s["phases"]["exam"]["playbook"]["moments"], 1)
        self.assertEqual(s["phases"]["exam"]["default"]["trades"], 0)


class LoginTest(unittest.TestCase):
    def test_the_billing_answer_moves_to_the_other_login(self):
        msg = "Claude Code can't use your Claude subscription, usually because a payment is past due · Pay or renew at https://claude.ai/settings/billing"
        self.assertTrue(any(w in msg.lower() for w in B.LIMIT_WORDS))


if __name__ == "__main__":
    unittest.main()
