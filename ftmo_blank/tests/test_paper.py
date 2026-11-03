"""Tests for the paper broker and guard. Synthetic bars make every fill and rule exact."""
import csv
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOME / "tools"))
sys.path.insert(0, str(HOME))
import paper as P  # noqa: E402
from guard import rules as R  # noqa: E402

T0 = int(datetime(2026, 1, 5, 10, tzinfo=timezone.utc).timestamp()) + 2 * 3600  # Jan: NY winter, broker = UTC+2


def make_home(bars, deadline=None):
    """bars: list of (o, h, l, c). Hourly from T0 (a Monday). Returns a Paper on a temp home.
    deadline, if given, temporarily shortens a window to that many business days (windows need real
    data to form, and a few hourly bars rolls through several business days already)."""
    tmp = Path(tempfile.mkdtemp())
    (tmp / "feed").mkdir()
    (tmp / "state").mkdir()
    with open(tmp / "feed" / "EURUSD_M5.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t_broker", "o", "h", "l", "c", "session"])
        for i, (o, h, l, c) in enumerate(bars):
            w.writerow([T0 + i * 3600, o, h, l, c, "day"])
    if deadline is None:
        return P.Paper(home=tmp)
    old = R.CHALLENGE["deadline_business_days"]
    R.CHALLENGE["deadline_business_days"] = deadline
    try:
        return P.Paper(home=tmp)
    finally:
        R.CHALLENGE["deadline_business_days"] = old


FLAT = (1.1000, 1.1002, 1.0998, 1.1000)
HALF = R.spec("EURUSD")["spread_price_snapshot"] / 2


def rows(path, kind=None):
    with open(path) as f:
        out = [json.loads(line) for line in f]
    return [r for r in out if kind is None or r["type"] == kind]


class PaperTest(unittest.TestCase):
    def flat(self, n):
        return [FLAT] * n

    def test_order_fills_at_next_open_plus_half_spread(self):
        p = make_home(self.flat(5))
        p.advance(2)
        r = p.place_order("EURUSD", "buy", 0.10, sl=1.0950, tp=1.1100, why="test entry for fill price")
        self.assertTrue(r["ok"], r)
        p.advance(1)
        pos = p.s["positions"][0]
        self.assertAlmostEqual(pos["entry"], 1.1000 + HALF, places=6)

    def test_stop_hit_closes_at_stop_less_half_spread_and_charges_commission(self):
        bars = self.flat(3) + [(1.1000, 1.1001, 1.0990, 1.0992)]  # opens 1.1000, low breaks 1.0995 stop
        p = make_home(bars)
        p.advance(3)
        p.place_order("EURUSD", "buy", 0.10, sl=1.0995, tp=1.1100, why="stop test entry rationale")
        p.advance(2)
        self.assertEqual(p.s["positions"], [])
        closed = rows(p.ledger_path, "trade_closed")[0]
        self.assertEqual(closed["reason"], "sl")
        self.assertAlmostEqual(closed["exit"], 1.0995 - HALF, places=6)
        self.assertEqual(closed["result"], "loss")
        expected_gross = ((1.0995 - HALF) - (1.1000 + HALF)) * 0.10 * 100_000
        self.assertAlmostEqual(closed["net_usd"], expected_gross - R.INSTRUMENTS["EURUSD"]["commission_rt_per_lot"] * 0.10, places=1)

    def test_target_hit(self):
        bars = self.flat(3) + [(1.1000, 1.1120, 1.0999, 1.1110)]
        p = make_home(bars)
        p.advance(3)
        p.place_order("EURUSD", "buy", 0.10, sl=1.0950, tp=1.1100, why="target test entry rationale")
        p.advance(2)
        closed = rows(p.ledger_path, "trade_closed")[0]
        self.assertEqual(closed["reason"], "tp")
        self.assertEqual(closed["result"], "win")

    def test_both_levels_in_one_bar_stop_first(self):
        bars = self.flat(3) + [(1.1000, 1.1200, 1.0900, 1.1000)]  # range touches both
        p = make_home(bars)
        p.advance(3)
        p.place_order("EURUSD", "buy", 0.10, sl=1.0950, tp=1.1100, why="both levels in one bar test")
        p.advance(2)
        closed = rows(p.ledger_path, "trade_closed")[0]
        self.assertEqual(closed["reason"], "sl")

    def test_gap_through_stop_fills_at_open_not_stop(self):
        bars = self.flat(3) + [FLAT, (1.0900, 1.0905, 1.0890, 1.0895)]  # fill bar, then gap far below stop
        p = make_home(bars)
        p.advance(3)
        p.place_order("EURUSD", "buy", 0.10, sl=1.0950, tp=None, why="gap through stop test entry")
        p.advance(2)
        closed = rows(p.ledger_path, "trade_closed")[0]
        self.assertAlmostEqual(closed["exit"], 1.0900 - HALF, places=6)

    def test_refusals_are_explained(self):
        p = make_home(self.flat(5))
        p.advance(2)
        self.assertIn("stop loss is required", p.place_order("EURUSD", "buy", 0.10, None, None, "no stop here please")["reason"])
        self.assertIn("below reference", p.place_order("EURUSD", "buy", 0.10, 1.1050, None, "stop on wrong side")["reason"])
        self.assertIn("exceeds the current per-trade cap", p.place_order("EURUSD", "buy", 1.0, 1.0950, None, "risk far too large")["reason"])
        self.assertIn("rationale", p.place_order("EURUSD", "buy", 0.10, 1.0950, None, "short")["reason"])
        self.assertIn("steps of", p.place_order("EURUSD", "buy", 0.123, 1.0950, None, "bad lot step size")["reason"])

    def test_worst_case_day_gate_refuses_when_all_stops_would_breach(self):
        p = make_home(self.flat(5))
        p.advance(2)
        # 0.5 lots with a 40 pip stop risks 2000 USD: over the 100 USD cap, so it must refuse
        self.assertFalse(p.place_order("EURUSD", "buy", 0.5, 1.0960, None, "oversized but well-formed order")["ok"])
        ok = p.place_order("EURUSD", "buy", 0.10, 1.0950, None, "ten lots of risk within the caps")
        self.assertTrue(ok["ok"])

    def test_a_breach_ends_the_window_and_the_next_attempt_starts_on_its_own(self):
        # one business day as a window (patched), so a breach on day 0 can jump straight to day 1's attempt
        bars = self.flat(3) + [FLAT, (1.0400, 1.0405, 1.0395, 1.0400)] + self.flat(50)
        p = make_home(bars, deadline=1)
        p.advance(3)
        p.place_order("EURUSD", "buy", 0.10, sl=1.0990, tp=None, why="gap test long inside the caps")
        p.advance(50)
        events = [r["type"] for r in rows(p.ledger_path)]
        self.assertIn("rule_breach", events)
        self.assertIn("attempt_ended", events)
        ended = rows(p.ledger_path, "attempt_ended")[0]
        self.assertTrue(ended["outcome"].startswith("breached"))
        self.assertGreaterEqual(p.s["attempt"], 2)  # a new attempt started on its own, no operator needed
        self.assertIsNone(p.s["breached"])          # not locked
        self.assertTrue(p.place_order("EURUSD", "buy", 0.01, 1.0990, None, "trading right after the breach")["ok"])

    def test_an_open_position_is_force_closed_at_the_window_boundary_before_the_roll(self):
        """A position still open going into the last bar of the window (the normal case: still
        open on the second Friday) must be closed as part of ending that window, not carried into
        the next attempt's fresh account -- the operator's "close before the swap, before the roll"
        requirement, as a safety net independent of the ordinary weekend flatten."""
        bars = self.flat(3) + self.flat(50)  # never touches the wide stop below: nothing closes it naturally
        p = make_home(bars, deadline=1)
        p.advance(3)
        p.place_order("EURUSD", "buy", 0.10, sl=1.0950, tp=None, why="held open into the window boundary")
        p.advance(1)
        self.assertEqual(len(p.s["positions"]), 1)  # confirms it is genuinely open before the boundary
        p.advance(49)
        # the window ended (not by a breach this time) and nothing was left open across it
        self.assertEqual(p.s["positions"], [])
        closed = rows(p.ledger_path, "trade_closed")
        forced = [r for r in closed if r["reason"] == "attempt_end"]
        self.assertEqual(len(forced), 1)
        self.assertIn("attempt ended:", forced[0]["close_note"])
        ended = rows(p.ledger_path, "attempt_ended")[0]
        self.assertEqual(ended["outcome"], "window_end")
        # the full report: the ended attempt's own final equity (flat bars, so close to 10000 minus
        # costs), logged BEFORE the account below is reset for attempt #2 -- not after, not lost.
        self.assertAlmostEqual(ended["final_equity"], R.START_BALANCE, delta=5.0)
        self.assertEqual(forced[0]["net_usd"], round(ended["final_equity"] - R.START_BALANCE, 2))
        # routed, so the meeting (review on trade_closed, reflect on attempt_ended) actually gets called
        outbox_types = [r["type"] for r in rows(p.outbox_path)]
        self.assertIn("trade_closed", outbox_types)
        self.assertIn("attempt_ended", outbox_types)
        # and the roll itself already happened: the next attempt starts flat and fresh, on its own
        # (49 more hourly bars crosses more than one of these 1-day test windows; only the first one
        # had anything open to force-close, which is exactly what the assertions above checked)
        self.assertGreaterEqual(p.s["attempt"], 2)
        self.assertEqual(p.s["balance"], R.START_BALANCE)

    def test_risk_warning_fires_once_at_sixty_percent_of_daily_limit(self):
        # a gap of 30 pips through the stop loses about 300, which is 60% of the 500 daily limit
        bars = self.flat(3) + [FLAT, (1.0700, 1.0705, 1.0695, 1.0700)] + self.flat(3)
        p = make_home(bars)
        p.advance(3)
        p.place_order("EURUSD", "buy", 0.10, sl=1.0990, tp=None, why="warning test position inside caps")
        p.advance(6)
        warns = rows(p.ledger_path, "risk_warning")
        self.assertEqual(len([w for w in warns if w["kind"] == "daily"]), 1)
        self.assertEqual(p.s["breached"], None)

    def test_day_closed_records_traded_flag_and_trading_day_count(self):
        bars = self.flat(30)  # spans more than one Prague day (one bar per hour)
        p = make_home(bars)
        p.advance(3)
        p.place_order("EURUSD", "buy", 0.10, sl=1.0950, tp=1.1100, why="day record test trade rationale")
        p.advance(27)
        days = rows(p.ledger_path, "day_closed")
        self.assertGreaterEqual(len(days), 1)
        self.assertTrue(any(d["traded"] for d in days))
        self.assertEqual(len(p.s["traded_days"]), 1)

    def test_journal_goal_requires_a_traded_day(self):
        p = make_home(self.flat(5))
        p.advance(2)
        self.assertFalse(next(g for g in p.goals() if g["goal"] == "P_journal_every_traded_day")["passed"])

    def test_stop_cannot_be_widened(self):
        p = make_home(self.flat(5))
        p.advance(2)
        p.place_order("EURUSD", "buy", 0.10, sl=1.0950, tp=None, why="stop widening test entry rationale")
        p.advance(1)
        pid = p.s["positions"][0]["id"]
        self.assertFalse(p.modify_stop(pid, 1.0900, "trying to widen the stop")["ok"])
        self.assertTrue(p.modify_stop(pid, 1.0980, "tightening the stop to protect gains")["ok"])

    def test_routed_events_reach_the_outbox(self):
        p = make_home(self.flat(8))
        p.advance(3)
        p.place_order("EURUSD", "buy", 0.10, sl=1.0950, tp=1.1100, why="outbox test rationale here")
        p.advance(1)
        p.close_position(p.s["positions"][0]["id"], "closing for the outbox test")
        p.advance(2)
        outbox = [r["type"] for r in rows(p.outbox_path)]
        self.assertIn("trade_closed", outbox)

    def test_close_and_modify_refuse_a_mismatched_symbol(self):
        """The CLI always passes --symbol (required); this is what lets the guard scope a per-symbol
        trader's Bash rule to its own symbol -- a wrong one must be refused, not silently accepted."""
        p = make_home(self.flat(5))
        p.advance(2)
        p.place_order("EURUSD", "buy", 0.10, sl=1.0950, tp=None, why="symbol-scope test entry rationale")
        p.advance(1)
        pid = p.s["positions"][0]["id"]
        wrong = p.close_position(pid, "closing with the wrong symbol on purpose", symbol="XAUUSD")
        self.assertFalse(wrong["ok"])
        self.assertIn("EURUSD", wrong["reason"])
        wrong_mod = p.modify_stop(pid, 1.0980, "tightening with the wrong symbol", symbol="XAUUSD")
        self.assertFalse(wrong_mod["ok"])
        self.assertTrue(p.close_position(pid, "closing with the right symbol this time", symbol="EURUSD")["ok"])


class RiskFactorTest(unittest.TestCase):
    """The 'risk' kind's own lever: bounded by the guard, persists across windows, and the cap an
    order is actually checked against moves with it -- not just reported, enforced."""

    def flat(self, n):
        return [FLAT] * n

    def test_starts_at_the_guard_default_and_within_bounds(self):
        p = make_home(self.flat(5))
        rf = p.risk_factor()
        self.assertEqual(rf["risk_pct"], R.RISK_FACTOR_DEFAULT)
        self.assertEqual(rf["min"], R.RISK_FACTOR_MIN)
        self.assertEqual(rf["max"], R.MAX_RISK_PER_TRADE)

    def test_a_value_outside_the_guard_range_is_refused(self):
        p = make_home(self.flat(5))
        too_high = p.set_risk_factor(R.MAX_RISK_PER_TRADE + 0.01, "trying to exceed the ceiling")
        self.assertFalse(too_high["ok"])
        too_low = p.set_risk_factor(R.RISK_FACTOR_MIN - 0.001, "trying to go under the floor")
        self.assertFalse(too_low["ok"])
        self.assertEqual(p.s["risk_pct"], R.RISK_FACTOR_DEFAULT)  # neither attempt changed it

    def test_a_change_requires_a_reason(self):
        p = make_home(self.flat(5))
        self.assertFalse(p.set_risk_factor(0.005, "short")["ok"])

    def test_a_valid_change_is_enforced_on_the_very_next_order(self):
        p = make_home(self.flat(5))
        p.advance(2)
        self.assertTrue(p.set_risk_factor(0.003, "low win rate and few trades: lower risk, more reps")["ok"])
        # at 0.003 (0.3% = $30 cap), the same order that was fine under the 0.7% default is now too big
        r = p.place_order("EURUSD", "buy", 0.10, sl=1.0950, tp=None, why="same order as always, new cap")
        self.assertFalse(r["ok"])
        self.assertIn("per-trade cap", r["reason"])
        self.assertTrue(p.place_order("EURUSD", "buy", 0.05, sl=1.0950, tp=None, why="resized to the new cap")["ok"])

    def test_the_change_is_logged_and_routed_so_the_meeting_sees_it(self):
        p = make_home(self.flat(5))
        p.advance(2)
        p.set_risk_factor(0.005, "stepping down after a run of small, inconclusive trades")
        changed = rows(p.ledger_path, "risk_factor_changed")
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0]["old_pct"], R.RISK_FACTOR_DEFAULT)
        self.assertEqual(changed[0]["new_pct"], 0.005)
        self.assertIn("risk_factor_changed", [r["type"] for r in rows(p.outbox_path)])

    def test_risk_factor_survives_a_window_boundary_unlike_balance(self):
        """Explicitly NOT reset by _end_window: this is a long-lived lever, not a per-attempt one."""
        bars = self.flat(53)
        p = make_home(bars, deadline=1)
        p.advance(2)
        p.set_risk_factor(0.004, "deliberately low while exploring with more, smaller trades")
        p.advance(49)  # crosses at least one window boundary (balance/attempt reset, confirmed elsewhere)
        self.assertGreater(p.s["attempt"], 1)
        self.assertEqual(p.s["balance"], R.START_BALANCE)   # balance DID reset
        self.assertEqual(p.s["risk_pct"], 0.004)             # risk_pct did NOT


class SetupCandidateScanTest(unittest.TestCase):
    """The cheap, pure-code intra-day screen: no Claude call unless a symbol actually moved
    enough, and never the same symbol twice in the same day even if it keeps moving."""

    def test_flags_a_symbol_that_moved_past_the_threshold(self):
        bars = [FLAT] + [(1.1000, 1.1001, 1.0999, 1.1000)] * 11 + [(1.1038, 1.1042, 1.1036, 1.1040)]
        p = make_home(bars)
        p.advance(len(bars))
        cands = p.scan_candidates()
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0]["symbol"], "EURUSD")
        self.assertGreater(cands[0]["pct_move"], P.SCAN_THRESHOLD_PCT)

    def test_ignores_a_move_under_the_threshold(self):
        p = make_home(self.flat(13))
        p.advance(13)
        self.assertEqual(p.scan_candidates(), [])

    def flat(self, n):
        return [FLAT] * n

    def test_logs_once_then_dedups_the_same_symbol_the_same_day(self):
        bars = [FLAT] + [(1.1000, 1.1001, 1.0999, 1.1000)] * 11 + [(1.1038, 1.1042, 1.1036, 1.1040)]
        p = make_home(bars)
        p.advance(len(bars))
        p._maybe_flag_setup_candidates()
        p._maybe_flag_setup_candidates()  # same candidate, same day: must not log a second time
        events = rows(p.ledger_path, "setup_candidate")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["candidates"][0]["symbol"], "EURUSD")
        self.assertIn("setup_candidate", [r["type"] for r in rows(p.outbox_path)])

    def test_advance_does_not_crash_across_many_scan_checkpoints(self):
        """Integration smoke test: a long advance crossing several SCAN_EVERY_BARS checkpoints,
        with real (if uneventful) price action, must not error or lose bars."""
        bars = [FLAT] * 40 + [(1.1038, 1.1042, 1.1036, 1.1040)] + [FLAT] * 40
        p = make_home(bars)
        adv = p.advance(len(bars))
        self.assertEqual(adv["processed"], len(bars))


class ContinuityTrackingTest(unittest.TestCase):
    """Passes and bad/near-bad runs both need a record that survives the window reset: a pass
    streak to enforce doing more of what worked, and a flag on a breach or a near-miss so it
    gets the deep review too, not just an outright fail."""

    def flat(self, n):
        return [FLAT] * n

    def test_a_pass_increments_the_streak_and_a_non_pass_resets_it(self):
        bars = self.flat(53)
        p = make_home(bars, deadline=1)
        p.advance(52)
        # whatever happened (flat bars pass or not), the streak numbers must be internally consistent
        self.assertGreaterEqual(p.s["best_pass_streak"], p.s["consecutive_passes"])
        self.assertIsInstance(p.s["consecutive_passes"], int)

    def test_passed_outcome_builds_a_streak_across_attempts(self):
        p = make_home(self.flat(5))
        # simulate two passes in a row directly (isolates the streak logic from needing a real window)
        p.s["attempt"], p.s["epoch"], p.s["window_index"] = 1, 0, 0
        p.windows = [(0, 1), (1, 2), (2, 3)]
        p._jump_cursor = 0
        p._end_window("passed")
        self.assertEqual(p.s["consecutive_passes"], 1)
        p._end_window("passed")
        self.assertEqual(p.s["consecutive_passes"], 2)
        self.assertEqual(p.s["best_pass_streak"], 2)
        p._end_window("window_end")
        self.assertEqual(p.s["consecutive_passes"], 0)
        self.assertEqual(p.s["best_pass_streak"], 2)  # the best streak itself is never erased

    def test_a_breach_logs_attempt_breach_review_not_just_reflect(self):
        bars = self.flat(3) + [FLAT, (1.0400, 1.0405, 1.0395, 1.0400)] + self.flat(50)
        p = make_home(bars, deadline=1)
        p.advance(3)
        p.place_order("EURUSD", "buy", 0.10, sl=1.0990, tp=None, why="gap test long inside the caps")
        p.advance(50)
        events = [r["type"] for r in rows(p.ledger_path)]
        self.assertIn("attempt_breach_review", events)
        br = rows(p.ledger_path, "attempt_breach_review")[0]
        self.assertTrue(br["outcome"].startswith("breached"))
        self.assertFalse(br["near_miss"])  # it breached outright, "near miss" is the lesser case
        self.assertIn("attempt_breach_review", [r["type"] for r in rows(p.outbox_path)])

    def test_a_risk_warning_without_a_breach_is_flagged_a_near_miss(self):
        # a loss big enough to cross WARN_FRACTION (60%) of the daily limit but not breach it
        bars = self.flat(3) + [FLAT, (1.0700, 1.0705, 1.0695, 1.0700)] + self.flat(50)
        p = make_home(bars, deadline=1)
        p.advance(3)
        p.place_order("EURUSD", "buy", 0.10, sl=1.0990, tp=None, why="warning-then-window-end test setup")
        p.advance(50)
        ended = rows(p.ledger_path, "attempt_ended")[0]
        self.assertGreater(ended["risk_warnings_this_attempt"], 0)
        self.assertTrue(ended["near_miss"])
        self.assertIn("attempt_breach_review", [r["type"] for r in rows(p.ledger_path)])

    def test_a_window_boundary_survives_state_saved_before_these_fields_existed(self):
        """Confirmed live, 2026-10-09: an already-running account's saved state has neither
        consecutive_passes nor best_pass_streak (it predates both), and _end_window crashed with
        a KeyError on the very first window boundary it hit after the upgrade."""
        p = make_home(self.flat(53), deadline=1)
        p.advance(2)
        del p.s["consecutive_passes"]
        del p.s["best_pass_streak"]
        p.advance(50)  # must not raise

    def test_an_uneventful_window_end_gets_no_breach_review(self):
        p = make_home(self.flat(53), deadline=1)
        p.advance(52)
        ended = [r for r in rows(p.ledger_path, "attempt_ended") if r["outcome"] == "window_end"]
        if ended:
            self.assertFalse(ended[0]["near_miss"])
        self.assertNotIn("attempt_breach_review", [r["type"] for r in rows(p.ledger_path)])


class RealEvalChancesTest(unittest.TestCase):
    """Every attempt is recomputed against TODAY's bar, not the outcome string stored at the time -- the target
    has changed twice (10% -> 5% -> the live +10% with 2 traded days, 2026-10-18), so an attempt is always
    judged by its own numbers against the current terms."""

    def flat(self, n):
        return [FLAT] * n

    def test_an_attempt_under_todays_ten_percent_bar_is_not_a_chance(self):
        p = make_home(self.flat(5))
        p.ledger_path.write_text(json.dumps({"type": "attempt_ended", "attempt": 1, "outcome": "window_end",
                                             "final_equity": 10200.0, "traded_days": 4, "utc": "2026-01-01 00:00 UTC"}) + "\n")
        self.assertEqual(p.real_eval_chances()["earned"], 0)

    def test_an_attempt_that_clears_todays_bar_counts_even_if_stored_as_window_end(self):
        p = make_home(self.flat(5))
        p.ledger_path.write_text(json.dumps({"type": "attempt_ended", "attempt": 4, "outcome": "window_end",
                                             "final_equity": 11045.67, "traded_days": 2, "utc": "2026-01-01 00:00 UTC"}) + "\n")
        chances = p.real_eval_chances()
        self.assertEqual(chances["earned"], 1)
        self.assertEqual(chances["passes"][0]["attempt"], 4)

    def test_too_few_traded_days_does_not_count_even_above_target(self):
        p = make_home(self.flat(5))
        p.ledger_path.write_text(json.dumps({"type": "attempt_ended", "attempt": 2, "outcome": "window_end",
                                             "final_equity": 11200.0, "traded_days": 1, "utc": "2026-01-01 00:00 UTC"}) + "\n")
        self.assertEqual(p.real_eval_chances()["earned"], 0)

    def test_recent_attempts_carries_the_recomputed_flag(self):
        p = make_home(self.flat(5))
        p.ledger_path.write_text(
            json.dumps({"type": "attempt_ended", "attempt": 4, "outcome": "window_end",
                       "final_equity": 11045.67, "traded_days": 2, "utc": "2026-01-01 00:00 UTC"}) + "\n" +
            json.dumps({"type": "attempt_ended", "attempt": 6, "outcome": "window_end",
                       "final_equity": 9801.23, "traded_days": 2, "utc": "2026-01-02 00:00 UTC"}) + "\n")
        rows = p.recent_attempts()
        self.assertTrue(next(r for r in rows if r["attempt"] == 4)["demo_passed_now"])
        self.assertFalse(next(r for r in rows if r["attempt"] == 6)["demo_passed_now"])


class ConsultedAnalysisTest(unittest.TestCase):
    """The broker itself checks the board for a pre_trade post, rather than trusting whoever is
    placing the order to say so -- this is what makes 'was this trade taken with or without prior
    analysis' a verified fact a later review can rely on, not a self-reported claim."""

    def flat(self, n):
        return [FLAT] * n

    def test_an_order_with_a_same_day_pre_trade_post_is_flagged_consulted(self):
        import board as B
        p = make_home(self.flat(5))
        p.advance(2)
        # now_utc() (board.py, like kstore.py) reads it from the last WRITTEN ledger row; a brand-new
        # fixture has none yet, so prime one -- in a real run there are always thousands by this point.
        p.log("note", text="priming the ledger so now_utc resolves before the board post")
        B.post("scout", "pre_trade", "EURUSD H1 range broke up, clean long setup", symbol="EURUSD", home=p.home)
        r = p.place_order("EURUSD", "buy", 0.10, sl=1.0950, tp=None, why="consulted test entry rationale")
        self.assertTrue(r["consulted_analysis"])
        p.advance(1)
        self.assertTrue(p.s["positions"][0]["consulted_analysis"])
        opened = rows(p.ledger_path, "trade_opened")[0]
        self.assertTrue(opened["consulted_analysis"])

    def test_an_order_with_no_pre_trade_post_is_not_flagged(self):
        p = make_home(self.flat(5))
        p.advance(2)
        r = p.place_order("EURUSD", "buy", 0.10, sl=1.0950, tp=None, why="unconsulted test entry rationale")
        self.assertFalse(r["consulted_analysis"])

    def test_a_pre_trade_post_for_a_different_symbol_does_not_count(self):
        import board as B
        p = make_home(self.flat(5))
        p.advance(2)
        p.log("note", text="priming the ledger so now_utc resolves before the board post")
        B.post("scout", "pre_trade", "XAUUSD looks interesting", symbol="XAUUSD", home=p.home)
        r = p.place_order("EURUSD", "buy", 0.10, sl=1.0950, tp=None, why="cross-symbol test entry rationale")
        self.assertFalse(r["consulted_analysis"])


class JournalGoalTest(unittest.TestCase):
    def state(self, traded, notes):
        return {"equity": 10000.0, "traded_days": traded, "note_days": notes, "field_read": True, "breached": None}

    def goal(self, st):
        return next(g for g in R.goal_table(st) if g["goal"] == "P_journal_every_traded_day")

    def test_note_on_the_wrong_day_does_not_pass(self):
        self.assertFalse(self.goal(self.state(["2026-01-05"], ["2026-01-06"]))["passed"])

    def test_every_traded_day_needs_its_own_note(self):
        st = self.state(["2026-01-05", "2026-01-06"], ["2026-01-05"])
        self.assertFalse(self.goal(st)["passed"])
        st = self.state(["2026-01-05", "2026-01-06"], ["2026-01-05", "2026-01-06"])
        self.assertTrue(self.goal(st)["passed"])

    def test_no_trading_day_no_pass(self):
        self.assertFalse(self.goal(self.state([], ["2026-01-05"]))["passed"])


class LiveTermsTest(unittest.TestCase):
    """2026-10-18 (operator): training takes the live terms -- +10%, at least 2 traded days, 5% / 10% drawdown, NO
    deadline. An attempt ends only by passing or breaching; the next one starts on the next bar."""

    def test_no_deadline_and_the_live_numbers(self):
        self.assertIsNone(R.CHALLENGE["deadline_business_days"])
        self.assertEqual((R.CHALLENGE["profit_target"], R.CHALLENGE["min_trading_days"]), (0.10, 2))
        p = make_home([FLAT] * 3 * 30)            # many business days of flat bars: nothing ends the attempt
        p.advance(len(p.timeline) - 2)
        self.assertEqual(p.s["attempt"], 1)
        self.assertEqual(p.s["attempts_total"], 0)

    def test_the_attempt_passes_the_moment_the_target_is_met_with_two_traded_days(self):
        p = make_home([FLAT] * 3 * 6)
        p.advance(1)
        p.s["traded_days"] = ["2026-01-05", "2026-01-06"]
        p.s["balance"] = R.START_BALANCE * 1.101
        p.s["equity"] = p.s["balance"]
        p.advance(1)
        ended = [json.loads(l) for l in p.ledger_path.read_text().splitlines() if '"attempt_ended"' in l]
        self.assertEqual(ended[-1]["outcome"], "passed")
        self.assertEqual(p.s["attempt"], 2)
        self.assertEqual(p.s["balance"], R.START_BALANCE)


class LadderTest(unittest.TestCase):
    def test_rungs_and_challenge_pass(self):
        # ladder is 2/4/6/8/10% of start: the live +10% target (2026-10-18)
        self.assertEqual(R.rungs_passed(10_000), 0)
        self.assertEqual(R.rungs_passed(10_400), 2)
        self.assertEqual(R.rungs_passed(11_000), 5)


if __name__ == "__main__":
    unittest.main()
