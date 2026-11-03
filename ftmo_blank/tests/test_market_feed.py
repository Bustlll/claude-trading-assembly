"""Tests for the market-data distributor (manual/LIVE_TRADING_IDEA.md section 2): publish once,
read many -- and in LIVE mode, publish the real broker snapshot, or, when the broker cannot be
reached, say so plainly rather than inventing one. Hermetic: the broker seam is always patched."""
import sys
import tempfile
import unittest
from pathlib import Path

HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOME / "tools"))
import market_feed as M  # noqa: E402
import live_broker as LB  # noqa: E402
from live_fakes import no_broker  # noqa: E402


class MarketFeedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "state").mkdir()
        self._real_client = LB._mt5_client
        LB._mt5_client = no_broker  # never the real shared bridge from a test

    def tearDown(self):
        LB._mt5_client = self._real_client

    def test_a_given_snapshot_is_published_as_is_without_rebuilding_one(self):
        snap = {"utc": "2026-01-05 10:00 UTC", "equity": 10123.45}
        rec = M.distribute("TRAINING", home=self.tmp, snapshot=snap)
        self.assertEqual(rec["snapshot"], snap)
        self.assertEqual(rec["published_utc"], "2026-01-05 10:00 UTC")

    def test_read_reports_its_own_age(self):
        M.distribute("TRAINING", home=self.tmp, snapshot={"utc": "x"})
        rec = M.read(home=self.tmp)
        self.assertIn("age_s", rec)
        self.assertGreaterEqual(rec["age_s"], 0)

    def test_reading_before_anything_is_published_says_so(self):
        self.assertIn("error", M.read(home=self.tmp))

    def test_live_mode_with_no_broker_refuses_to_invent_a_snapshot(self):
        rec = M.distribute("LIVE", home=self.tmp)
        self.assertIsNone(rec["snapshot"])
        self.assertIn("snapshot failed", rec["error"])

    def test_live_mode_publishes_the_loops_real_snapshot_as_is(self):
        snap = {"utc": "2026-10-07 10:00 UTC", "equity": 9606.52, "currency": "EUR"}
        rec = M.distribute("LIVE", home=self.tmp, snapshot=snap)
        self.assertEqual(rec["snapshot"], snap)
        self.assertNotIn("error", rec)
        self.assertEqual(M.read(home=self.tmp)["published_utc"], "2026-10-07 10:00 UTC")

    def test_cli_distribute_then_read_round_trips(self):
        import contextlib
        import io
        import json
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            M.main(["distribute", "LIVE"], home=self.tmp)
        self.assertIn("snapshot failed", json.loads(buf.getvalue())["error"])


if __name__ == "__main__":
    unittest.main()
