"""Tests for tools/live_routing.py: every event the live loop can emit lands on a LIVE kind (or on
nothing, by design), every prompt fits the live engine's prompt cap with a realistic snapshot, and
no live prompt ever tells a worker to use the replay's tools."""
import sys
import unittest
from pathlib import Path

HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOME / "tools"))
sys.path.insert(0, str(HOME))
import live_routing as LR  # noqa: E402
from guard import policy as P  # noqa: E402
from guard import rules as R  # noqa: E402

SNAP = {"utc": "2026-10-07 07:02 UTC", "prague_day": "2026-10-07", "weekday": "Wednesday", "mode": "LIVE",
        "armed": True, "market_open": True, "newest_tick_age_s": 3, "login": 1000000001, "currency": "EUR",
        "balance": 9606.52, "equity": 9620.1, "initial_balance": 9606.52, "day_start_equity": 9606.52,
        "day_pnl": 13.58, "daily_loss_used": -13.58, "daily_loss_limit": 480.33, "total_loss_used": -13.58,
        "total_loss_limit": 960.65, "risk_pct": 0.003, "per_trade_cap": 28.82,
        "own_positions": [{"ticket": 1000 + i, "symbol": "EURUSD", "side": "buy", "lots": 0.05, "entry": 1.1,
                           "sl": 1.095, "tp": 1.11, "floating": 3.2, "opened_utc": "2026-10-07 06:00"} for i in range(5)],
        "own_open_risk": 120.0, "own_day_pnl": 13.58, "own_halt": None, "entry_block": None,
        "other_runners_positions": {"XAUUSD sell": 2, "GBPUSD buy": 1},
        "own_traded_days": ["2026-10-0%d" % d for d in range(1, 8)], "own_trades_today": 2,
        "own_closed_trades": {"closed": 40, "wins": 18, "losses": 22, "net": -12.3, "mean_r_net": -0.02},
        "last_close": {s: 12345.678 for s in R.INSTRUMENTS}}
CLOSED = {"ticket": 11, "symbol": "EURUSD", "side": "buy", "lots": 0.1, "entry": 1.1, "exit": 1.095,
          "initial_sl": 1.095, "tp": 1.11, "reason": "sl", "net": -50.7, "currency": "EUR", "risk_ccy": 50.0,
          "r_net": -1.014, "r_price": -1.0, "result": "loss", "opened_utc": "2026-10-07 06:00:00",
          "closed_utc": "2026-10-07 08:01:00", "day": "2026-10-07", "why_open": "x" * 300,
          "consulted_analysis": True, "ref_price": 1.0999}
RECENT = [{"type": "live_trade_closed", "ticket": i, "symbol": "EURUSD", "side": "buy", "reason": "sl",
           "result": "loss", "r_price": -1.0, "net": -50.7, "closed_utc": "2026-10-07 08:01:00"} for i in range(30)]

EVENTS = {
    "live_reassess": {"snapshot": SNAP, "recent_events": RECENT, "next_decision_utc": "06:00 UTC",
                      "movers": [{"symbol": s, "pct_move": 0.4, "bars": 12} for s in list(R.INSTRUMENTS)[:8]]},
    "live_setup_candidate": {"snapshot": SNAP, "candidates": [{"symbol": "XAUUSD", "pct_move": 0.51, "bars": 12},
                                                              {"symbol": "GBPJPY", "pct_move": -0.4, "bars": 12}]},
    "live_trade_closed": {"snapshot": SNAP, **CLOSED},
    "live_day_closed": {"snapshot": SNAP, "day": "2026-10-06", "own_realized": -20.0, "closed_trades": 2,
                        "orders_placed": 2, "traded": True, "journal_notes": 1, "results": ["loss", "win"],
                        "currency": "EUR"},
    "live_risk_warning": {"snapshot": SNAP, "kind": "daily", "used": 300.0, "limit": 480.0, "used_pct_of_limit": 62.5},
    "live_risk_review": {"snapshot": SNAP, "day": "2026-10-09", "weekly": True, "closed_today": 3,
                         "today": {"closed": 3, "wins": 1, "losses": 2, "net": -10.0, "mean_r_net": -0.2},
                         "all_time": {"closed": 40, "wins": 18, "losses": 22, "net": -12.3, "mean_r_net": -0.02},
                         "last_20": {"closed": 20, "wins": 9, "losses": 11, "net": -5.0, "mean_r_net": -0.03},
                         "lever": {"risk_pct": 0.003, "set_by": "initial default", "min": 0.003, "max": 0.01,
                                   "history": [{"utc": "x", "old": 0.003, "new": 0.004, "why": "y" * 200}] * 10}},
}


class LiveRoutingTest(unittest.TestCase):
    def test_every_loop_event_routes_to_a_live_kind(self):
        for ev, payload in EVENTS.items():
            kind, _ = LR.route(ev, payload)
            self.assertIn(kind, P.LIVE_KINDS, ev)

    def test_recorded_only_events_wake_nobody(self):
        self.assertIsNone(LR.route("live_halt", {"reason": "day-loss halt"}))
        self.assertIsNone(LR.route("live_trade_opened", {"ticket": 1}))
        self.assertIsNone(LR.route("live_day_closed", {"day": "2026-10-06", "traded": False, "journal_notes": 0}))
        self.assertIsNone(LR.route("live_setup_candidate", {"candidates": []}))

    def test_an_unknown_event_is_an_error_not_a_silent_drop(self):
        with self.assertRaises(ValueError):
            LR.route("turn_reassess", {})

    def test_every_prompt_fits_the_live_engines_prompt_cap(self):
        for ev, payload in EVENTS.items():
            _, prompt = LR.route(ev, payload)
            self.assertLess(len(prompt), P.LIVE_LIMITS["max_prompt_chars"], f"{ev}: {len(prompt)} chars")

    def test_no_live_prompt_points_at_the_replays_tools(self):
        for ev, payload in EVENTS.items():
            _, prompt = LR.route(ev, payload)
            self.assertNotIn("paper.py", prompt, ev)
            self.assertNotIn("/tools/board.py", prompt, ev)
            self.assertIn("THIS IS THE REAL ACCOUNT", prompt, ev)

    def test_a_manual_close_gets_the_extra_scrutiny_question(self):
        _, prompt = LR.route("live_trade_closed", {**EVENTS["live_trade_closed"], "reason": "manual"})
        self.assertIn("NOT A STOP OR TARGET", prompt)
        _, prompt = LR.route("live_trade_closed", EVENTS["live_trade_closed"])
        self.assertNotIn("NOT A STOP OR TARGET", prompt)

    def test_the_decision_turn_has_no_traded_days_quota_and_knows_when_the_next_one_is(self):
        _, prompt = LR.route("live_reassess", EVENTS["live_reassess"])
        self.assertIn("or a skip with the reason, a normal outcome", prompt)   # 2026-11-01: no push to trade on live
        self.assertNotIn("expected, not optional", prompt)
        self.assertIn("The next decision turn is at 06:00 UTC", prompt)
        self.assertIn("TOP MOVERS", prompt)

    def test_the_end_of_window_checkpoint_goes_to_the_one_kind_that_owns_the_lever(self):
        kind, prompt = LR.route("live_risk_review", EVENTS["live_risk_review"])
        self.assertEqual(kind, "live_risk")
        self.assertIn("risk-factor [--set PCT] [--day-halt F] [--giveback F] [--max-positions N]", prompt)   # 2026-10-20: all its levers

    def test_a_scout_can_hand_any_symbol_to_the_decider_not_only_the_four(self):
        _, prompt = LR.route("live_setup_candidate", EVENTS["live_setup_candidate"])
        self.assertIn('the decider kind "live" for any other symbol', prompt)

    def test_every_prompt_states_the_madrid_window(self):
        for ev, payload in EVENTS.items():
            _, prompt = LR.route(ev, payload)
            self.assertIn("Europe/Madrid", prompt, ev)

    def test_the_symbol_list_matches_the_guards(self):
        self.assertEqual(tuple(LR.LIVE_TRADER_SYMBOLS), tuple(P.SYMBOL_TRADERS))


    def test_dreaming_goes_to_live_dream_with_the_rules_and_without_the_account(self):
        lessons = ["Entered on the breakout bar; the pullback entry would have halved the risk. " * 3] * 15
        kind, prompt = LR.route("live_dream", {"lessons": lessons, "snapshot": SNAP})
        self.assertEqual(kind, "live_dream")
        self.assertIn("dreams.py propose", prompt)
        self.assertNotIn("1000000001", prompt)            # the account never reaches the dreamer
        self.assertLess(len(prompt), P.LIVE_LIMITS["max_prompt_chars"])
        self.assertIsNone(LR.route("live_dream", {"lessons": []}))

if __name__ == "__main__":
    unittest.main()
