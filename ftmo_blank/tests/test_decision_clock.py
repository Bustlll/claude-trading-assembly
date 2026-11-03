"""THE DECISION CLOCK (tools/paper.py, 2026-10-18): the replay stops where the LIVE desk would wake someone --
a decision turn every hour inside the window, the move screen on every closed M5 bar (a symbol re-screened an
hour after its flag), a price alert's condition met AT its bar (not at the next hourly turn) -- instead of
running a whole day and routing everything at its end. Real M5 bars."""
import csv
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOME / "tools"))
sys.path.insert(0, str(HOME))
import paper as P  # noqa: E402

MON = datetime(2026, 1, 5, tzinfo=timezone.utc)   # a Monday in January: broker = UTC+2, Madrid = UTC+1
BROKER_OFFSET = 2 * 3600
FLAT = (1.1000, 1.1001, 1.0999, 1.1000)


def utc(day: int, hh: int, mm: int = 0) -> datetime:
    return MON + timedelta(days=day, hours=hh, minutes=mm)


def make_home(overrides: dict, days: int = 2):
    """M5 EURUSD bars Monday 00:00 UTC onwards, flat unless overrides[(day, hh, mm)] = (o, h, l, c)."""
    tmp = Path(tempfile.mkdtemp())
    (tmp / "feed").mkdir()
    (tmp / "state").mkdir()
    with open(tmp / "feed" / "EURUSD_M5.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t_broker", "o", "h", "l", "c", "session"])
        t = MON
        while t < MON + timedelta(days=days):
            key = ((t - MON).days, t.hour, t.minute)
            o, h, l, c = overrides.get(key, FLAT)
            w.writerow([int(t.timestamp()) + BROKER_OFFSET, o, h, l, c, "day"])
            t += timedelta(minutes=5)
    return P.Paper(home=tmp), tmp


def outbox(tmp):
    path = tmp / "state" / "outbox.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def ramp(day, hh, frm, to):
    """twelve M5 bars over one UTC hour, closing from `frm` to `to`"""
    out = {}
    for i in range(12):
        c = frm + (to - frm) * (i + 1) / 12
        out[(day, hh, i * 5)] = (c, c + 0.0001, c - 0.0001, c)
    return out


def until(p, stop, limit=300):
    """advance_to_decision until it stops for `stop` (hourly decision turns in between are passed over)."""
    for _ in range(limit):
        adv = p.advance_to_decision(2000)
        if adv["stop"] in (stop, "end"):
            return adv
    raise AssertionError(f"no {stop} stop")


class DecisionClockTest(unittest.TestCase):
    def test_a_decision_turn_every_hour_of_the_window_like_live(self):
        p, _ = make_home({})
        stops = []
        while True:
            adv = p.advance_to_decision(2000)
            if adv["stop"] != "reassess" or not adv["at"].startswith("2026-01-05"):
                break
            stops.append(adv["at"][11:16])
            self.assertIn("movers", adv)
        # 07:00 ... 19:00 Madrid (UTC+1 in January): the bars that CLOSE on each hour of the window
        self.assertEqual(stops, [f"{h:02d}:55" for h in range(5, 18)])
        # 2026-10-20: the once-a-day swing turn at 15:45 New York (20:45 UTC in January: the bar opening 20:40 closes it)
        self.assertEqual((adv["stop"], adv["at"]), ("swing", "2026-01-05 20:40 UTC"))
        adv = p.advance_to_decision(2000)
        self.assertEqual((adv["stop"], adv["at"]), ("reassess", "2026-01-06 05:55 UTC"))   # next day's first turn

    def test_an_in_window_move_stops_at_its_own_bar_not_at_the_hour(self):
        p, tmp = make_home(ramp(0, 10, 1.1000, 1.1050))       # 11:00-12:00 Madrid, +0.45% over the hour
        adv = until(p, "scan")
        self.assertEqual(adv["at"], "2026-01-05 10:35 UTC")    # the bar the move crossed 0.30%, mid-hour
        cand = [e for e in outbox(tmp) if e["type"] == "setup_candidate"]
        self.assertEqual(cand[-1]["candidates"][0]["symbol"], "EURUSD")
        self.assertEqual(cand[-1]["utc"], "2026-01-05 10:35 UTC")
        nxt = p.advance_to_decision(2000)                      # flagged: no second scout for it within the hour
        self.assertEqual(nxt["stop"], "reassess")

    def test_a_night_move_wakes_nobody(self):
        p, tmp = make_home(ramp(0, 21, 1.1000, 1.1050))       # 22:00-23:00 Madrid: outside the window
        while not p.advance_to_decision(2000)["at"].startswith("2026-01-06"):
            pass
        self.assertFalse([e for e in outbox(tmp) if e["type"] == "setup_candidate"])

    def test_the_decision_turn_flags_its_movers_so_no_scout_repeats_them(self):
        jump = {(0, 6, 55): (1.1000, 1.1051, 1.0999, 1.1050)}  # one bar, closing on the 08:00 Madrid turn
        p, tmp = make_home(jump)
        until(p, "reassess")                                   # 07:00 Madrid
        adv = p.advance_to_decision(2000)                      # 08:00 Madrid: the turn wins over the screen
        self.assertEqual((adv["stop"], adv["at"]), ("reassess", "2026-01-05 06:55 UTC"))
        self.assertEqual(adv["movers"][0]["symbol"], "EURUSD")
        self.assertIn("EURUSD", p.s["scan_flags"])
        self.assertFalse([e for e in outbox(tmp) if e["type"] == "setup_candidate"])


class AlertTest(unittest.TestCase):
    def test_refuses_a_level_price_is_already_beyond(self):
        p, _ = make_home({})
        p.advance_to_decision(2000)
        r = p.set_alert("EURUSD", 1.0990, None, "would fire at once, so refused")
        self.assertFalse(r["ok"])
        self.assertIn("already above", r["reason"])
        self.assertFalse(p.set_alert("EURUSD", None, None, "no level given at all")["ok"])

    def test_fires_inside_the_window_not_on_a_night_touch(self):
        touch = (1.1000, 1.1025, 1.0999, 1.1000)
        p, tmp = make_home({(0, 22, 0): touch, (1, 7, 0): touch})   # 23:00 Madrid (night), 08:00 Madrid Tue
        while p.advance_to_decision(2000)["at"] != "2026-01-05 07:55 UTC":   # set at the 09:00 Madrid turn
            pass
        r = p.set_alert("EURUSD", 1.1020, None, "short the rejection if M5 closes back below 1.1015", hours=24)
        self.assertTrue(r["ok"])
        adv = until(p, "alert")
        self.assertEqual(adv["at"], "2026-01-06 07:00 UTC")         # the night touch was ignored
        hit = [e for e in outbox(tmp) if e["type"] == "level_alert"]
        self.assertEqual(len(hit), 1)
        self.assertEqual(hit[0]["why"], "short the rejection if M5 closes back below 1.1015")
        self.assertEqual(p.s["alerts"], [])

    def test_expires_on_its_own_and_caps_how_many_wait(self):
        p, tmp = make_home({})
        p.advance_to_decision(2000)
        for lvl in (1.1010, 1.1020, 1.1030):
            self.assertTrue(p.set_alert("EURUSD", lvl, None, "a plan at this level", hours=0.5)["ok"])
        self.assertFalse(p.set_alert("EURUSD", 1.1040, None, "a fourth is one too many")["ok"])
        p.advance(12)                                               # one hour later
        self.assertEqual(p.s["alerts"], [])
        self.assertEqual(len([e for e in outbox(tmp) if e["type"] == "level_alert"]), 0)

    def test_the_alert_survives_a_reload_from_disk(self):
        p, _ = make_home({})
        p.advance_to_decision(2000)
        p.set_alert("EURUSD", 1.1020, None, "a plan written to disk")
        q = P.Paper(home=p.home)
        self.assertEqual(q.s["alerts"][0]["level"], 1.1020)
        p.reload()
        self.assertEqual(p.s["alerts"][0]["level"], 1.1020)


class AlertConditionTest(unittest.TestCase):
    """2026-10-18: touch alerts next to price were used as a 5-minute timer; alerts now carry the real condition."""
    WICK = (1.1000, 1.1015, 1.0999, 1.1000)   # trades through 1.1010, closes back below
    ABOVE = (1.1012, 1.1013, 1.1011, 1.1012)  # closes above 1.1010

    def test_a_touch_next_to_price_is_refused_as_a_poll(self):
        p, _ = make_home({})
        p.advance_to_decision(2000)
        r = p.set_alert("EURUSD", 1.10005, None, "just check again in a bit")
        self.assertFalse(r["ok"])
        self.assertIn("poll", r["reason"])
        self.assertTrue(p.set_alert("EURUSD", 1.10005, None, "close beyond it", when="close", tf="M15")["ok"])

    def test_close_ignores_a_wick_and_fires_on_the_closed_bar(self):
        bars = {(0, 8, 0): self.WICK, **{(0, 9, m): self.ABOVE for m in (0, 5, 10)}}
        p, tmp = make_home(bars)
        p.advance_to_decision(2000)
        self.assertTrue(p.set_alert("EURUSD", 1.1010, None, "S4 BUY on T-CLOSE>1.1010 M15", when="close", tf="M15")["ok"])
        adv = until(p, "alert")
        self.assertEqual((adv["stop"], adv["at"]), ("alert", "2026-01-05 09:10 UTC"))   # the 09:00 M15 closed above
        hit = [e for e in outbox(tmp) if e["type"] == "level_alert"][-1]
        self.assertIn("T-CLOSE", hit["condition"])

    def test_reject_fires_when_a_bar_trades_through_and_closes_back(self):
        p, tmp = make_home({(0, 9, 5): self.WICK})
        p.advance_to_decision(2000)
        p.set_alert("EURUSD", 1.1010, None, "S2 SELL on T-REJECT 1.1010 M15", when="reject", tf="M15")
        adv = until(p, "alert")
        self.assertEqual((adv["stop"], adv["at"]), ("alert", "2026-01-05 09:10 UTC"))

    def test_an_alert_fires_at_its_own_bar_between_hourly_turns(self):
        # operator, 2026-10-18: an alert met at 14:35 must wake its trader at 14:35, not at the 15:00 turn
        p, tmp = make_home({(0, 13, 35): (1.1000, 1.1030, 1.0999, 1.1000)})
        p.advance_to_decision(2000)
        self.assertTrue(p.set_alert("EURUSD", 1.1025, None, "S2 SELL if price trades into 1.1025", hours=12)["ok"])
        adv = until(p, "alert")
        self.assertEqual(adv["at"], "2026-01-05 13:35 UTC")    # the touching bar, 25 min before the next turn

    def test_hold_needs_n_consecutive_closes(self):
        bars = {(0, 9, m): self.ABOVE for m in range(0, 30, 5)}
        p, _ = make_home(bars)
        p.advance_to_decision(2000)
        p.set_alert("EURUSD", 1.1010, None, "S1 BUY on T-HOLD 1.1010 M15x2", when="hold", tf="M15", nbars=2)
        adv = until(p, "alert")
        self.assertEqual((adv["stop"], adv["at"]), ("alert", "2026-01-05 09:25 UTC"))  # the second M15 close


class CompactOutputTest(unittest.TestCase):
    def test_bars_text_is_one_line_per_bar_with_the_day_once(self):
        p, _ = make_home({})
        p.advance_to_decision(2000)
        text = P.bars_text("EURUSD", "M15", p.bars_view("EURUSD", 4, "M15"))
        lines = text.splitlines()
        self.assertTrue(lines[0].startswith("EURUSD M15, last 4 closed bars"))
        self.assertEqual(sum(1 for x in lines if x.startswith("# trading day")), 1)
        self.assertEqual(len(lines), 6)
        self.assertIn("1.1001", lines[-1])


if __name__ == "__main__":
    unittest.main()
