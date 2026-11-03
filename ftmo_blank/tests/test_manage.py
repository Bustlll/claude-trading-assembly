"""2026-10-19: the position manager's wake rules (tools/manage_core.py), the paper twins of its verbs (partial close,
target moves, the risk worker's one widen), the replay stopping for it, and the specialists on call (tools/consult.py)."""
import csv
import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path

HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOME / "tools"))
sys.path.insert(0, str(HOME))
sys.path.insert(0, str(HOME.parent / "selfcall"))

import manage_core as MC  # noqa: E402

UTC = dt.timezone.utc


def bars(start: dt.datetime, closes: list[float], tf_min: int = 5, wick: float = 0.0) -> list[dict]:
    out, o = [], closes[0]
    for i, c in enumerate(closes):
        t = start + dt.timedelta(minutes=tf_min * i)
        out.append({"utc": t.strftime("%Y-%m-%d %H:%M"), "o": o, "h": max(o, c) + wick, "l": min(o, c) - wick, "c": c})
        o = c
    return out


class TriggerTest(unittest.TestCase):
    T0 = dt.datetime(2026, 1, 7, 9, 0, tzinfo=UTC)   # a Wednesday, inside the window

    def pos(self, **kw):
        p = {"symbol": "EURUSD", "side": "buy", "entry": 1.1000, "sl": 1.0990, "sl0": 1.0990, "tp": 1.1030,
             "opened_utc": "2026-01-07 09:00", "why": "S1 retest, INV close <1.0994"}
        p.update(kw)
        return p

    def run_(self, closes, now_min, st=None, in_window=True, m15=None, **kw):
        m5 = bars(self.T0, closes)
        now = self.T0 + dt.timedelta(minutes=now_min)
        return MC.evaluate(self.pos(**kw), m5, m15 or [], now, in_window=in_window, rollover_utc=None,
                           friday_flat_utc=None, st=st)

    def test_r_milestones_fire_once_each(self):
        trig, st, info = self.run_([1.1000, 1.1003, 1.1006], 15)
        self.assertEqual([t["code"] for t in trig], ["R+0.5"])
        self.assertGreaterEqual(info["mfe_r"], 0.5)
        trig, st, _ = self.run_([1.1000, 1.1003, 1.1006], 15, st=st)
        self.assertEqual(trig, [])                                    # never twice
        trig, st, _ = self.run_([1.1000, 1.1006, 1.1011, 1.1016], 20, st=st)
        self.assertEqual([t["code"] for t in trig], ["R+1", "R+1.5"])

    def test_adverse_half_r(self):
        trig, _, info = self.run_([1.1000, 1.0997, 1.0994], 15)
        self.assertIn("R-0.5", [t["code"] for t in trig])

    def test_inv_close_and_structure_against(self):
        m15 = bars(self.T0 - dt.timedelta(minutes=90), [1.1004, 1.1003, 1.1002, 1.1001, 1.1000, 1.1001, 1.0999, 1.0993],
                   tf_min=15)
        trig, _, info = self.run_([1.1000, 1.0998, 1.0996], 15, m15=m15)
        codes = [t["code"] for t in trig]
        self.assertIn("INV", codes)                 # the 09:15 M15 bar closed 1.0993 < 1.0994
        self.assertIn("BOS_AGAINST", codes)         # and below the last 6 M15 bars' low
        self.assertEqual(info["inv"], {"op": "<", "level": 1.0994})

    def test_stall_after_an_hour_with_nothing(self):
        trig, _, _ = self.run_([1.1000, 1.1001] * 7, 65)
        self.assertIn("STALL", [t["code"] for t in trig])

    def test_outside_the_window_only_the_burst_and_the_schedule(self):
        trig, _, _ = self.run_([1.1000, 1.1006, 1.1012, 1.0990], 20, in_window=False)
        self.assertEqual(trig, [])                 # +1R and -1R happened, but nobody is woken at night for them

    def test_pre_swap_once_a_day_and_pre_flatten_on_friday(self):
        m5 = bars(self.T0, [1.1000, 1.1001])
        now = self.T0 + dt.timedelta(minutes=10)
        roll = now + dt.timedelta(minutes=15)
        trig, st, _ = MC.evaluate(self.pos(), m5, [], now, in_window=False, rollover_utc=roll, friday_flat_utc=None,
                                  day="2026-01-07")
        self.assertEqual([t["code"] for t in trig], ["PRE_SWAP"])
        trig, st, _ = MC.evaluate(self.pos(), m5, [], now + dt.timedelta(minutes=5), in_window=False,
                                  rollover_utc=roll, friday_flat_utc=None, st=st, day="2026-01-07")
        self.assertEqual(trig, [])
        far = now + dt.timedelta(minutes=50)
        trig, _, _ = MC.evaluate(self.pos(), m5, [], now, in_window=False, rollover_utc=far,
                                 friday_flat_utc=now + dt.timedelta(minutes=25), day="2026-01-09")
        self.assertEqual([t["code"] for t in trig], ["PRE_FLATTEN"])

    def test_rollover_follows_new_york(self):
        self.assertEqual(MC.rollover_utc(dt.datetime(2026, 10, 19, 19, 0, tzinfo=UTC)).hour, 21)   # NY summer time
        self.assertEqual(MC.rollover_utc(dt.datetime(2026, 11, 3, 19, 0, tzinfo=UTC)).hour, 22)    # NY winter time

    def _night(self, spike_hour_routine: bool):
        start = dt.datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        hist = []
        for i in range(MC.HISTORY_M5):
            t = start + dt.timedelta(minutes=5 * i)
            rng = 0.0010 if (spike_hour_routine and t.hour == 2) else 0.0002
            hist.append({"utc": t.strftime("%Y-%m-%d %H:%M"), "o": 1.1, "h": 1.1 + rng, "l": 1.1, "c": 1.1 + rng / 2})
        last_t = start + dt.timedelta(minutes=5 * MC.HISTORY_M5)
        while last_t.hour != 2:
            last_t += dt.timedelta(minutes=5)
        hist = [r for r in hist if r["utc"] < last_t.strftime("%Y-%m-%d %H:%M")]
        hist.append({"utc": last_t.strftime("%Y-%m-%d %H:%M"), "o": 1.1, "h": 1.1012, "l": 1.1, "c": 1.1010})
        return hist, last_t + dt.timedelta(minutes=5)

    def test_a_night_burst_wakes_it_with_a_cooldown_and_a_cap(self):
        hist, now = self._night(spike_hour_routine=False)
        p = self.pos(opened_utc="2026-01-01 00:00")
        trig, st, info = MC.evaluate(p, hist[-50:], [], now, in_window=False, rollover_utc=None, friday_flat_utc=None,
                                     m5_hist=hist)
        self.assertEqual([t["code"] for t in trig], ["BURST"])
        trig, st, _ = MC.evaluate(p, hist[-50:], [], now + dt.timedelta(minutes=5), in_window=False, rollover_utc=None,
                                  friday_flat_utc=None, m5_hist=hist, st=st)
        self.assertEqual(trig, [])   # cooldown (and the bar is no longer fresh)

    def test_the_routine_asia_open_does_not_wake_it(self):
        hist, now = self._night(spike_hour_routine=True)   # this hour always runs 5x the rest
        trig, _, info = MC.evaluate(self.pos(opened_utc="2026-01-01 00:00"), hist[-50:], [], now, in_window=False,
                                    rollover_utc=None, friday_flat_utc=None, m5_hist=hist)
        self.assertEqual(trig, [])
        self.assertLess(info["burst"]["ratio_same_hour"], MC.BURST_HOUR_K)

    def test_parse_inv_forms(self):
        self.assertEqual(MC.parse_inv("INV M15 close >25580"), {"op": ">", "level": 25580.0})
        self.assertEqual(MC.parse_inv("INV close above 109.70 (gap)"), {"op": ">", "level": 109.7})
        self.assertEqual(MC.parse_inv("invalidation: close below 1.0710"), {"op": "<", "level": 1.071})
        self.assertIsNone(MC.parse_inv("no plan written"))


class WatchTest(unittest.TestCase):
    """2026-10-20: the manager decides whether it needs to be awake again -- a level, a time, or quiet (let it rip)."""
    T0 = dt.datetime(2026, 1, 7, 9, 0, tzinfo=UTC)

    def pos(self):
        return {"symbol": "EURUSD", "side": "buy", "entry": 1.1000, "sl": 1.0990, "sl0": 1.0990, "tp": 1.1030,
                "opened_utc": "2026-01-07 09:00", "why": "S1 retest, INV close <1.0994"}

    def ev(self, closes, now_min, st, in_window=True, **kw):
        return MC.evaluate(self.pos(), bars(self.T0, closes), [], self.T0 + dt.timedelta(minutes=now_min),
                           in_window=in_window, rollover_utc=None, friday_flat_utc=None, st=st, **kw)

    def test_set_watch_validates(self):
        now = self.T0 + dt.timedelta(minutes=10)
        _, r = MC.set_watch({}, now, side="buy", price=1.1005, above=1.1003, why="the trail trigger level")
        self.assertFalse(r["ok"])                     # already above it
        _, r = MC.set_watch({}, now, side="buy", price=1.1005, in_min=2, why="too soon to be a real re-wake")
        self.assertFalse(r["ok"])
        _, r = MC.set_watch({}, now, side="buy", price=1.1005, why="nothing asked for at all here")
        self.assertFalse(r["ok"])
        st, r = MC.set_watch({"fired": ["R+0.5"]}, now, side="buy", price=1.1005, above=1.1012, in_min=30,
                             why="trail above the range high")
        self.assertTrue(r["ok"])
        self.assertEqual(st["fired"], ["R+0.5"])      # the trigger history is kept
        self.assertEqual(st["watch"]["above"], 1.1012)

    def test_quiet_lets_it_rip_and_the_friday_notice_still_comes(self):
        now = self.T0 + dt.timedelta(minutes=5)
        st, _ = MC.set_watch({}, now, side="buy", price=1.1001, quiet=True, why="plan on track, let it rip to the target")
        trig, st, info = self.ev([1.1000, 1.1006, 1.1012], 15, st)
        self.assertEqual(trig, [])
        self.assertEqual(info["silenced"], ["R+0.5", "R+1"])
        trig, st, _ = MC.evaluate(self.pos(), bars(self.T0, [1.1000, 1.1006]), [], self.T0 + dt.timedelta(minutes=20),
                                  in_window=True, rollover_utc=None, st=st,
                                  friday_flat_utc=self.T0 + dt.timedelta(minutes=45))
        self.assertEqual([t["code"] for t in trig], ["PRE_FLATTEN"])

    def test_a_quiet_spell_ends_and_silenced_wakes_do_not_come_back_late(self):
        now = self.T0 + dt.timedelta(minutes=5)
        st, _ = MC.set_watch({}, now, side="buy", price=1.1001, quiet=True, quiet_min=10, why="ten quiet minutes, then normal")
        trig, st, _ = self.ev([1.1000, 1.1006], 10, st)
        self.assertEqual(trig, [])                    # R+0.5 silenced during the spell
        trig, st, _ = self.ev([1.1000, 1.1006, 1.1007, 1.1008], 20, st)
        self.assertEqual(trig, [])                    # the spell is over, but R+0.5 was acknowledged, not owed
        trig, st, _ = self.ev([1.1000, 1.1006, 1.1007, 1.1012], 20, st)
        self.assertEqual([t["code"] for t in trig], ["R+1"])
        self.assertIsNone(st["watch"])

    def test_its_own_level_and_time_wake_it_even_at_night(self):
        now = self.T0 + dt.timedelta(minutes=5)
        st, _ = MC.set_watch({}, now, side="buy", price=1.1001, below=1.0996, in_min=60, quiet=True,
                             why="quiet unless the range low breaks; look again in an hour")
        trig, st, _ = self.ev([1.1000, 1.1001, 1.0995], 15, st, in_window=False)
        self.assertEqual([t["code"] for t in trig], ["WATCH_BELOW"])
        trig, st, _ = self.ev([1.1000, 1.1001, 1.0995], 70, st, in_window=False)
        self.assertEqual([t["code"] for t in trig], ["WATCH_TIME"])
        self.assertTrue(st["watch"]["quiet"])         # still quiet: only its own wakes came

    def test_both_desks_store_the_same_watch(self):
        import live_broker as LB
        import live_loop as LL
        import live_market as LM
        from live_fakes import FakeMT5, pos as fpos
        tmp = Path(tempfile.mkdtemp())
        (tmp / "state").mkdir()
        fake = FakeMT5(positions=[fpos(7, "EURUSD", "buy", 0.1, 1.1000, sl=1.0990, magic=LB.MAGIC)],
                       tick_bid=1.10050, tick_ask=1.10060)
        saved = LB.LEDGER_PATH
        LB.LEDGER_PATH = tmp / "state" / "live_ledger.jsonl"
        try:
            now = dt.datetime(2026, 1, 7, 9, 30, tzinfo=UTC)
            r = LM.watch("EURUSD", 7, above=1.1020, in_min=45, why="trail once the range high breaks", m=fake,
                         home=tmp, now=now)
            self.assertTrue(r["ok"], r)
            self.assertEqual(LL._read(LL._manage_path(tmp), {})["7"]["watch"]["above"], 1.1020)
            r = LM.watch("EURUSD", 8, quiet=True, why="not ours: must be refused", m=fake, home=tmp, now=now)
            self.assertFalse(r["ok"])
            theirs = FakeMT5(positions=[fpos(9, "EURUSD", "buy", 0.1, 1.1000, sl=1.0990, magic=313700)])
            r = LM.watch("EURUSD", 9, quiet=True, why="a delta runner's position: refused", m=theirs, home=tmp, now=now)
            self.assertFalse(r["ok"])
        finally:
            LB.LEDGER_PATH = saved


class PaperManageTest(unittest.TestCase):
    """The paper twins: close --volume, modify --tp, widen (risk only), and the replay stopping for the manager."""

    def setUp(self):
        import paper as P
        self.P = P
        self.tmp = Path(tempfile.mkdtemp())
        for d in ("feed", "state"):
            (self.tmp / d).mkdir()
        import live_broker as LB
        t = dt.datetime(2026, 1, 7, 8, 0, tzinfo=UTC)
        px = 1.1000
        with open(self.tmp / "feed" / "EURUSD_M5.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["t_broker", "o", "h", "l", "c", "session"])
            for i in range(60):
                c = round(px + 0.0001, 5)
                w.writerow([LB.utc_to_broker_epoch(t + dt.timedelta(minutes=5 * i)), px, c + 0.00005, px - 0.00005, c, "day"])
                px = c
        self.p = P.Paper(home=self.tmp)
        self.p.advance(2)

    def open_one(self, lots=0.2):
        r = self.p.place_order("EURUSD", "buy", lots, 1.0980, 1.1100, "a test entry with a real reason")
        self.assertTrue(r["ok"], r)
        self.p.advance(1)
        self.assertEqual(len(self.p.s["positions"]), 1)
        return self.p.s["positions"][0]

    def test_partial_close_then_the_rest_carries_the_whole_result(self):
        pos = self.open_one(0.2)
        r = self.p.close_position(pos["id"], "bank half at the first level", "EURUSD", volume=0.1)
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["partial"])
        self.p.advance(1)
        self.assertAlmostEqual(self.p.s["positions"][0]["lots"], 0.1)
        self.assertNotEqual(self.p.s["positions"][0]["realized_usd"], 0.0)
        self.p.close_position(pos["id"], "close the rest at the level", "EURUSD")
        self.p.advance(1)
        rows = [json.loads(x) for x in (self.tmp / "state" / "ledger.jsonl").read_text().splitlines()]
        closed = [r for r in rows if r["type"] == "trade_closed"][-1]
        parts = [r for r in rows if r["type"] == "partial_closed"]
        self.assertEqual(len(parts), 1)
        self.assertEqual(closed["lots"], 0.2)                          # the trade's own size
        self.assertEqual(len(closed["partial_closes"]), 1)

    def test_target_moves_and_stop_only_toward_profit(self):
        pos = self.open_one()
        self.assertTrue(self.p.modify_position(pos["id"], None, 1.1200, "extend to the next level", "EURUSD")["ok"])
        self.assertEqual(self.p.s["positions"][0]["tp"], 1.1200)
        r = self.p.modify_position(pos["id"], 1.0970, None, "a wider stop through modify", "EURUSD")
        self.assertFalse(r["ok"])
        self.assertIn("widen", r["reason"])

    def test_widen_once_and_at_most_twice_the_entry_risk(self):
        pos = self.open_one()
        r = self.p.widen_stop(pos["id"], 1.0900, "far past twice the entry risk, a different trade", "EURUSD")
        self.assertFalse(r["ok"])
        r = self.p.widen_stop(pos["id"], 1.0975, "INV is a close below 1.0980; the sweeps reach 1.0977", "EURUSD")
        self.assertTrue(r["ok"], r)
        r = self.p.widen_stop(pos["id"], 1.0970, "a second widen on the same trade is refused", "EURUSD")
        self.assertFalse(r["ok"])
        self.assertIn("already widened", r["reason"])

    def test_the_own_rails_are_risk_levers_on_paper_too(self):
        self.assertEqual(self.p.lever("max_positions"), 5)
        self.assertFalse(self.p.set_risk_levers("past the daily line on purpose", day_halt=0.06)["ok"])
        r = self.p.set_risk_levers("one position at a time while the edge is unproven", max_positions=1)
        self.assertTrue(r["ok"], r)
        self.open_one()
        r = self.p.place_order("EURUSD", "buy", 0.1, 1.0980, 1.1100, "a second position past the cap")
        self.assertFalse(r["ok"])
        self.assertIn("max concurrent positions", r["reason"])
        self.assertEqual(self.p.risk_factor()["levers"]["max_positions"]["value"], 1)

    def test_paper_watch_quiet_silences_the_replay_wakes(self):
        pos = self.open_one()
        pos["sl0"] = pos["sl"] = round(pos["entry"] - 0.0004, 5)
        self.p.save()
        r = self.p.set_watch(pos["id"], "EURUSD", quiet=True, why="let it rip to the target")
        self.assertTrue(r["ok"], r)
        stops = [self.p.advance_to_decision(6)["stop"] for _ in range(3)]
        self.assertNotIn("manage", stops)

    def test_the_replay_stops_for_the_manager(self):
        pos = self.open_one()
        pos["sl0"] = pos["sl"] = round(pos["entry"] - 0.0004, 5)   # a tight stop so +0.5R comes within a few bars
        self.p.save()
        stops = [self.p.advance_to_decision(6)["stop"] for _ in range(4)]
        self.assertIn("manage", stops)
        rows = [json.loads(x) for x in (self.tmp / "state" / "outbox.jsonl").read_text().splitlines()]
        wakes = [r for r in rows if r["type"] == "manage_wake"]
        self.assertTrue(wakes and wakes[0]["triggers"][0]["code"].startswith("R+"))


class ConsultTest(unittest.TestCase):
    """The specialist answers inside the asker's turn: stored as its child, posted to the board, logged, printed."""

    def test_consult_runs_the_specialist_now_and_records_it(self):
        import consult as C
        from guard import policy as PO
        from selfcall.engine import Engine
        tmp = Path(tempfile.mkdtemp())
        (tmp / "state").mkdir()
        calls = []

        def fake(d):
            calls.append(d)
            return {"answer": {"status": "done", "summary": "CHART VERDICT TAKE | level 1.1000 | stop 1.0985",
                               "findings": ["the 1.1000 shelf held 3 times"], "sources": [], "next_demands": []},
                    "cost_usd": 0.04, "session_id": "s", "denials": []}

        eng = Engine(home=tmp, kinds=PO.LIVE_KINDS, limits=PO.LIVE_LIMITS, invoker=fake)
        parent = eng.store.add("live_trade_eurusd", "a trader turn", "", None, 1, "event:x", dedupe=False)
        res = C.consult("live", "chart", "EURUSD", "is 1.1000 real support for a buy here?", "THE TRADE: buy",
                        parent=parent, view_builder=lambda *a: ("THE SYMBOL FILE x", eng), home=tmp)
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["summary"].startswith("CHART VERDICT"))
        self.assertEqual(calls[0]["kind"], "live_chart")
        self.assertIn("THE SYMBOL FILE x", calls[0]["prompt"])
        row = eng.store.get(res["demand"])
        self.assertEqual((row["parent"], row["source"], row["depth"], row["status"]), (parent, "consult", 2, "done"))
        posts = [json.loads(x) for x in (tmp / "state" / "live_board.jsonl").read_text().splitlines()]
        self.assertEqual((posts[-1]["worker"], posts[-1]["topic"], posts[-1]["symbol"]), ("chart", "consult", "EURUSD"))
        self.assertTrue((tmp / "state" / "live_consults.jsonl").exists())
        for _ in range(2):
            C.consult("live", "timing", "EURUSD", "now or after the open settles?", "x", parent=parent,
                      view_builder=lambda *a: ("CLOCK x", eng), home=tmp)
        res = C.consult("live", "chart", "EURUSD", "a fourth question in one turn", "x", parent=parent,
                        view_builder=lambda *a: ("x", eng), home=tmp)
        self.assertFalse(res["ok"])
        self.assertIn("at most 3", res["reason"])

    def test_a_failed_specialist_never_blocks_the_asker(self):
        import consult as C
        from guard import policy as PO
        from selfcall.engine import Engine
        tmp = Path(tempfile.mkdtemp())
        (tmp / "state").mkdir()

        def boom(d):
            raise RuntimeError("timed out")

        eng = Engine(home=tmp, kinds=PO.LIVE_KINDS, limits=PO.LIVE_LIMITS, invoker=boom)
        res = C.consult("live", "timing", "EURUSD", "is now a good time to enter?", "x",
                        view_builder=lambda *a: ("x", eng), home=tmp)
        self.assertFalse(res["ok"])
        self.assertIn("decide without it", res["reason"])


if __name__ == "__main__":
    unittest.main()


class SwingTurnTest(unittest.TestCase):
    """2026-10-20: the once-a-day swing turn at the old training turn's moment (15:45 New York, Monday-Thursday)."""

    def test_swing_key_follows_new_york_and_skips_friday(self):
        k = MC.swing_key(dt.datetime(2026, 10, 20, 19, 45, tzinfo=UTC))      # Tuesday, NY summer time: 15:45 NY
        self.assertEqual(k, "2026-10-20")
        self.assertIsNone(MC.swing_key(dt.datetime(2026, 10, 20, 19, 44, tzinfo=UTC)))
        self.assertIsNone(MC.swing_key(dt.datetime(2026, 10, 20, 20, 50, tzinfo=UTC)))   # more than an hour late
        self.assertIsNone(MC.swing_key(dt.datetime(2026, 10, 23, 19, 50, tzinfo=UTC)))   # Friday
        self.assertEqual(MC.swing_key(dt.datetime(2026, 11, 3, 20, 50, tzinfo=UTC)), "2026-11-03")   # NY winter

    def _rows(self, start, closes):
        return bars(start, closes)

    def test_day_movers_measure_from_the_days_first_open(self):
        start = dt.datetime(2026, 1, 6, 8, 0, tzinfo=UTC)
        rows = {"A": self._rows(start, [100 + 0.1 * i for i in range(30)]), "B": self._rows(start, [50.0] * 30),
                "C": self._rows(start, [10.0] * 5)}
        mv = MC.day_movers(rows, dt.datetime(2026, 1, 5, 23, 0, tzinfo=UTC))
        self.assertEqual([m["symbol"] for m in mv], ["A", "B"])     # C has too few bars today
        self.assertGreater(mv[0]["pct_day"], 2.8)
        self.assertEqual(mv[0]["close_in_range"], 1.0)

    def test_both_desks_take_the_swing_turn_once_on_the_same_day(self):
        import live_broker as LB
        import live_loop as LL
        import paper as P
        from live_fakes import FakeMT5
        tmp = Path(tempfile.mkdtemp())
        for d in ("feed", "state"):
            (tmp / d).mkdir()
        t0 = dt.datetime(2026, 1, 6, 8, 0, tzinfo=UTC)      # a Tuesday; NY winter: the swing slot opens 20:45 UTC
        rows, px = [], 1.1
        for i in range(170):
            c = round(px + 0.00005, 5)
            rows.append({"t": LB.utc_to_broker_epoch(t0 + dt.timedelta(minutes=5 * i)), "o": px, "h": c, "l": px, "c": c})
            px = c
        with open(tmp / "feed" / "EURUSD_M5.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["t_broker", "o", "h", "l", "c", "session"])
            for r in rows:
                w.writerow([r["t"], r["o"], r["h"], r["l"], r["c"], "day"])
        p = P.Paper(home=tmp)
        train = []
        while p._bar():
            what, got = p._clock(p.t_now())
            if what == "swing":
                train.append((p.s["swing_day"], [m["symbol"] for m in got], P.bar_utc(p.t_now() + 300).strftime("%H:%M")))

        class ClockFake(FakeMT5):
            clock_b = 0

            def copy_rates_from_pos(self, symbol, timeframe, start_pos, count):
                if symbol != "EURUSD" or timeframe != self.TIMEFRAME_M5:
                    return []
                got = [{"time": r["t"], "open": r["o"], "high": r["h"], "low": r["l"], "close": r["c"]}
                       for r in rows if r["t"] <= self.clock_b]
                return got[max(0, len(got) - count):]

        fake, st, live = ClockFake(), {}, []
        saved = LB._now_utc
        try:
            clock = t0
            while clock < t0 + dt.timedelta(minutes=5 * 170):
                clock += dt.timedelta(minutes=5)
                LB._now_utc = (lambda c: (lambda: c))(clock)
                fake.clock_b = LB.utc_to_broker_epoch(clock)
                ev = LL.swing_event(fake, {"market_open": True}, clock, st)
                if ev:
                    live.append((ev["key"], [m["symbol"] for m in ev["movers"]], clock.strftime("%H:%M")))
        finally:
            LB._now_utc = saved
        self.assertEqual(train, live)
        self.assertEqual(train, [("2026-01-06", ["EURUSD"], "20:45")])
