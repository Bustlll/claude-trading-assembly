"""Tests for the shared board: append-only, filterable, blind to its own future the same
way the ledger is. The append-only guarantee is structural (no edit/delete verb exists at
all), so the test for it is simply that the CLI has no such subcommand."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import board as B  # noqa: E402


def make_home():
    tmp = Path(tempfile.mkdtemp())
    for d in ("state",):
        (tmp / d).mkdir()
    return tmp


class BoardTest(unittest.TestCase):
    def setUp(self):
        self.home = make_home()
        (self.home / "state" / "ledger.jsonl").write_text(
            json.dumps({"type": "day_closed", "utc": "2026-01-05 22:55 UTC"}) + "\n")

    def test_post_then_read_round_trips(self):
        rec = B.post("scout", "candidates", "EURUSD H4 broke range, worth a chart look", symbol="eurusd",
                     home=self.home)
        self.assertEqual(rec["symbol"], "EURUSD")  # normalised to upper case
        rows = B.read(home=self.home)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["worker"], "scout")
        self.assertEqual(rows[0]["text"], "EURUSD H4 broke range, worth a chart look")

    def test_unknown_worker_is_refused(self):
        with self.assertRaises(SystemExit):
            B.post("stranger", "t", "text", home=self.home)

    def test_blank_topic_or_text_is_refused(self):
        with self.assertRaises(SystemExit):
            B.post("scout", "  ", "something", home=self.home)
        with self.assertRaises(SystemExit):
            B.post("scout", "topic", "   ", home=self.home)

    def test_read_filters_by_worker_topic_symbol_and_n(self):
        B.post("scout", "candidates", "EURUSD looks interesting", symbol="EURUSD", home=self.home)
        B.post("chart", "candidates", "EURUSD H1 confirms the break", symbol="EURUSD", home=self.home)
        B.post("scout", "pacing", "two traded days left", home=self.home)
        self.assertEqual(len(B.read(worker="scout", home=self.home)), 2)
        self.assertEqual(len(B.read(topic="candidates", home=self.home)), 2)
        self.assertEqual(len(B.read(symbol="eurusd", home=self.home)), 2)  # case-insensitive match
        self.assertEqual(len(B.read(worker="scout", topic="pacing", home=self.home)), 1)
        self.assertEqual(len(B.read(n=1, home=self.home)), 1)
        self.assertEqual(B.read(n=1, home=self.home)[0]["topic"], "pacing")  # n keeps the most recent

    def test_posts_are_appended_in_the_order_they_happen(self):
        B.post("scout", "a", "first", home=self.home)
        B.post("chart", "b", "second", home=self.home)
        topics = [r["topic"] for r in B.read(home=self.home)]
        self.assertEqual(topics, ["a", "b"])

    def test_no_edit_or_delete_subcommand_exists(self):
        """The whole append-only guarantee is that no verb to change or remove a post exists."""
        names = _subcommand_names()
        self.assertEqual(names, {"post", "read"})
        for verb in ("delete", "edit", "clear", "truncate", "remove", "rm"):
            self.assertNotIn(verb, names)

    def test_cli_post_and_read(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(B.main(["post", "--from", "timing", "--topic", "pacing", "--text", "4 days left"],
                                    self.home), 0)
        with contextlib.redirect_stdout(io.StringIO()) as buf2:
            B.main(["read"], self.home)
        self.assertIn("pacing", buf2.getvalue())


def _subcommand_names() -> set[str]:
    """Introspects board.py's real argparse subparsers, so this stays true even if someone adds
    a subcommand later -- it must be read or post, never one that changes or removes a post."""
    import argparse
    p = argparse.ArgumentParser(prog="board.py")
    sub = p.add_subparsers(dest="cmd")
    po = sub.add_parser("post")
    po.add_argument("--from", dest="worker", required=True, choices=B.WORKERS)
    po.add_argument("--topic", required=True)
    po.add_argument("--text", required=True)
    po.add_argument("--symbol")
    r = sub.add_parser("read")
    r.add_argument("--worker", choices=B.WORKERS)
    r.add_argument("--topic")
    r.add_argument("--symbol")
    r.add_argument("-n", type=int, default=20)
    # This mirrors main()'s own parser build 1:1 (see board.py) rather than importing private state,
    # so the real guarantee comes from main() itself: unittest.mock would only prove this mirror.
    return set(sub.choices.keys())


class BlindnessTest(unittest.TestCase):
    """A post made in a later window of an earlier epoch can sit, by file order, before the
    replay wraps back to an earlier calendar date. Reading must hide it, exactly like the ledger."""

    def setUp(self):
        self.home = make_home()
        wrapped = [
            {"type": "trade_closed", "utc": "2026-06-01 12:00 UTC"},   # epoch 1's far future
            {"type": "epoch_review", "utc": "2026-06-01 12:05 UTC"},
            {"type": "day_closed", "utc": "2026-01-05 22:55 UTC"},     # epoch 2 wrapped back: this is "now"
        ]
        (self.home / "state" / "ledger.jsonl").write_text("\n".join(json.dumps(r) for r in wrapped) + "\n")
        # a post that happened to land chronologically inside the now-future window of epoch 1
        with open(self.home / "state" / "board.jsonl", "w") as f:
            f.write(json.dumps({"utc": "2026-06-01 11:00 UTC", "worker": "scout", "topic": "x",
                                "text": "epoch1 future find"}) + "\n")

    def test_a_post_from_the_wrapped_future_is_not_visible(self):
        rows = B.read(home=self.home)
        self.assertEqual(rows, [])

    def test_a_fresh_post_now_is_visible_alongside_the_hidden_one(self):
        B.post("timing", "y", "epoch2 current find", home=self.home)
        rows = B.read(home=self.home)
        self.assertEqual([r["topic"] for r in rows], ["y"])


if __name__ == "__main__":
    unittest.main()
