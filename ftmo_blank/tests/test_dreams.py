"""DREAMS (tools/dreams.py): live's lessons reach the training replay only as timeless process rules of
unknown origin. The checks are the guarantee -- whatever a model writes, a dream that would tell a 2025
replay about its own future, or say where it came from, never lands. Training then owns the dreams:
it notes, revises and retires them, under the same timeless rules (the replay wraps epochs)."""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOME / "tools"))
import dreams as D  # noqa: E402

GOOD = "Wait for the first pullback after a breakout bar closes instead of entering on the breakout bar itself."


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "guard").mkdir()
        shutil.copy(HOME / "guard" / "instruments.json", self.tmp / "guard" / "instruments.json")

    def propose(self, text):
        return D.propose(text, self.tmp)


class LiveProposalTest(_Base):
    def test_a_timeless_process_rule_is_accepted_without_a_date_or_a_source(self):
        r = self.propose(GOOD)
        self.assertTrue(r["ok"], r)
        public = (self.tmp / "knowledge" / "dreams.json").read_text()
        self.assertIn("first pullback", public)
        for leak in ("2026", "live", "utc", "day"):
            self.assertNotIn(leak, public.lower().replace("dreams", "").replace("today", ""))

    def test_everything_that_could_poison_the_replay_is_refused_with_a_reason(self):
        bad = {
            "On 2026-10-07 the breakout long worked, so take breakouts early.": "date",
            "In October, pullback entries after a breakout worked better than chasing.": "month",
            "Avoid entries in the first 15 minutes after 13:30 when the cash market opens.": "clock time",
            "Longs above 4195 held better than longs taken below that level on pullbacks.": "price-like",
            "EURUSD shorts during the London session held better than the New York session.": "instrument",
            "Gold breakouts held better than breakouts on other markets during the week.": "market",
            "Metals rallied hard, so favour long entries on metals after any pullback.": "market did",
            "In the live account, stops closer than 1.5 ATR were hit by noise more often.": "came from",
            "Stops closer than 1.5 ATR were hit by noise more often this week than wider stops.": "time",
        }
        for text, why in bad.items():
            r = self.propose(text)
            self.assertFalse(r["ok"], text)
            self.assertTrue(any(why in reason for reason in r["reasons"]), (text, r["reasons"]))
        self.assertFalse((self.tmp / "knowledge" / "dreams.json").exists())

    def test_near_duplicates_and_the_daily_cap(self):
        self.assertTrue(self.propose(GOOD)["ok"])
        self.assertFalse(self.propose(GOOD.replace("itself", "itself first"))["ok"])
        self.assertTrue(self.propose("A stop placed 1.5 ATR beyond the swing that defines the setup survives "
                                     "noise better than a 1 ATR stop.")["ok"])
        self.assertTrue(self.propose("Stand aside for the first 15 minutes after the US cash open; spreads and "
                                     "wicks settle after that.")["ok"])
        r = self.propose("Take partial profit at 2R and move the stop to break-even only after the partial fills.")
        self.assertFalse(r["ok"])
        self.assertTrue(any("cap" in x for x in r["reasons"]))

    def test_every_decision_is_audited_where_training_cannot_read(self):
        self.propose(GOOD)
        self.propose("Gold breakouts held better than anything else.")
        rows = [json.loads(l) for l in (self.tmp / "state" / "dreams_audit.jsonl").read_text().splitlines()]
        self.assertEqual([r["event"] for r in rows], ["accepted", "refused"])
        self.assertTrue(all("utc" in r and "day" in r for r in rows))   # provenance lives only here


class TrainingOwnsThemTest(_Base):
    def setUp(self):
        super().setUp()
        self.assertTrue(self.propose(GOOD)["ok"])

    def test_training_can_note_revise_and_retire(self):
        self.assertTrue(D.note("1", "Held in 4 of 6 trades where it applied; both failures were range days.",
                               self.tmp)["ok"])
        self.assertTrue(D.revise("dream 001", "Wait for the first pullback after a breakout closes; skip it on "
                                 "range days.", "the notes show range days fail", self.tmp)["ok"])
        d = D.dreams(self.tmp)["dream 001"]
        self.assertIn("range days", d["rule"])
        self.assertEqual((len(d["notes"]), d["revised"]), (1, 1))
        self.assertTrue(D.retire("1", "did not hold over a larger sample", self.tmp)["ok"])
        self.assertEqual(D.dreams(self.tmp), {})

    def test_training_edits_stay_timeless_too(self):
        """The replay wraps epochs: a dated note would leak into a later replay of an earlier window."""
        self.assertFalse(D.note("1", "Failed on 2025-03-04 when the range was thin.", self.tmp)["ok"])
        self.assertFalse(D.revise("1", "Only take it on EURUSD.", "it worked there", self.tmp)["ok"])
        self.assertFalse(D.revise("1", GOOD + " Then confirm.", "short", self.tmp)["ok"])   # a reason is required
        self.assertFalse(D.retire("1", "short", self.tmp)["ok"])   # a reason is required

    def test_training_may_say_it_tested_something(self):
        self.assertTrue(D.note("1", "Tested across 12 replay trades: held in 8.", self.tmp)["ok"])

    def test_numbers_keep_counting_after_a_retirement(self):
        D.retire("1", "did not hold over a larger sample", self.tmp)
        r = self.propose("A stop placed 1.5 ATR beyond the swing that defines the setup survives noise better.")
        self.assertEqual(r["id"], "dream 002")


class CliTest(_Base):
    def test_read_after_propose_round_trips(self):
        import contextlib
        import io
        D.main(["propose", "--text", GOOD], self.tmp)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            D.main(["read"], self.tmp)
        self.assertIn("dream 001", json.loads(buf.getvalue()))


if __name__ == "__main__":
    unittest.main()
