"""THE SHARED DESK (tools/desk_shared.py) and PATHWAYS (tools/pathways.py), 2026-10-18.

What a twin worker sees must be the same on both desks (parity_check info.* drives both), and the lessons a
worker gets must be the ones on its situation's path, each in full, with everything else named, never hidden.
Also the replay's bar views: the bucket the last closed M5 bar completes is closed, not forming.
"""
import csv
import datetime as dt
import io
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

HOME = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(HOME / "tools"), str(HOME / "agent_code"), str(HOME)]

import desk_shared as DS  # noqa: E402
import pathways as PW  # noqa: E402
import paper as P  # noqa: E402


def _home():
    tmp = Path(tempfile.mkdtemp(prefix="desk_shared_"))
    for d in ("state", "papers", "feed"):
        (tmp / d).mkdir()
    return tmp


def _post(utc, symbol, text, topic="lesson", worker="review", **kw):
    return {"utc": utc, "worker": worker, "topic": topic, "symbol": symbol, "text": text, **kw}


class PathwaysTest(unittest.TestCase):
    def setUp(self):
        self.home = _home()
        board = [_post("2025-05-01 10:00 UTC", "XAUUSD", "XAUUSD S4 breakout chase at the London open, stop 1 pt past "
                       "the swing: -1R. Wait for the retest."),
                 _post("2025-05-02 10:00 UTC", "US100.CASH", "US100 stopped inside the US open hour 13:30 UTC; stop "
                       "clearance must cover the cash-open noise."),
                 _post("2025-05-03 10:00 UTC", "EURJPY", "EURJPY range fade, nothing special."),
                 _post("2025-05-03 11:00 UTC", "XAUUSD", "not a lesson", topic="scan", worker="scout"),
                 _post("2025-06-20 10:00 UTC", "XAUUSD", "A LATER EPOCH'S LESSON the replay has not lived yet")]
        (self.home / "state" / "board.jsonl").write_text("\n".join(json.dumps(r) for r in board) + "\n")
        (self.home / "state" / "live_board.jsonl").write_text(json.dumps(
            _post("2026-10-14 12:00 UTC", "XAUUSD", "LIVE: gold entry slipped 4 pts at the open.", day="2026-10-14")) + "\n")
        (self.home / "papers" / "xauusd_trend_notes.md").write_text("# Gold trend notes\n\nS1 continuation after "
                                                                   "acceptance; entries at the retest.\n")
        (self.home / "papers" / "future_day.md").write_text("# About 2025-07-01\n\nnot yet happened\n")
        (self.home / "state" / "ledger.jsonl").write_text(json.dumps({"type": "note", "utc": "2025-05-10 09:00"}) + "\n")

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def test_cards_are_tagged_by_symbol_code_and_session(self):
        cs = {c["id"]: c for c in PW.cards("training", self.home)}
        c = cs["L1"]
        self.assertEqual(c["own_symbol"], "XAUUSD")
        self.assertIn("S4", c["codes"])
        self.assertIn("A1", c["codes"])           # "chase" is the vocabulary's A1
        self.assertIn("london", c["sessions"])
        self.assertEqual(cs["L2"]["own_symbol"], "US100.cash")   # US100.CASH on the board -> the broker's spelling
        self.assertIn("us_open", cs["L2"]["sessions"])
        self.assertNotIn("L4", cs)                # a scan post is not a lesson

    def test_training_is_blind_to_its_future_and_to_live(self):
        ids = {c["id"] for c in PW.cards("training", self.home)}
        self.assertNotIn("L5", ids)               # dated after the replay's now
        self.assertNotIn("P:future_day", ids)     # names a day after the replay's now
        self.assertFalse(any(i.startswith("LL") for i in ids))   # live reaches training only as dreams
        live = {c["id"] for c in PW.cards("live", self.home)}
        self.assertTrue({"L5", "LL1", "P:future_day"} <= live)

    def test_the_situations_own_symbol_comes_first_and_unrelated_cards_stay_off_the_path(self):
        sit = PW.situation(["XAUUSD"], ["entry"], dt.datetime(2025, 5, 9, 8, 0, tzinfo=dt.timezone.utc))
        path = [c["id"] for _, _, c in PW.walk(sit, "training", self.home)]
        self.assertEqual(path[0], "L1")
        self.assertNotIn("L3", path)              # another symbol, nothing in common

    def test_fx_relatives_share_a_non_dollar_currency(self):
        self.assertIn("EURJPY", PW.relatives("EURUSD"))
        self.assertNotIn("USDJPY", PW.relatives("EURUSD"))
        self.assertIn("US30.cash", PW.relatives("US100.cash"))

    def test_sessions_follow_dst(self):
        summer = dt.datetime(2025, 7, 2, 13, 40, tzinfo=dt.timezone.utc)   # 09:40 New York
        winter = dt.datetime(2025, 1, 8, 14, 40, tzinfo=dt.timezone.utc)   # 09:40 New York
        self.assertIn("us_open", PW.sessions_at(summer))
        self.assertIn("us_open", PW.sessions_at(winter))
        self.assertNotIn("us_open", PW.sessions_at(dt.datetime(2025, 1, 8, 13, 40, tzinfo=dt.timezone.utc)))

    def test_brief_prints_whole_cards_and_names_the_rest(self):
        sit = PW.situation(["XAUUSD", "US100.cash"], ["entry"], dt.datetime(2025, 5, 9, 13, 40, tzinfo=dt.timezone.utc))
        cs = PW.cards("training", self.home)
        text = PW.brief(sit, "training", "scout", self.home, max_full=1, corpus=cs)
        full = [c for c in cs if c["text"] in text]
        self.assertEqual(len(full), 1)            # exactly one card printed, and printed whole
        self.assertIn("more on this path", text)  # 2026-11-01: an entry worker gets the count, not the names
        self.assertNotIn("THE WHOLE LIBRARY", text)
        text = PW.brief(sit, "training", "review", self.home, max_full=1, corpus=cs)
        self.assertIn("ALSO ON THIS PATH", text)  # a review still sees the others named
        text = PW.brief(sit, "training", "cli", self.home, max_full=1, corpus=cs)
        self.assertIn("THE WHOLE LIBRARY", text)  # and the CLI the whole map

    def test_calibration_never_reaches_an_entry_decision(self):
        """2026-11-01: the blind-ruler record read as "every direction loses, skip" in front of every entry."""
        cal = PW._card("C:XAUUSD", "calibration", "CALIBRATION XAUUSD: with the move -0.20R A1", "2025-05-08", "XAUUSD")
        rev = PW._card("L99", "lesson", "[2025-05-08 04:55 UTC] review/lesson: CALIBRATION XAUUSD (5-day): KEEP A1",
                       "2025-05-08", "XAUUSD")
        take = PW._card("L98", "lesson", "[2025-05-08 04:55 UTC] review/lesson: TAKE XAUUSD: S2 at the retest, A1 entry",
                        "2025-05-08", "XAUUSD")
        sit = PW.situation(["XAUUSD"], ["entry"], None)
        for role in ("scout", "trader", "decider"):
            text = PW.brief(sit, "training", role, self.home, corpus=[cal, rev, take])
            self.assertNotIn("CALIBRATION XAUUSD", text, role)
            self.assertIn("TAKE XAUUSD", text, role)
        self.assertIn("CALIBRATION XAUUSD", PW.brief(sit, "training", "review", self.home, corpus=[cal, rev, take]))

    def test_no_skip_verdict_reaches_an_entry_decision(self):
        """2026-11-01, ablation (todos/009): without any calibration card the library still dismissed 15 of 21 moments."""
        vault = PW._card("L1", "lesson", "[2025-05-08 04:55 UTC] vault/vault_lesson: XAUUSD enter only when flat", "2025-05-08", "XAUUSD")
        flat = PW._card("L2", "lesson", "[2025-05-08 04:55 UTC] review/lesson: XAUUSD flat day, rejections kept losers out", "2025-05-08", "XAUUSD")
        win = PW._card("L3", "lesson", "[2025-05-08 04:55 UTC] review/lesson: XAUUSD id3 S4 RETEST WIN +1.6R, entry at the shelf", "2025-05-08", "XAUUSD")
        sit = PW.situation(["XAUUSD"], ["entry"], None)
        for role in ("scout", "trader", "decider"):
            text = PW.brief(sit, "training", role, self.home, corpus=[vault, flat, win])
            self.assertIn("id3 S4 RETEST WIN", text)
            self.assertNotIn("enter only when flat", text)
            self.assertNotIn("rejections kept", text)

    def test_interleave_gives_each_symbol_its_best_card_early(self):
        sit = PW.situation(["XAUUSD", "US100.cash"], ["entry"], None)
        order = [c["id"] for _, _, c in PW._interleave(PW.walk(sit, "training", self.home), sit["symbols"])]
        self.assertEqual(set(order[:2]), {"L1", "L2"})

    def test_both_lessons_verbs_run_on_their_own_desk(self):
        import board as TB
        import live_board as LBd
        out = io.StringIO()
        with redirect_stdout(out):
            TB.main(["lessons", "--symbol", "XAUUSD"], home=self.home)
        self.assertIn("LESSONS ON THIS PATH", out.getvalue())
        self.assertNotIn("LIVE: gold", out.getvalue())
        out = io.StringIO()
        with redirect_stdout(out):
            LBd.main(["lessons", "--symbol", "XAUUSD"], home=self.home)
        self.assertIn("LIVE: gold", out.getvalue())
        out = io.StringIO()
        with redirect_stdout(out):
            TB.main(["lessons", "--show", "L2"], home=self.home)
        self.assertIn("cash-open noise", out.getvalue())


class ViewTest(unittest.TestCase):
    def test_packets_attach_the_first_movers_and_name_the_rest(self):
        rows = [{"utc": "2026-01-07 10:45", "day": "2026-01-07", "tf": "M15", "session": "day",
                 "o": 1.0, "h": 1.5, "l": 0.5, "c": 1.25}]
        text = DS.packets(lambda s, tf, n: [{**rows[0], "tf": tf}], ["A", "B", "C", "D"], "BARS")
        for sym in "ABC":
            for tf, _ in DS.PACKET:
                self.assertIn(f"{sym} {tf}, last 1 closed bars", text)
        self.assertIn("NOT ATTACHED (read before judging them): D", text)
        self.assertIn("2026-01-07 10:45 day 1.0 1.5 0.5 1.25", text)   # full precision, every field

    def test_thread_is_today_newest_whole_posts_within_budget(self):
        posts = [_post(f"2026-01-07 0{i}:00 UTC", "XAUUSD", f"post {i} " + "x" * 600, topic="scan") for i in range(6)]
        posts.insert(0, _post("2026-01-06 09:00 UTC", "XAUUSD", "yesterday", topic="scan"))
        text = DS.thread(posts, "2026-01-07", "XAUUSD", lambda p: p["utc"][:10])
        self.assertNotIn("yesterday", text)
        self.assertIn("post 5", text)                                  # the newest is always there
        self.assertIn("earlier one(s) not shown", text)
        self.assertLess(len(text), DS.THREAD_CHARS + 400)
        for i in range(6):                                             # a post is whole or absent
            if f"post {i}" in text:
                self.assertIn(f"post {i} " + "x" * 600, text)

    def test_an_oversized_turn_keeps_the_turn_and_points_to_the_lessons(self):
        build = lambda lessons: "x" * (DS.PROMPT_FIT - 1_000) + lessons
        self.assertEqual(DS.fit_lessons(build, "L" * 500, "CMD"), "x" * (DS.PROMPT_FIT - 1_000) + "L" * 500)
        out = DS.fit_lessons(build, "L" * 5_000, "CMD")
        self.assertLessEqual(len(out), DS.PROMPT_FIT)
        self.assertIn("CMD --symbol S", out)

    def test_both_decision_turns_stay_under_the_cap_with_huge_lessons(self):
        import routing as TR
        import live_routing as LR
        from guard import policy as PO
        snap = {"bar_index": 0, "bars_total": 0, "utc": "-", "epoch": 0, "window_index": 0, "windows_per_epoch": 1,
                "window_day": 1, "window_business_days": 10, "attempt": 1, "pass_rate": None, "attempts_total": 0}
        big = "LESSONS ON THIS PATH " + "y" * 30_000
        _, t = TR.route("turn_reassess", {"snapshot": snap, "goals": [], "lessons_text": big})
        _, l = LR.route("live_reassess", {"snapshot": {}, "lessons_text": big})
        for text in (t, l):
            self.assertLess(len(text), PO.LIMITS["max_prompt_chars"])
            self.assertIn("lessons --symbol S walks", text)

    def test_checklist_core_is_word_for_word_on_both_desks(self):
        for desk in ("training", "live"):
            self.assertIn(DS.ENTRY_RULES_CORE, DS.entry_rules(desk))
        self.assertEqual(DS.entry_rules("training"), DS.entry_rules("live"))   # same terms, same rules (2026-10-18)


class ReplayBarsTest(unittest.TestCase):
    """The bucket the last closed M5 bar completes is CLOSED (2026-10-18): before, at every top-of-the-hour stop
    the M15 and H1 bars that had just closed were dropped as forming -- one bar behind the live broker."""

    def setUp(self):
        self.home = _home()
        t0 = dt.datetime(2026, 1, 7, 9, 0, tzinfo=dt.timezone.utc)
        import live_broker as LB
        with open(self.home / "feed" / "EURUSD_M5.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["t_broker", "o", "h", "l", "c", "session"])
            for k in range(24):   # 09:00 .. 10:55, the last bar closes at 11:00
                px = 1.1 + k / 10_000
                w.writerow([LB.utc_to_broker_epoch(t0 + dt.timedelta(minutes=5 * k)), px, px, px, px, "day"])
        self.p = P.Paper(home=self.home)

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def test_top_of_the_hour_shows_the_bars_that_just_closed(self):
        self.p.s["cursor"] = len(self.p.timeline)          # the 10:55 bar has closed: it is 11:00
        self.assertEqual(self.p.bars_view("EURUSD", 1, "M15")[-1]["utc"], "2026-01-07 10:45")
        self.assertEqual(self.p.bars_view("EURUSD", 1, "H1")[-1]["utc"], "2026-01-07 10:00")
        self.assertEqual(self.p.recent_bars("EURUSD", 1, "H1")[-1]["utc"], "2026-01-07 10:00")

    def test_mid_bucket_never_shows_a_forming_bar(self):
        self.p.s["cursor"] = len(self.p.timeline) - 1      # it is 10:55: the 10:45 M15 and 10:00 H1 are forming
        self.assertEqual(self.p.bars_view("EURUSD", 1, "M15")[-1]["utc"], "2026-01-07 10:30")
        self.assertEqual(self.p.bars_view("EURUSD", 1, "H1")[-1]["utc"], "2026-01-07 09:00")
        last_close = self.p.bars_view("EURUSD", 1, "M5")[-1]["c"]
        self.assertLessEqual(self.p.bars_view("EURUSD", 1, "M15")[-1]["c"], last_close)  # nothing from the future


if __name__ == "__main__":
    unittest.main()


class PassGradeTest(unittest.TestCase):
    """tools/pass_grade.py: a met-and-declined plan is replayed on the bars after it, with its own stop/target."""

    def test_the_plan_grammar_is_read_and_distances_are_not_prices(self):
        import pass_grade as PG
        p = PG.parse_plan("XAUUSD S2 FAILED-CTR buy @~3347 T-REJECT ... | SL@S 3336 (>6 under sweep) TP 3360")
        self.assertEqual((p["side"], p["sl"], p["tp"]), ("buy", 3336.0, 3360.0))
        self.assertIsNone(PG.parse_plan("S4 sell RETEST, SL@S 8+ over retest high, TP 3265")["sl"])
        self.assertTrue(PG.parse_plan("T-CLOSE M15 >3318 then RETEST T-REJECT, SL@S <3304 TP 3345")["two_step"])
        self.assertEqual(PG.parse_plan("plan without a side", "above", "reject")["side"], "sell")

    def _bars(self, path):
        return [{"utc": f"2025-06-04 11:{30 + 5 * i:02d}", "o": c, "h": h, "l": l, "c": c} for i, (h, l, c) in enumerate(path)]

    def test_stop_first_and_target_first_and_a_bar_reaching_both_counts_as_the_stop(self):
        import pass_grade as PG
        base = {"id": 1, "symbol": "XAUUSD", "touched_utc": "2025-06-04 11:25 UTC", "dir": "below", "when": "reject",
                "bar": {"o": 3346, "h": 3347, "l": 3343, "c": 3346.0}, "why": "S2 buy SL 3336 TP 3360"}
        self.assertIn("TARGET first", PG.grade(base, self._bars([(3350, 3344, 3349), (3361, 3348, 3360)]))["result"])
        self.assertIn("STOP first", PG.grade(base, self._bars([(3350, 3335, 3340)]))["result"])
        self.assertIn("STOP first", PG.grade(base, self._bars([(3361, 3335, 3350)]))["result"])
        self.assertIn("neither", PG.grade(base, self._bars([(3350, 3344, 3349)]))["result"])

    def test_only_untaken_alerts_are_passes(self):
        import pass_grade as PG
        rows = [{"type": "level_alert", "id": 1, "symbol": "XAUUSD", "touched_utc": "2025-06-04 11:25 UTC", "day": "d"},
                {"type": "level_alert", "id": 2, "symbol": "EURUSD", "touched_utc": "2025-06-04 12:00 UTC", "day": "d"},
                {"type": "order_placed", "symbol": "EURUSD", "utc": "2025-06-04 12:05 UTC"},
                {"type": "note", "utc": "2025-06-04 11:30 UTC", "text": "declined: pierce only 1 pt"}]
        ps = PG.passes(rows, "d", lambda r: r.get("day"))
        self.assertEqual([p["id"] for p in ps], [1])
        self.assertEqual(ps[0]["decision_note"], "declined: pierce only 1 pt")


class LiveAlertsTest(unittest.TestCase):
    """tools/live_alerts.py on a fake broker whose clock moves: storage, refusal, firing once, expiry, window."""

    def setUp(self):
        sys.path.insert(0, str(HOME / "tests"))
        import live_broker as LB
        from live_fakes import FakeMT5
        self.LB = LB
        self.home = _home()
        self.saved = (LB._now_utc, LB.LEDGER_PATH)
        LB.LEDGER_PATH = self.home / "state" / "live_ledger.jsonl"
        t0 = dt.datetime(2026, 1, 7, 7, 0, tzinfo=dt.timezone.utc)
        m5 = []
        for k in range(48):   # 07:00 .. 10:55, a rise to 1.1047 then a drop
            px = 1.1 + (k if k < 30 else 60 - k) / 10_000 * 1.6
            m5.append({"time": LB.utc_to_broker_epoch(t0 + dt.timedelta(minutes=5 * k)), "open": px, "high": px + 2e-5,
                       "low": px - 2e-5, "close": px})
        import paper as Pp
        rows = [{"t": r["time"], "o": r["open"], "h": r["high"], "l": r["low"], "c": r["close"], "session": "day"} for r in m5]
        m15 = [{"time": b["t"], "open": b["o"], "high": b["h"], "low": b["l"], "close": b["c"]}
               for b in Pp.resample(rows, "M15", rows[-1]["t"] + 10_000)]

        class Clock(FakeMT5):
            clock_b = 0

            def copy_rates_from_pos(s, symbol, timeframe, start_pos, count):
                src = {FakeMT5.TIMEFRAME_M5: m5, FakeMT5.TIMEFRAME_M15: m15}.get(timeframe, [])
                got = [r for r in src if r["time"] <= s.clock_b]
                return got[max(0, len(got) - count):]
        self.fake = Clock()
        self.t0 = t0

    def tearDown(self):
        self.LB._now_utc, self.LB.LEDGER_PATH = self.saved
        shutil.rmtree(self.home, ignore_errors=True)

    def _at(self, t):
        self.fake.clock_b = self.LB.utc_to_broker_epoch(t)
        self.LB._now_utc = lambda: t
        return t

    def test_an_alert_fires_once_on_the_closing_bar_and_is_logged(self):
        import live_alerts as LA
        import live_loop as LL
        now = self._at(self.t0 + dt.timedelta(minutes=30))
        r = LA.set_alert("EURUSD", 1.1030, None, "S4 BUY on T-CLOSE above 1.1030 M15", when="close", tf="M15",
                         m=self.fake, now=now, home=self.home)
        self.assertTrue(r["ok"], r)
        fired = []
        while now < self.t0 + dt.timedelta(hours=3):
            now = self._at(now + dt.timedelta(minutes=5))
            fired += LA.check(self.fake, now, LL.in_window, home=self.home)
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0]["type"], "live_level_alert")
        self.assertGreater(fired[0]["bar"]["c"], 1.1030)
        self.assertEqual(LA.active(self.home), [])
        types = [json.loads(x)["type"] for x in (self.home / "state" / "live_ledger.jsonl").read_text().splitlines()]
        self.assertEqual(types, ["alert_set", "level_alert"])

    def test_refusals_match_training(self):
        import live_alerts as LA
        now = self._at(self.t0 + dt.timedelta(minutes=30))
        self.assertFalse(LA.set_alert("EURUSD", 1.0, None, "already above it", m=self.fake, now=now, home=self.home)["ok"])
        self.assertFalse(LA.set_alert("NOTREAL", 2.0, None, "not a tradable symbol", m=self.fake, now=now,
                                      home=self.home)["ok"])
        self.assertFalse(LA.set_alert("EURUSD", 1.1009, None, "next to price: a poll", m=self.fake, now=now,
                                      home=self.home)["ok"])

    def test_an_unmet_alert_expires(self):
        import live_alerts as LA
        import live_loop as LL
        now = self._at(self.t0 + dt.timedelta(minutes=30))
        LA.set_alert("EURUSD", 1.2, None, "far away, short life", when="close", tf="M15", hours=0.5, m=self.fake,
                     now=now, home=self.home)
        for _ in range(12):
            now = self._at(now + dt.timedelta(minutes=5))
            self.assertEqual(LA.check(self.fake, now, LL.in_window, home=self.home), [])
        self.assertEqual(LA.active(self.home), [])


class OpportunitiesTest(unittest.TestCase):
    """tools/opportunities.py: every flagged mover that was not traded is walked forward by code."""

    def _m5(self, start, closes, spread=0.0002):
        t0 = datetime_ = dt.datetime.strptime(start, "%Y-%m-%d %H:%M")
        return [{"utc": (t0 + dt.timedelta(minutes=5 * i)).strftime("%Y-%m-%d %H:%M"), "o": c, "h": c + spread,
                 "l": c - spread, "c": c} for i, c in enumerate(closes)]

    def test_the_verdict_line_is_read_first(self):
        import opportunities as OPP
        at = dt.datetime(2025, 6, 4, 9, 0, tzinfo=dt.timezone.utc)
        posts = [_post("2025-06-04 09:05 UTC", "XAUUSD", "metals flush\nVERDICT XAUUSD DISMISS A1 A5 | chase, same "
                       "move as silver\nVERDICT XAGUSD WATCH S4 | retest", topic="scan", worker="scout")]
        v = OPP.verdict_of("XAUUSD", at, posts)
        self.assertEqual((v["verdict"], v["codes"]), ("DISMISS", ["A1", "A5"]))
        self.assertEqual(OPP.verdict_of("XAGUSD", at, posts)["verdict"], "WATCH")
        self.assertEqual(OPP.verdict_of("EURUSD", at, posts)["verdict"], "?")

    def test_the_yardstick_with_the_move_and_the_fade(self):
        import opportunities as OPP
        m15 = self._m5("2025-06-04 05:00", [1.0] * 14, spread=0.001)          # ATR(M15) = 0.002
        up = self._m5("2025-06-04 09:00", [1.0] + [1.0 + 0.0008 * i for i in range(1, 12)], spread=0.0)
        w = OPP.walk("2025-06-04 09:00", +1, up, m15)
        self.assertEqual((w["with_move"], w["fade"]), ("target", "stop"))   # ran 3 ATR on: 2R target, the fade stopped
        self.assertGreaterEqual(w["ran_on_atr"], 3.0)

    def test_a_traded_mover_is_not_a_rejection_and_rows_are_recorded_once(self):
        import opportunities as OPP
        home = _home()
        rows = [{"type": "setup_candidate", "utc": "2025-06-04 09:00 UTC", "candidates": [
                    {"symbol": "XAUUSD", "pct_move": 0.4}, {"symbol": "EURUSD", "pct_move": 0.4}]},
                {"type": "order_placed", "symbol": "EURUSD", "utc": "2025-06-04 09:30 UTC"}]
        m15 = self._m5("2025-06-04 05:00", [1.0] * 14, spread=0.001)
        bars = self._m5("2025-06-04 09:00", [1.0] * 30)
        fetch = lambda s, tf, n: bars if tf == "M5" else m15
        got = OPP.grade_day(rows, [], "d", lambda r: "d", fetch, "training")
        self.assertEqual([g["symbol"] for g in got], ["XAUUSD"])
        self.assertEqual(OPP.record(got, "training", home), 1)
        self.assertEqual(OPP.record(got, "training", home), 0)
        upto = dt.datetime(2025, 6, 4, 8, 0, tzinfo=dt.timezone.utc)
        self.assertEqual(OPP.load("training", home, upto), [])              # blind to the replay's own future
        shutil.rmtree(home, ignore_errors=True)


class MockLevelsTest(unittest.TestCase):
    """2026-10-18 (operator): an alert carries the planner's MOCK stop and target; the woken trader sets its own."""

    def test_the_cli_requires_mock_levels_on_opposite_sides_of_the_level(self):
        import alerts_core as AC
        args = ("XAUUSD", None, 3400.0, "S2 SELL on the retest of 3400", 3410.0, 2.0, "reject", "M15", 2, 8, [])
        err, _ = AC.check_new(*args, require_plan=True)
        self.assertIn("--sl P --tp P", err)
        err, _ = AC.check_new(*args, 3390.0, 3380.0, require_plan=True)     # both below the level: no side
        self.assertIn("opposite sides", err)
        err, spec = AC.check_new(*args, 3380.0, 3430.0, require_plan=True)
        self.assertIsNone(err)
        self.assertEqual((spec["plan_side"], spec["plan_sl"], spec["plan_tp"]), ("buy", 3380.0, 3430.0))

    def test_a_pass_is_graded_on_the_alerts_own_mock_levels(self):
        import pass_grade as PG
        p = {"id": 1, "symbol": "XAUUSD", "touched_utc": "2025-06-04 11:25 UTC", "dir": "below", "when": "reject",
             "bar": {"o": 3346, "h": 3347, "l": 3343, "c": 3346.0}, "why": "no levels written in the prose at all",
             "plan_side": "buy", "plan_sl": 3336.0, "plan_tp": 3360.0}
        bars = [{"utc": "2025-06-04 11:30", "o": 3349, "h": 3361, "l": 3348, "c": 3360}]
        self.assertIn("TARGET first", PG.grade(p, bars)["result"])


class ResumeTest(unittest.TestCase):
    """A pause the operator (or the trained-day watcher) set holds even after the usage window comes back."""

    def test_a_manual_pause_is_never_auto_resumed(self):
        import importlib
        import time as _time
        import resume_check as RC
        home = _home()
        (home / "state" / "MANUAL_PAUSE").write_text("paused for the operator\n")
        (home / "state" / "paused_until.json").write_text(json.dumps({"resume_at": _time.time() - 60,
                                                                      "reason": "usage limit"}))
        started = []
        saved = (RC.STATE, RC.subprocess.run)
        RC.STATE = home / "state"
        RC.subprocess.run = lambda *a, **k: started.append(a)
        try:
            with redirect_stdout(io.StringIO()):
                RC.main()
            self.assertEqual(started, [])                                    # the limit lifted, the pause holds
            (home / "state" / "MANUAL_PAUSE").unlink()
            with redirect_stdout(io.StringIO()):
                RC.main()
            self.assertEqual(len(started), 1)                                # no manual pause: it resumes
        finally:
            RC.STATE, RC.subprocess.run = saved
            shutil.rmtree(home, ignore_errors=True)
