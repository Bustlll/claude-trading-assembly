"""Tests for the clock-driven safety rails (Friday flatten, rollover SL guard) that live outside
the per-order gate chain because they must act on EXISTING positions proactively. Hermetic, same
convention as test_live_broker.py: a fake MT5 double through live_broker.py's own _mt5_client
seam, never a real bridge."""
import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path

HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOME / "tools"))
sys.path.insert(0, str(HOME))
import live_broker as L  # noqa: E402
import live_watchdog as W  # noqa: E402
from test_live_broker import _FakeMT5, _pos  # noqa: E402


class AdaptivePollingTest(unittest.TestCase):
    """operator's explicit call, 2026-10-10: idle most of the week, tight only near a window --
    the whole point is to NOT poll every 30s around the clock for a rule that can only ever fire
    in a one-hour slice of the week."""

    def test_zero_when_already_inside_the_friday_flatten_window(self):
        self.assertEqual(W._seconds_until_next_critical_window(
            dt.datetime(2026, 10, 9, 20, 5, tzinfo=dt.timezone.utc)), 0.0)

    def test_zero_when_the_clock_is_at_the_rollover_hour(self):
        # that DAY's rollover hour (17:00 New York moves with US DST), not the import-time ROLLOVER_HOUR_UTC: the test
        # failed from the first day of US winter time (2026-11-01) on a summer date
        hour = W.rollover_hour_utc(dt.datetime(2026, 10, 7, 12, tzinfo=dt.timezone.utc))
        self.assertEqual(W._seconds_until_next_critical_window(
            dt.datetime(2026, 10, 7, hour, 30, tzinfo=dt.timezone.utc)), 0.0)

    def test_far_from_either_window_returns_a_large_gap_well_past_the_lead_window(self):
        # Monday 09:00 UTC -- nowhere near Friday 20:00 or any day's 21:00 rollover hour.
        secs = W._seconds_until_next_critical_window(dt.datetime(2026, 10, 5, 9, 0, tzinfo=dt.timezone.utc))
        self.assertGreater(secs, W.LEAD_MINUTES * 60)

    def test_counts_down_correctly_approaching_the_rollover_hour(self):
        # 30 minutes before this day's rollover hour -- inside the lead window.
        hour = W.rollover_hour_utc(dt.datetime(2026, 10, 7, 12, tzinfo=dt.timezone.utc))
        now = dt.datetime(2026, 10, 7, hour - 1, 30, tzinfo=dt.timezone.utc)
        secs = W._seconds_until_next_critical_window(now)
        self.assertLessEqual(secs, W.LEAD_MINUTES * 60)
        self.assertAlmostEqual(secs, 1800, delta=1)

    def test_idle_poll_is_short_enough_to_never_miss_the_lead_window(self):
        """If this ever regressed (idle interval >= lead window), a check could land just past
        the lead window's start and sleep right through it -- the one invariant that makes the
        whole adaptive scheme safe."""
        self.assertLess(W.IDLE_POLL_S, W.LEAD_MINUTES * 60)


class FridayFlattenDueTest(unittest.TestCase):
    def test_false_on_an_ordinary_weekday(self):
        self.assertFalse(W._friday_flatten_due(dt.datetime(2026, 10, 8, 12, 0, tzinfo=dt.timezone.utc)))  # Thursday

    def test_false_on_friday_before_the_cutoff(self):
        self.assertFalse(W._friday_flatten_due(dt.datetime(2026, 10, 9, 19, 59, tzinfo=dt.timezone.utc)))

    def test_true_on_friday_at_and_after_the_cutoff(self):
        self.assertTrue(W._friday_flatten_due(dt.datetime(2026, 10, 9, 20, 0, tzinfo=dt.timezone.utc)))
        self.assertTrue(W._friday_flatten_due(dt.datetime(2026, 10, 9, 23, 59, tzinfo=dt.timezone.utc)))

    def test_true_all_day_saturday(self):
        self.assertTrue(W._friday_flatten_due(dt.datetime(2026, 10, 10, 3, 0, tzinfo=dt.timezone.utc)))

    def test_false_on_sunday(self):
        self.assertFalse(W._friday_flatten_due(dt.datetime(2026, 10, 11, 23, 0, tzinfo=dt.timezone.utc)))


class WatchdogGateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "state").mkdir()
        self._real_home = L.HOME
        L.HOME = self.tmp
        L.LEDGER_PATH = self.tmp / "state" / "live_ledger.jsonl"
        W.ROLLOVER_STATE_PATH = self.tmp / "state" / "live_rollover.json"

    def tearDown(self):
        L.HOME = self._real_home

    def test_friday_flatten_closes_only_this_workers_own_tagged_positions(self):
        ours = _pos(1, "XAUUSD", "buy", 0.1, 4100.0, sl=4080.0, magic=L.MAGIC)
        theirs = _pos(2, "EURUSD", "buy", 0.1, 1.1000, sl=1.0950, magic=313700)
        fake = _FakeMT5(positions=[ours, theirs], tick_bid=4105.0, tick_ask=4105.5)
        W._friday_flatten_pass(fake, dry_run=False)
        self.assertEqual(len(fake.sent), 1)
        self.assertEqual(fake.sent[0]["position"], 1)
        self.assertEqual(fake.sent[0]["magic"], L.MAGIC)

    def test_friday_flatten_dry_run_sends_nothing(self):
        ours = _pos(1, "XAUUSD", "buy", 0.1, 4100.0, sl=4080.0, magic=L.MAGIC)
        fake = _FakeMT5(positions=[ours], tick_bid=4105.0, tick_ask=4105.5)
        W._friday_flatten_pass(fake, dry_run=True)
        self.assertEqual(fake.sent, [])

    def test_friday_flatten_survives_an_unreadable_positions_list(self):
        class _Broken(_FakeMT5):
            def positions_get(self, ticket=None):
                return None
        fake = _Broken()
        W._friday_flatten_pass(fake, dry_run=False)  # must not raise
        self.assertEqual(fake.sent, [])

    def test_rollover_widens_a_short_during_the_window_and_leaves_longs_alone(self):
        short = _pos(3, "EURUSD", "sell", 0.1, 1.1000, sl=1.1050, magic=L.MAGIC)
        long_ = _pos(4, "EURUSD", "buy", 0.1, 1.1000, sl=1.0950, magic=L.MAGIC)
        fake = _FakeMT5(positions=[short, long_])
        in_window = dt.datetime.now(dt.timezone.utc).replace(hour=W.ROLLOVER_HOUR_UTC)
        import unittest.mock as mock
        with mock.patch.object(W, "_now_utc", return_value=in_window):
            W._rollover_pass(fake, dry_run=False)
        self.assertEqual(len(fake.sent), 1)   # only the short was touched
        self.assertEqual(fake.sent[0]["action"], fake.TRADE_ACTION_SLTP)
        self.assertEqual(fake.sent[0]["position"], 3)
        self.assertGreater(fake.sent[0]["sl"], 1.1050)   # widened upward, away from price
        state = json.loads(W.ROLLOVER_STATE_PATH.read_text())
        self.assertIn("3", state)
        self.assertEqual(state["3"]["orig"], 1.1050)

    def test_the_rollover_cushion_keeps_the_target(self):
        """2026-10-19: the SLTP request was sent without "tp", which MT5 reads as 0 -- the ETHUSD short lost its
        2375 target at the 21:05 UTC widen. The position's own target must ride along, on widen and restore."""
        short = _pos(3, "EURUSD", "sell", 0.1, 1.1000, sl=1.1050, tp=1.0900, magic=L.MAGIC)
        fake = _FakeMT5(positions=[short])
        in_window = dt.datetime.now(dt.timezone.utc).replace(hour=W.ROLLOVER_HOUR_UTC)
        import unittest.mock as mock
        with mock.patch.object(W, "_now_utc", return_value=in_window):
            W._rollover_pass(fake, dry_run=False)
        self.assertEqual(fake.sent[0]["tp"], 1.0900)
        self.assertTrue(W._set_sl(fake, 3, "EURUSD", 1.1050))
        self.assertEqual(fake.sent[-1]["tp"], 1.0900)

    def test_set_sl_sends_nothing_when_the_position_cannot_be_read(self):
        class _Broken(_FakeMT5):
            def positions_get(self, ticket=None):
                return None
        fake = _Broken()
        self.assertFalse(W._set_sl(fake, 3, "EURUSD", 1.1060))
        self.assertEqual(fake.sent, [])

    def test_rollover_never_re_widens_a_ticket_already_recorded_this_window(self):
        short = _pos(3, "EURUSD", "sell", 0.1, 1.1000, sl=1.1100, magic=L.MAGIC)  # already wide
        fake = _FakeMT5(positions=[short])
        W.ROLLOVER_STATE_PATH.write_text(json.dumps({"3": {"orig": 1.1050, "wide": 1.1100}}))
        in_window = dt.datetime.now(dt.timezone.utc).replace(hour=W.ROLLOVER_HOUR_UTC)
        import unittest.mock as mock
        with mock.patch.object(W, "_now_utc", return_value=in_window):
            W._rollover_pass(fake, dry_run=False)
        self.assertEqual(fake.sent, [])

    def test_rollover_restores_outside_the_window_when_nothing_else_moved_it(self):
        short = _pos(3, "EURUSD", "sell", 0.1, 1.1000, sl=1.1100, magic=L.MAGIC)  # still at the widened level
        fake = _FakeMT5(positions=[short])
        W.ROLLOVER_STATE_PATH.write_text(json.dumps({"3": {"orig": 1.1050, "wide": 1.1100}}))
        outside_window = dt.datetime.now(dt.timezone.utc).replace(hour=(W.ROLLOVER_HOUR_UTC + 2) % 24)
        import unittest.mock as mock
        with mock.patch.object(W, "_now_utc", return_value=outside_window):
            W._rollover_pass(fake, dry_run=False)
        self.assertEqual(len(fake.sent), 1)
        self.assertAlmostEqual(fake.sent[0]["sl"], 1.1050)
        state = json.loads(W.ROLLOVER_STATE_PATH.read_text())
        self.assertNotIn("3", state)

    def test_rollover_leaves_state_untouched_on_an_unreadable_read_never_evicts(self):
        W.ROLLOVER_STATE_PATH.write_text(json.dumps({"999": {"orig": 1.0, "wide": 1.1}}))

        class _Broken(_FakeMT5):
            def positions_get(self, ticket=None):
                return None
        fake = _Broken()
        W._rollover_pass(fake, dry_run=False)
        state = json.loads(W.ROLLOVER_STATE_PATH.read_text())
        self.assertIn("999", state)  # untouched, not evicted, despite the ticket not existing here

    def test_rollover_ignores_other_workers_positions(self):
        theirs = _pos(5, "EURUSD", "sell", 0.1, 1.1000, sl=1.1050, magic=313700)
        fake = _FakeMT5(positions=[theirs])
        in_window = dt.datetime.now(dt.timezone.utc).replace(hour=W.ROLLOVER_HOUR_UTC)
        import unittest.mock as mock
        with mock.patch.object(W, "_now_utc", return_value=in_window):
            W._rollover_pass(fake, dry_run=False)
        self.assertEqual(fake.sent, [])

    def test_rollover_dry_run_sends_nothing(self):
        short = _pos(3, "EURUSD", "sell", 0.1, 1.1000, sl=1.1050, magic=L.MAGIC)
        fake = _FakeMT5(positions=[short])
        in_window = dt.datetime.now(dt.timezone.utc).replace(hour=W.ROLLOVER_HOUR_UTC)
        import unittest.mock as mock
        with mock.patch.object(W, "_now_utc", return_value=in_window):
            W._rollover_pass(fake, dry_run=True)
        self.assertEqual(fake.sent, [])


class RolloverSpreadTest(unittest.TestCase):
    """2026-10-13. Widen by the instrument's measured rollover spike; restore once the live spread is back
    to normal (<= RESTORE_SPREAD_MULT x typical on RESTORE_NORMAL_POLLS polls in a row), or at the time cap
    -- after a USDMXN stop was taken by a post-rollover spread spike the widen (0 cushion) never covered."""

    def setUp(self):
        import unittest.mock as mock
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "state").mkdir()
        self._saved = (L.HOME, L.LEDGER_PATH, W.ROLLOVER_STATE_PATH, W.MEASURED_SPREADS_PATH)
        L.HOME, L.LEDGER_PATH = self.tmp, self.tmp / "state" / "live_ledger.jsonl"
        W.ROLLOVER_STATE_PATH = self.tmp / "state" / "live_rollover.json"
        W.MEASURED_SPREADS_PATH = self.tmp / "measured.json"
        W.MEASURED_SPREADS_PATH.write_text(json.dumps({"USDMXN": {"median": 0.0047, "rollover_max": 0.0695}}))
        real_spec = W.R.spec
        self._spec = mock.patch.object(W.R, "spec", side_effect=lambda sym: (
            {**real_spec(sym), "spread_price_snapshot": self.typical} if sym == "USDMXN" else real_spec(sym)))
        self._spec.start()
        self.typical = 0.0047
        self.day = dt.datetime(2026, 10, 13, tzinfo=dt.timezone.utc)

    def tearDown(self):
        self._spec.stop()
        L.HOME, L.LEDGER_PATH, W.ROLLOVER_STATE_PATH, W.MEASURED_SPREADS_PATH = self._saved

    def run_at(self, hour, minute, fake):
        import unittest.mock as mock
        with mock.patch.object(W, "_now_utc", return_value=self.day.replace(hour=hour, minute=minute)):
            W._rollover_pass(fake, dry_run=False)

    def short(self, sl=18.285):
        return _pos(7, "USDMXN", "sell", 0.06, 18.2953, sl=sl, magic=L.MAGIC)

    def ledger(self):
        return [json.loads(x) for x in L.LEDGER_PATH.read_text().splitlines()] if L.LEDGER_PATH.exists() else []

    def test_the_rollover_hour_follows_new_york_17_00(self):
        self.assertEqual(W.rollover_hour_utc(dt.datetime(2026, 10, 13, 12, tzinfo=dt.timezone.utc)), 21)  # EDT
        self.assertEqual(W.rollover_hour_utc(dt.datetime(2026, 11, 2, 12, tzinfo=dt.timezone.utc)), 22)   # EST

    def test_widen_covers_the_measured_rollover_spike(self):
        fake = _FakeMT5(positions=[self.short()], ticks={"USDMXN": (18.250, 18.255)})
        self.run_at(21, 0, fake)
        self.assertAlmostEqual(fake.sent[0]["sl"], 18.285 + 0.0695, places=6)   # the spike, not 2 x typical

    def test_widen_uses_the_live_spread_when_nothing_is_known(self):
        self.typical = 0.0
        W.MEASURED_SPREADS_PATH.write_text("{}")
        fake = _FakeMT5(positions=[self.short()], ticks={"USDMXN": (18.250, 18.260)})
        self.run_at(21, 0, fake)
        self.assertAlmostEqual(fake.sent[0]["sl"], 18.285 + W.ROLLOVER_WIDEN_MULT * 0.010, places=6)

    def test_no_spread_at_all_sends_nothing_and_says_so(self):
        self.typical = 0.0
        W.MEASURED_SPREADS_PATH.write_text("{}")

        class NoQuote(_FakeMT5):
            def symbol_info_tick(self, symbol):
                return None
        fake = NoQuote(positions=[self.short()])
        self.run_at(21, 0, fake)
        self.assertEqual(fake.sent, [])
        self.assertTrue(any(r["type"] == "rollover_guard_error" and "no spread" in r["msg"] for r in self.ledger()))

    def _widened(self, spread):
        wide = 18.285 + 0.0695
        W.ROLLOVER_STATE_PATH.write_text(json.dumps({"7": {"orig": 18.285, "wide": wide, "typical": self.typical,
                                                           "normal_polls": 0}}))
        return _FakeMT5(positions=[self.short(sl=wide)], ticks={"USDMXN": (18.255, 18.255 + spread)})

    def test_restore_waits_while_the_spread_is_still_wide(self):
        fake = self._widened(spread=0.03)                 # ~6x typical, as at 21:55 that night
        self.run_at(22, 0, fake)
        self.run_at(22, 1, fake)
        self.assertEqual(fake.sent, [])
        self.assertIn("7", json.loads(W.ROLLOVER_STATE_PATH.read_text()))

    def test_restore_after_the_spread_is_normal_on_consecutive_polls(self):
        fake = self._widened(spread=0.007)                # 1.5x typical
        self.run_at(22, 0, fake)
        self.assertEqual(fake.sent, [])                   # one normal poll is not yet enough
        self.run_at(22, 1, fake)
        self.assertEqual(len(fake.sent), 1)
        self.assertAlmostEqual(fake.sent[0]["sl"], 18.285)
        rows = [r for r in self.ledger() if r["type"] == "rollover_sl_restore"]
        self.assertEqual(rows[0]["why"], "spread back to normal")

    def test_a_spike_between_normal_polls_starts_the_count_again(self):
        fake = self._widened(spread=0.007)
        self.run_at(22, 0, fake)
        fake._ticks = {"USDMXN": (18.255, 18.285)}       # spread 0.03 again
        self.run_at(22, 1, fake)
        fake._ticks = {"USDMXN": (18.255, 18.262)}
        self.run_at(22, 2, fake)
        self.assertEqual(fake.sent, [])                   # 1 normal poll since the spike

    def test_the_time_cap_restores_even_if_the_spread_never_settles(self):
        fake = self._widened(spread=0.03)
        self.run_at(23, 0, fake)                          # rollover 21:00 + 120 min
        self.assertEqual(len(fake.sent), 1)
        self.assertIn("time cap", [r for r in self.ledger() if r["type"] == "rollover_sl_restore"][0]["why"])

    def test_no_own_positions_clears_leftover_state(self):
        W.ROLLOVER_STATE_PATH.write_text(json.dumps({"561482224": {"orig": 18.285, "wide": 18.285}}))
        self.run_at(3, 0, _FakeMT5(positions=[]))
        self.assertEqual(json.loads(W.ROLLOVER_STATE_PATH.read_text()), {})


if __name__ == "__main__":
    unittest.main()
