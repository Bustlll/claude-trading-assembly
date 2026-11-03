import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import routing  # noqa: E402

SNAP = {"bar_index": 100, "bars_total": 2880, "utc": "2026-01-15 10:00 UTC", "epoch": 1, "window_index": 1,
        "windows_per_epoch": 12, "window_day": 9, "window_business_days": 10, "attempt": 1,
        "pass_rate": 0.0, "attempts_total": 1}
GOALS = [{"goal": "P_field_read", "passed": False, "evidence": "field not read"},
         {"goal": "L0_equity_plus_2pct", "passed": True, "evidence": "equity 10300.80 vs 10200.00"}]


class RoutingTest(unittest.TestCase):
    def test_reassess_builds_trade_prompt(self):
        kind, prompt = routing.route("turn_reassess", {"snapshot": SNAP, "goals": GOALS})
        self.assertEqual(kind, "trade")
        self.assertIn("GOAL: equity", prompt)       # 2026-11-01: one line, not the rung ladder
        self.assertNotIn("last_close", prompt)       # nor the 34 last closes

    def test_reassess_carries_its_own_context_and_the_castigation(self):
        # 2026-11-01 (operator: "it needs to be aware of its own context"; 25% of a usage window without an order)
        own = {"self_text": "YOUR OWN CONTEXT (code-measured): 40 calls", "castigated": False, "castigation_text": "CASTIGATED. X"}
        _, prompt = routing.route("turn_reassess", {"snapshot": SNAP, "goals": GOALS, **own})
        self.assertTrue(prompt.startswith("YOUR OWN CONTEXT"))
        self.assertNotIn("CASTIGATED. X", prompt)
        _, prompt = routing.route("turn_reassess", {"snapshot": SNAP, "goals": GOALS, **own, "castigated": True})
        self.assertTrue(prompt.startswith("CASTIGATED. X"))
        self.assertIn("consult.py training chart|timing", prompt)   # 2026-10-19: chart/timing are asked inside the turn
        self.assertIn("next_demands: scout", prompt)

    def test_a_castigated_desk_wakes_no_scout(self):
        cands = [{"symbol": "XAUUSD", "pct_move": 0.5, "bars": 12}]
        self.assertEqual(routing.route("setup_candidate", {"candidates": cands})[0], "scout")
        self.assertIsNone(routing.route("setup_candidate", {"candidates": cands, "castigated": True}))
        kind, prompt = routing.route("level_alert", {"symbol": "XAUUSD", "level": 3206.4, "dir": "below",
                                                     "castigated": True, "castigation_text": "CASTIGATED. X"})
        self.assertEqual(kind, "trade_xauusd")
        self.assertTrue(prompt.startswith("CASTIGATED. X"))

    # ---- the decision clock (2026-10-18) ----
    def test_reassess_names_movers_alerts_and_the_window(self):
        _, prompt = routing.route("turn_reassess", {"snapshot": SNAP, "goals": GOALS, "window": "WIN",
                                                    "movers": [{"symbol": "XAUUSD", "pct_move": 0.41, "bars": 12}]})
        self.assertIn("XAUUSD +0.41%", prompt)
        self.assertIn("TURN REASSESS at 2026-01-15 10:00 UTC", prompt)
        self.assertIn("alert --symbol S --above P|--below P", prompt)

    def test_reassess_events_are_trimmed_to_fit_the_prompt_cap(self):
        many = [{"type": "trade_closed", "why_open": "x" * 230, "n": i} for i in range(60)]
        _, prompt = routing.route("turn_reassess", {"snapshot": SNAP, "goals": GOALS, "recent_events": many})
        self.assertIn("older events not shown", prompt)
        self.assertIn('"n":59', prompt)           # the newest survive
        from guard import policy as PO           # the cap itself, not a copy of its number (40k since 2026-10-18)
        self.assertLess(len(prompt), PO.LIMITS["max_prompt_chars"])

    def test_a_traded_day_still_gets_its_review_but_a_skip_note_alone_does_not(self):
        # 2026-10-18 (training works like live): a day with journal notes is reviewed, as live_day_closed is;
        # a day with no trade, no note and nothing graded still costs no call
        self.assertIsNone(routing.route("day_closed", {"traded": False, "journal_notes_today": 0}))
        self.assertEqual(routing.route("day_closed", {"traded": False, "journal_notes_today": 1})[0], "review")
        kind, _ = routing.route("day_closed", {"traded": True, "journal_notes_today": 1})
        self.assertEqual(kind, "review")

    def test_scout_is_not_forced_to_hand_off(self):
        _, prompt = routing.route("setup_candidate", {"candidates": [
            {"symbol": "XAUUSD", "pct_move": 0.5, "bars": 12}], "traded_days_total": 0, "window_day": 7})
        self.assertNotIn("HAND-OFF REQUIRED", prompt)
        self.assertIn("as it happens", prompt)
        self.assertNotIn("PACING: traded days", prompt)   # 2026-10-18: no deadline, so never a pacing push

    def test_pacing_is_never_a_reason_to_trade(self):
        # ids 26-29: four Asia market entries justified by pacing, each about -1R; since 2026-10-18 there is no
        # deadline at all (live terms), so no prompt pushes a trade for a traded-days count
        _, prompt = routing.route("setup_candidate", {"candidates": [
            {"symbol": "XAUUSD", "pct_move": 0.5, "bars": 12}], "traded_days_total": 0, "window_day": 9})
        self.assertNotIn("PACING: traded days", prompt)  # (the vocabulary lists code A3 PACING in every prompt)
        self.assertNotIn("PRE-ORDER CHECKLIST", prompt)   # 2026-11-01: a scout places no order; the trader carries it
        _, prompt = routing.route("turn_reassess", {"snapshot": SNAP, "goals": GOALS})
        self.assertIn("Pacing is never the reason for an order", prompt)
        self.assertIn("PRE-ORDER CHECKLIST", prompt)
        _, prompt = routing.route("level_alert", {"symbol": "XAUUSD", "level": 3206.4, "dir": "below"})
        self.assertIn("PRE-ORDER CHECKLIST", prompt)

    def test_level_alert_wakes_the_symbols_own_trader_or_the_generalist(self):
        base = {"id": 3, "dir": "below", "level": 3360.0, "why": "buy the retest if M5 closes back above 3362",
                "by": "trade", "set_utc": "2025-05-06 07:00 UTC", "ref": 3377.7, "utc": "2025-05-06 10:15 UTC",
                "bar": {"o": 3362, "h": 3363, "l": 3359.5, "c": 3361}}
        kind, prompt = routing.route("level_alert", {**base, "symbol": "XAUUSD"})
        self.assertEqual(kind, "trade_xauusd")
        self.assertIn("buy the retest if M5 closes back above 3362", prompt)
        kind, _ = routing.route("level_alert", {**base, "symbol": "GBPJPY"})
        self.assertEqual(kind, "trade")

    def test_reassess_points_to_the_board_and_the_specialists(self):
        _, prompt = routing.route("turn_reassess", {"snapshot": SNAP, "goals": GOALS})
        self.assertIn(f"{routing.BOARD} post --from trade", prompt)   # 2026-11-01: today's board is attached, not read
        self.assertIn("scout", prompt)
        self.assertIn("chart", prompt)
        self.assertIn("timing", prompt)

    def test_idle_day_does_not_wake_model(self):
        self.assertIsNone(routing.route("day_closed", {"traded": False, "journal_notes_today": 0}))

    def test_setup_candidate_wakes_scout_cheaply_naming_the_movers(self):
        kind, prompt = routing.route("setup_candidate", {"candidates": [
            {"symbol": "EURUSD", "pct_move": 0.42, "bars": 12}]})
        self.assertEqual(kind, "scout")
        self.assertIn("EURUSD (+0.42% over 12 bars)", prompt)

    def test_an_empty_candidate_list_does_not_wake_the_model(self):
        self.assertIsNone(routing.route("setup_candidate", {"candidates": []}))

    def test_attempt_passed_review_names_the_streak(self):
        kind, prompt = routing.route("attempt_passed_review", {"attempt": 9, "final_equity": 10545.67,
                                                                "traded_days": 4, "consecutive_passes": 3,
                                                                "best_pass_streak": 3})
        self.assertEqual(kind, "review")
        self.assertIn("pass #3 IN A ROW", prompt)
        self.assertIn("must NOT change", prompt)

    def test_a_breach_gets_the_deep_review_not_the_light_reflect(self):
        self.assertIsNone(routing.route("attempt_ended", {"outcome": "breached:daily_loss"}))
        kind, prompt = routing.route("attempt_breach_review", {"attempt": 9, "outcome": "breached:daily_loss",
                                                                "final_equity": 9400.0, "traded_days": 2,
                                                                "risk_warnings_this_attempt": 1})
        self.assertEqual(kind, "review")
        self.assertIn("a BREACH", prompt)
        self.assertIn("not a verdict on anyone", prompt)
        self.assertIn("goal stays +10%", prompt)

    def test_a_near_miss_gets_the_deep_review_too_labelled_as_such(self):
        self.assertIsNone(routing.route("attempt_ended", {"outcome": "window_end", "near_miss": True}))
        kind, prompt = routing.route("attempt_breach_review", {"attempt": 9, "outcome": "window_end",
                                                                "final_equity": 9600.0, "traded_days": 3,
                                                                "risk_warnings_this_attempt": 2})
        self.assertEqual(kind, "review")
        self.assertIn("NEAR MISS", prompt)

    def test_an_uneventful_window_end_still_gets_the_light_reflect(self):
        kind, _ = routing.route("attempt_ended", {"outcome": "window_end", "near_miss": False})
        self.assertEqual(kind, "reflect")

    def test_trade_closed_review_is_told_to_post_a_lesson_to_the_board(self):
        kind, prompt = routing.route("trade_closed", {"result": "loss", "reason": "sl", "r_multiple": -1.0,
                                                       "symbol": "EURUSD", "consulted_analysis": True})
        self.assertEqual(kind, "review")
        self.assertIn(f"{routing.BOARD} post --from review --topic lesson --symbol EURUSD", prompt)

    def test_trade_closed_review_must_check_the_board_before_judging(self):
        """A specialist's pre-trade work must be looked up, not assumed -- for a trade taken
        WITH consulted analysis as much as one taken without it."""
        kind, prompt = routing.route("trade_closed", {"result": "win", "reason": "tp", "r_multiple": 1.5,
                                                       "symbol": "XAUUSD", "consulted_analysis": True})
        self.assertEqual(kind, "review")
        self.assertIn(f"{routing.BOARD} read --topic pre_trade --symbol XAUUSD", prompt)
        self.assertIn("consulted_analysis=True", prompt)
        self.assertIn("quote what it said", prompt)

    def test_trade_closed_review_names_the_absence_when_nothing_was_consulted(self):
        kind, prompt = routing.route("trade_closed", {"result": "loss", "reason": "sl", "r_multiple": -1.0,
                                                       "symbol": "USOIL.cash", "consulted_analysis": False})
        self.assertIn("consulted_analysis=False", prompt)
        self.assertIn("no specialist analysis existed before this", prompt)

    def test_a_mechanical_window_end_close_is_judged_only_on_entry_not_exit(self):
        kind, prompt = routing.route("trade_closed", {"result": "win", "reason": "attempt_end",
                                                       "r_multiple": 0.3, "symbol": "EURUSD"})
        self.assertEqual(kind, "review")
        self.assertIn("MECHANICAL", prompt)
        self.assertIn("Do not judge the exit", prompt)
        self.assertNotIn("FIRST, before judging anything else", prompt)  # the full board-check path, skipped

    def test_pass_rate_milestone_points_at_the_risk_kind_not_a_self_set(self):
        _, prompt = routing.route("pass_rate_milestone", {"pass_rate": 0.5, "milestone": 0.5,
                                                           "attempts_total": 10, "attempts_passed": 5})
        self.assertIn('"risk" kind', prompt)
        self.assertIn("Do not set it yourself", prompt)

    def test_epoch_review_points_at_the_risk_kind(self):
        _, prompt = routing.route("epoch_review", {"epoch": 0, "attempts_total": 20, "attempts_passed": 2,
                                                    "pass_rate": 0.1})
        self.assertIn('"risk" kind', prompt)



class DreamsInTrainingTest(unittest.TestCase):
    def test_every_training_prompt_knows_dreams_are_hypotheses_it_owns(self):
        import importlib
        R = importlib.import_module("routing")
        self.assertIn("DREAMS:", R.CONTEXT)
        self.assertIn("HYPOTHESES", R.CONTEXT)
        self.assertIn("never by faith", R.CONTEXT)
        for verb in ("note ID", "revise ID", "retire ID"):
            self.assertIn(verb, R.CONTEXT)

if __name__ == "__main__":
    unittest.main()
