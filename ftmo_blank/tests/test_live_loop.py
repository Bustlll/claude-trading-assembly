"""Tests for tools/live_loop.py: what one LIVE tick reports. Hermetic -- every broker call goes to
tests/live_fakes.FakeMT5, the clock is pinned per test, state lives in a temp dir."""
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
import live_loop as LL  # noqa: E402
from live_fakes import FakeMT5, pos, deal, m5_rates, broker_epoch, point_live_broker_at  # noqa: E402

from zoneinfo import ZoneInfo  # noqa: E402

UTC = dt.timezone.utc
WED_0802 = dt.datetime(2026, 10, 7, 8, 2, tzinfo=UTC)        # 10:02 Madrid (CEST), inside the window
OPEN_0702_MADRID = dt.datetime(2026, 10, 7, 5, 2, tzinfo=UTC)  # 07:02 Madrid: the window's first slot


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "state").mkdir()
        self._restore = point_live_broker_at(self.tmp)
        (self.tmp / "state" / "MODE").write_text("LIVE")
        self._window = (LL.WINDOW_TZ, LL.WINDOW_START_MIN, LL.WINDOW_END_MIN, LL.DECISION_EVERY_MIN)
        LL.WINDOW_TZ, LL.WINDOW_START_MIN, LL.WINDOW_END_MIN, LL.DECISION_EVERY_MIN = (
            ZoneInfo("Europe/Madrid"), 7 * 60, 20 * 60, 60)  # the operator's 07:00-20:00 Madrid, hourly

    def tearDown(self):
        self._restore()
        LL.WINDOW_TZ, LL.WINDOW_START_MIN, LL.WINDOW_END_MIN, LL.DECISION_EVERY_MIN = self._window

    def at(self, now: dt.datetime, **kw) -> FakeMT5:
        L._now_utc = lambda: now
        fake = FakeMT5(tick_time=broker_epoch(now), **kw)
        L._mt5_client = lambda: fake
        return fake

    def ledger(self):
        p = self.tmp / "state" / "live_ledger.jsonl"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def place_row(self, ticket, **kw):
        row = {"type": "order_placed", "source": L.SOURCE_TAG, "ticket": ticket, "symbol": "EURUSD", "side": "buy",
               "lots": 0.1, "entry": 1.1000, "sl": 1.0950, "tp": 1.1100, "risk_ccy": 50.0, "currency": "USD",
               "why": "test entry", "utc": "2026-10-07 06:00:00", "day": "2026-10-07", "consulted_analysis": True,
               "ref_price": 1.0999, **kw}
        with open(self.tmp / "state" / "live_ledger.jsonl", "a") as f:
            f.write(json.dumps(row) + "\n")


class ReconcileTest(_Base):
    def test_a_new_own_position_is_seen_and_reported_opened(self):
        fake = self.at(WED_0802, positions=[pos(11, "EURUSD", "buy", 0.1, 1.1000, sl=1.0950, magic=L.MAGIC),
                                             pos(12, "XAUUSD", "sell", 0.1, 4100.0, sl=4150.0, magic=313700)])
        ev = LL.reconcile(fake, home=self.tmp, now=WED_0802)
        self.assertEqual([(e["type"], e["ticket"]) for e in ev], [("live_trade_opened", 11)])  # not the other runner's

    def test_a_stop_hit_at_the_broker_becomes_a_closed_trade_with_its_R(self):
        self.place_row(11)
        fake = self.at(WED_0802, positions=[pos(11, "EURUSD", "buy", 0.1, 1.1000, sl=1.0950, magic=L.MAGIC)])
        LL.reconcile(fake, home=self.tmp, now=WED_0802)
        fake._positions = []
        fake._deals = [deal("EURUSD", 0.0, magic=L.MAGIC, commission=-0.35, position_id=11, entry=0, reason=3,
                            price=1.1000, volume=0.1, when=WED_0802 - dt.timedelta(hours=2)),
                       deal("EURUSD", -50.0, magic=L.MAGIC, commission=-0.35, position_id=11, entry=1, reason=4,
                            price=1.0950, volume=0.1, when=WED_0802 - dt.timedelta(minutes=1))]
        ev = LL.reconcile(fake, home=self.tmp, now=WED_0802)
        self.assertEqual(len(ev), 1)
        t = ev[0]
        self.assertEqual((t["type"], t["reason"], t["result"]), ("live_trade_closed", "sl", "loss"))
        self.assertAlmostEqual(t["net"], -50.7)
        self.assertAlmostEqual(t["r_price"], -1.0)
        self.assertAlmostEqual(t["r_net"], -1.014)
        self.assertTrue(t["consulted_analysis"])
        self.assertEqual(t["day"], "2026-10-07")
        self.assertEqual([r["type"] for r in self.ledger()].count("trade_closed"), 1)
        self.assertEqual(LL.reconcile(fake, home=self.tmp, now=WED_0802), [])  # never reported twice

    def test_a_closed_trade_carries_every_stop_this_worker_moved(self):
        # 2026-10-12: the review called a twice-trailed USOIL trade "no trail" -- it never saw the moves
        self.place_row(12)
        with open(self.tmp / "state" / "live_ledger.jsonl", "a") as f:
            for old, new, at in ((1.0950, 1.1005, "06:30:00"), (1.1005, 1.1030, "07:00:00")):
                f.write(json.dumps({"type": "modify_placed", "source": L.SOURCE_TAG, "symbol": "EURUSD", "id": 12,
                                    "old_sl": old, "new_sl": new, "why": "trail", "utc": f"2026-10-07 {at}"}) + "\n")
            f.write(json.dumps({"type": "modify_placed", "source": L.SOURCE_TAG, "symbol": "EURUSD", "id": 99,
                                "old_sl": 1.0, "new_sl": 1.1, "why": "another ticket"}) + "\n")
        fake = self.at(WED_0802, deals=[deal("EURUSD", 30.0, magic=L.MAGIC, position_id=12, entry=1, reason=5,
                                             price=1.1060, volume=0.1, when=WED_0802 - dt.timedelta(minutes=1))])
        t = LL.reconcile(fake, home=self.tmp, now=WED_0802)[0]
        self.assertEqual([(m["old_sl"], m["new_sl"]) for m in t["stop_moves"]], [(1.0950, 1.1005), (1.1005, 1.1030)])
        self.assertEqual([r for r in self.ledger() if r["type"] == "trade_closed"][0]["stop_moves"], t["stop_moves"])

    def test_a_vanished_position_without_an_exit_deal_is_not_called_closed(self):
        """An empty positions read can be a dropped RPC: no exit deal, no 'closed' -- retried later."""
        fake = self.at(WED_0802, positions=[pos(11, "EURUSD", "buy", 0.1, 1.1000, sl=1.0950, magic=L.MAGIC)])
        LL.reconcile(fake, home=self.tmp, now=WED_0802)
        fake._positions = []
        self.assertEqual(LL.reconcile(fake, home=self.tmp, now=WED_0802), [])
        seen = json.loads((self.tmp / "state" / "live_positions_seen.json").read_text())
        self.assertIn("11", seen)

    def test_a_trade_opened_and_closed_between_two_ticks_is_still_reviewed(self):
        self.place_row(21)
        fake = self.at(WED_0802, deals=[
            deal("EURUSD", 0.0, magic=L.MAGIC, position_id=21, entry=0, reason=3, price=1.1000, volume=0.1),
            deal("EURUSD", 100.0, magic=L.MAGIC, position_id=21, entry=1, reason=5, price=1.1100, volume=0.1)])
        ev = LL.reconcile(fake, home=self.tmp, now=WED_0802)
        self.assertEqual([(e["type"], e["reason"], e["r_price"]) for e in ev], [("live_trade_closed", "tp", 2.0)])

    def test_exit_reasons_say_who_closed_it(self):
        cases = [(3, "ftmo_blank_fri_flat", "watchdog_flatten"), (3, "ftmo_blank_live:close", "manual"),
                 (0, "", "operator_manual"), (6, "", "stop_out")]
        for i, (code, comment, want) in enumerate(cases):
            tk = 100 + i
            self.place_row(tk)
            fake = self.at(WED_0802, deals=[deal("EURUSD", -1.0, magic=L.MAGIC, position_id=tk, entry=1, reason=code,
                                                  price=1.0990, volume=0.1, comment=comment)])
            ev = LL.reconcile(fake, home=self.tmp, now=WED_0802)
            self.assertEqual(ev[0]["reason"], want, comment)


class ScanTest(_Base):
    def test_a_move_past_the_threshold_is_flagged_a_flat_or_stale_one_is_not(self):
        fake = self.at(WED_0802)
        fake._rates = {
            ("EURUSD", fake.TIMEFRAME_M5): m5_rates(1.1000, 0.0005, 14, WED_0802.replace(minute=0)),  # +0.6%
            ("GBPUSD", fake.TIMEFRAME_M5): m5_rates(1.2500, 0.0, 14, WED_0802.replace(minute=0)),     # flat
            ("XAUUSD", fake.TIMEFRAME_M5): m5_rates(4000.0, 4.0, 14, WED_0802 - dt.timedelta(hours=2)),  # stale
        }
        got = LL.scan_candidates(fake, WED_0802, flagged=[])
        self.assertEqual([c["symbol"] for c in got], ["EURUSD"])
        self.assertEqual(LL.scan_candidates(fake, WED_0802, flagged=["EURUSD"]), [])


class TickScheduleTest(_Base):
    def tick(self, now, **kw):
        fake = self.at(now, **kw)
        return fake, LL.tick(now=now, home=self.tmp)["events"]

    def test_the_window_is_07_to_20_madrid_and_follows_its_dst_switch(self):
        self.assertFalse(LL.in_window(dt.datetime(2026, 10, 7, 4, 59, tzinfo=UTC)))  # 06:59 CEST
        self.assertTrue(LL.in_window(dt.datetime(2026, 10, 7, 5, 0, tzinfo=UTC)))    # 07:00 CEST
        self.assertTrue(LL.in_window(dt.datetime(2026, 10, 7, 17, 59, tzinfo=UTC)))  # 19:59 CEST
        self.assertFalse(LL.in_window(dt.datetime(2026, 10, 7, 18, 0, tzinfo=UTC)))  # 20:00 CEST
        self.assertFalse(LL.in_window(dt.datetime(2026, 10, 27, 5, 30, tzinfo=UTC)))  # 06:30 CET, after Oct 25
        self.assertTrue(LL.in_window(dt.datetime(2026, 10, 27, 6, 0, tzinfo=UTC)))    # 07:00 CET
        self.assertFalse(LL.in_window(dt.datetime(2026, 10, 10, 10, 0, tzinfo=UTC)))  # Saturday

    def test_one_decision_turn_per_slot_with_the_screen_on_every_tick_between(self):
        _, ev = self.tick(OPEN_0702_MADRID)
        self.assertEqual([e["type"] for e in ev], ["live_reassess"])
        self.assertEqual(ev[0]["next_decision_utc"], "06:00 UTC")  # 08:00 Madrid
        fake = self.at(OPEN_0702_MADRID + dt.timedelta(minutes=5))
        end = (OPEN_0702_MADRID + dt.timedelta(minutes=5)).replace(minute=5)
        fake._rates = {("EURUSD", fake.TIMEFRAME_M5): m5_rates(1.1000, 0.0005, 14, end)}
        ev = LL.tick(now=OPEN_0702_MADRID + dt.timedelta(minutes=5), home=self.tmp)["events"]
        self.assertEqual([e["type"] for e in ev], ["live_setup_candidate"])  # a 5-minute tick can lead to a trade
        _, ev = self.tick(OPEN_0702_MADRID + dt.timedelta(minutes=10))
        self.assertEqual(ev, [])  # EURUSD already flagged today; nothing else moved
        _, ev = self.tick(OPEN_0702_MADRID + dt.timedelta(hours=1))
        self.assertEqual([e["type"] for e in ev], ["live_reassess"])  # the next slot

    def moving_tick(self, at):
        """A tick at `at` where EURUSD moved +0.6% over the last hour of closed M5 bars."""
        fake = self.at(at)
        end = at.replace(minute=at.minute - at.minute % 5, second=0, microsecond=0)
        fake._rates = {("EURUSD", fake.TIMEFRAME_M5): m5_rates(1.1000, 0.0005, 14, end)}
        return [e["type"] for e in LL.tick(now=at, home=self.tmp)["events"]]

    def test_a_flagged_symbol_can_trigger_again_an_hour_later(self):
        # operator, 2026-10-12: re-trigger after 1h instead of once per day
        self.tick(OPEN_0702_MADRID)                                         # this slot's decision turn, flat
        t0 = OPEN_0702_MADRID + dt.timedelta(minutes=5)
        self.assertEqual(self.moving_tick(t0), ["live_setup_candidate"])
        self.assertEqual(self.moving_tick(t0 + dt.timedelta(minutes=30)), [])           # inside its hour
        _, ev = self.tick(t0 + dt.timedelta(minutes=55))                                 # next slot, flat
        self.assertEqual([e["type"] for e in ev], ["live_reassess"])
        self.assertEqual(self.moving_tick(t0 + dt.timedelta(minutes=60)), ["live_setup_candidate"])  # again

    def test_a_decision_turn_restarts_the_hour_of_the_movers_it_showed(self):
        self.tick(OPEN_0702_MADRID)
        t0 = OPEN_0702_MADRID + dt.timedelta(minutes=5)
        self.assertEqual(self.moving_tick(t0), ["live_setup_candidate"])
        self.assertEqual(self.moving_tick(t0 + dt.timedelta(minutes=55)), ["live_reassess"])  # decider saw it
        self.assertEqual(self.moving_tick(t0 + dt.timedelta(minutes=60)), [])                 # not re-handled
        self.assertEqual(self.moving_tick(t0 + dt.timedelta(minutes=110)), [])  # its hour runs from t0+55 now

    def test_a_flag_without_a_time_starts_its_hour_at_the_first_tick(self):
        # flags written before flag times were kept (the live state of 2026-10-12 morning)
        t0 = OPEN_0702_MADRID + dt.timedelta(minutes=5)
        LL._write(LL._state_path(self.tmp), {"day": LL._prague_day(t0), "decided_slot": LL.decision_slot(t0),
                                             "flagged": ["EURUSD"]})
        self.assertEqual(self.moving_tick(t0), [])
        self.assertEqual(self.moving_tick(t0 + dt.timedelta(minutes=30)), [])
        _, ev = self.tick(t0 + dt.timedelta(minutes=55))
        self.assertEqual(self.moving_tick(t0 + dt.timedelta(minutes=60)), ["live_setup_candidate"])

    def test_a_decision_turn_carries_the_top_movers(self):
        fake = self.at(OPEN_0702_MADRID)
        end = OPEN_0702_MADRID.replace(minute=0)
        fake._rates = {("XAUUSD", fake.TIMEFRAME_M5): m5_rates(4000.0, 2.0, 14, end),
                       ("EURUSD", fake.TIMEFRAME_M5): m5_rates(1.1000, 0.00001, 14, end)}
        ev = LL.tick(now=OPEN_0702_MADRID, home=self.tmp)["events"]
        self.assertEqual([m["symbol"] for m in ev[0]["movers"]], ["XAUUSD", "EURUSD"])

    def test_nothing_is_scheduled_outside_the_window_or_on_a_weekend(self):
        for t in (dt.datetime(2026, 10, 7, 4, 30, tzinfo=UTC),    # 06:30 Madrid
                  dt.datetime(2026, 10, 7, 19, 0, tzinfo=UTC),    # 21:00 Madrid
                  dt.datetime(2026, 10, 10, 10, 0, tzinfo=UTC)):  # Saturday
            _, ev = self.tick(t)
            scheduled = {"live_reassess", "live_setup_candidate", "live_risk_review"}
            self.assertFalse([e for e in ev if e["type"] in scheduled], t)  # a day rollover may still be recorded

    def test_window_end_hands_the_risk_worker_a_checkpoint_on_days_with_closed_trades(self):
        self.place_row(41)
        after = dt.datetime(2026, 10, 7, 18, 5, tzinfo=UTC)  # 20:05 Madrid
        _, ev = self.tick(after, deals=[deal("EURUSD", 30.0, magic=L.MAGIC, position_id=41, entry=1, reason=5,
                                             price=1.1060, volume=0.1, when=after - dt.timedelta(hours=3))])
        risk = [e for e in ev if e["type"] == "live_risk_review"]
        self.assertEqual(len(risk), 1)
        self.assertEqual((risk[0]["closed_today"], risk[0]["lever"]["risk_pct"]), (1, L.LIVE_RISK_INITIAL_PCT))
        _, ev = self.tick(after + dt.timedelta(minutes=5))
        self.assertFalse([e for e in ev if e["type"] == "live_risk_review"])  # once a day

    def test_a_trade_closing_after_the_checkpoint_still_reaches_the_risk_worker(self):
        # 2026-10-12: the 20:00 Madrid checkpoint saw an empty day; a target filled 8 minutes later
        after = dt.datetime(2026, 10, 7, 18, 0, 20, tzinfo=UTC)   # 20:00 Madrid
        _, ev = self.tick(after)
        self.assertFalse([e for e in ev if e["type"] == "live_risk_review"])
        self.place_row(42)
        close = after + dt.timedelta(minutes=10)
        d = [deal("EURUSD", 30.0, magic=L.MAGIC, position_id=42, entry=1, reason=5, price=1.1060, volume=0.1,
                  when=close - dt.timedelta(minutes=2))]
        _, ev = self.tick(close, deals=d)
        risk = [e for e in ev if e["type"] == "live_risk_review"]
        self.assertEqual(len(risk), 1)
        self.assertTrue(risk[0]["late"])
        self.assertEqual(risk[0]["closed_today"], 1)
        _, ev = self.tick(close + dt.timedelta(minutes=5), deals=d)
        self.assertFalse([e for e in ev if e["type"] == "live_risk_review"])   # covered: not again

    def test_a_date_only_checkpoint_from_before_counts_as_covering_no_closes(self):
        self.place_row(43)
        now = dt.datetime(2026, 10, 7, 18, 15, tzinfo=UTC)
        LL._write(LL._state_path(self.tmp), {"day": LL._prague_day(now), "risk_checked": "2026-10-07"})
        _, ev = self.tick(now, deals=[deal("EURUSD", 30.0, magic=L.MAGIC, position_id=43, entry=1, reason=5,
                                           price=1.1060, volume=0.1, when=now - dt.timedelta(minutes=5))])
        self.assertEqual(len([e for e in ev if e["type"] == "live_risk_review"]), 1)

    def test_a_quiet_weekday_skips_the_checkpoint_but_friday_always_has_one(self):
        _, ev = self.tick(dt.datetime(2026, 10, 7, 18, 5, tzinfo=UTC))
        self.assertFalse([e for e in ev if e["type"] == "live_risk_review"])
        _, ev = self.tick(dt.datetime(2026, 10, 9, 18, 5, tzinfo=UTC))  # Friday 20:05 Madrid
        self.assertTrue([e for e in ev if e["type"] == "live_risk_review" and e["weekly"]])

    def test_the_prague_day_rolling_over_closes_the_previous_day(self):
        self.place_row(5, day="2026-10-06")
        self.tick(dt.datetime(2026, 10, 6, 21, 30, tzinfo=UTC))  # 23:30 Prague
        _, ev = self.tick(dt.datetime(2026, 10, 6, 22, 5, tzinfo=UTC))  # 00:05 Prague, the 7th
        closed = [e for e in ev if e["type"] == "live_day_closed"]
        self.assertEqual(len(closed), 1)
        self.assertEqual((closed[0]["day"], closed[0]["orders_placed"], closed[0]["traded"]), ("2026-10-06", 1, True))

    def test_a_risk_warning_fires_once_a_day(self):
        _, ev = self.tick(WED_0802, balance=10_000.0, equity=9_650.0)  # 350 of a 500 daily limit = 70%
        self.assertEqual([e["kind"] for e in ev if e["type"] == "live_risk_warning"], ["daily"])
        _, ev = self.tick(WED_0802 + dt.timedelta(minutes=5), balance=10_000.0, equity=9_650.0)
        self.assertFalse([e for e in ev if e["type"] == "live_risk_warning"])

    def test_this_workers_halt_is_reported_and_stops_the_screening(self):
        loss = [deal("EURUSD", -200.0, magic=L.MAGIC, when=WED_0802 - dt.timedelta(hours=1))]
        self.tick(WED_0802, deals=loss)  # the slot's decision turn
        later = WED_0802 + dt.timedelta(minutes=5)
        fake = self.at(later, deals=loss)
        fake._rates = {("EURUSD", fake.TIMEFRAME_M5): m5_rates(1.1000, 0.0005, 14, later.replace(minute=5))}
        st = json.loads((self.tmp / "state" / "live_loop.json").read_text())
        self.assertIn("day-loss halt", st["halt"])
        ev = LL.tick(now=later, home=self.tmp)["events"]
        self.assertNotIn("live_setup_candidate", [e["type"] for e in ev])

    def test_a_trade_closed_before_a_decision_turn_is_in_that_turns_events(self):
        self.place_row(31)
        early = dt.datetime(2026, 10, 7, 4, 40, tzinfo=UTC)  # 06:40 Madrid, before the window
        self.tick(early, deals=[deal("EURUSD", 100.0, magic=L.MAGIC, position_id=31, entry=1, reason=5,
                                     price=1.1100, volume=0.1, when=early - dt.timedelta(minutes=10))])
        _, ev = self.tick(OPEN_0702_MADRID)
        reassess = next(e for e in ev if e["type"] == "live_reassess")
        self.assertIn(("live_trade_closed", 31), [(r["type"], r.get("ticket")) for r in reassess["recent_events"]])

    def test_an_unreachable_broker_is_a_tick_with_an_error_not_a_crash(self):
        L._mt5_client = lambda: (_ for _ in ()).throw(RuntimeError("bridge down"))
        res = LL.tick(now=WED_0802, home=self.tmp)
        self.assertEqual(res["events"], [])
        self.assertIn("bridge down", res["error"])


class DreamTriggerTest(_Base):
    def tick(self, now, **kw):
        self.at(now, **kw)
        return None, LL.tick(now=now, home=self.tmp)["events"]

    def _post(self, topic, text, day):
        rec = {"utc": f"{day} 12:00 UTC", "day": day, "worker": "review", "topic": topic, "text": text}
        p = self.tmp / "state" / "live_board.jsonl"
        with open(p, "a") as f:
            f.write(json.dumps(rec) + "\n")

    def test_the_days_process_lessons_wake_live_dream_at_rollover(self):
        self._post("lesson", "Entered late after the breakout; waiting for the pullback would have cut risk.", "2026-10-06")
        self._post("pre_trade", "EURUSD long above 1.1200, stop 1.1180", "2026-10-06")
        self._post("lesson", "A lesson from another day", "2026-10-05")
        self.tick(dt.datetime(2026, 10, 6, 21, 30, tzinfo=UTC))
        _, ev = self.tick(dt.datetime(2026, 10, 6, 22, 5, tzinfo=UTC))      # rollover: the day closes first
        self.assertEqual([e["type"] for e in ev if e["type"] in ("live_day_closed", "live_dream")], ["live_day_closed"])
        # the day-close review runs in that tick's drain and posts its day_review -- the dream, one tick
        # later, distils it too (2026-10-12: built in the rollover tick it never could)
        self._post("day_review", "Held a winner to target instead of closing it early on a quiet afternoon.", "2026-10-06")
        _, ev = self.tick(dt.datetime(2026, 10, 6, 22, 10, tzinfo=UTC))
        dream = [e for e in ev if e["type"] == "live_dream"]
        self.assertEqual(len(dream), 1)
        self.assertEqual(dream[0]["lessons"], ["Entered late after the breakout; waiting for the pullback would have cut risk.",
                                               "Held a winner to target instead of closing it early on a quiet afternoon."])
        _, ev = self.tick(dt.datetime(2026, 10, 6, 22, 15, tzinfo=UTC))
        self.assertFalse([e for e in ev if e["type"] == "live_dream"])          # once

    def test_a_day_without_lessons_dreams_nothing(self):
        self.tick(dt.datetime(2026, 10, 6, 21, 30, tzinfo=UTC))
        self.tick(dt.datetime(2026, 10, 6, 22, 5, tzinfo=UTC))
        _, ev = self.tick(dt.datetime(2026, 10, 6, 22, 10, tzinfo=UTC))
        self.assertFalse([e for e in ev if e["type"] == "live_dream"])

if __name__ == "__main__":
    unittest.main()
