"""tools/castigation.py: the training desk's usage meter since its last order, and the castigation (2026-11-01)."""
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import castigation as CG  # noqa: E402


class CastigationTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        (self.home / "state").mkdir()
        self._errors = CG.DELTA_ERRORS_FILE        # never the dashboard's real Errors tab from a test
        CG.DELTA_ERRORS_FILE = self.home / "errors.jsonl"
        self.addCleanup(setattr, CG, "DELTA_ERRORS_FILE", self._errors)
        con = sqlite3.connect(self.home / "state" / "selfcall.db")
        con.execute("CREATE TABLE demands (id TEXT, kind TEXT, created REAL, cost_usd REAL)")
        con.commit()
        con.close()
        self.ledger = self.home / "state" / "ledger.jsonl"
        self.ledger.write_text(json.dumps({"type": "order_placed", "bar": 5, "id": 1}) + "\n")

    def spend(self, usd, at, kind="scout", n=1):
        con = sqlite3.connect(self.home / "state" / "selfcall.db")
        for i in range(n):
            con.execute("INSERT INTO demands VALUES (?,?,?,?)", (f"{at}{i}", kind, at, usd / n))
        con.commit()
        con.close()

    def test_the_meter_counts_spend_since_the_last_order_as_a_share_of_a_window(self):
        m = CG.meter(self.home, now=1000.0)
        self.assertEqual((m["state"], m["usd"], m["calls"]), ("ok", 0.0, 0))
        self.spend(CG.USD_PER_WINDOW * 0.10, 1001.0, n=4)
        self.spend(5.0, 999.0)                          # before the meter started: not this stretch's
        m = CG.meter(self.home, now=1002.0)
        self.assertAlmostEqual(m["pct"], 10.0, places=0)
        self.assertEqual(m["calls"], 4)
        self.assertIn("10% of one 5-hour usage window", CG.self_text(m))

    def test_25_percent_without_an_order_castigates_once_and_50_stops_training(self):
        CG.meter(self.home, now=1000.0)
        fired = []
        self.spend(CG.USD_PER_WINDOW * 0.26, 1001.0)
        m = CG.settle(self.home, now=1002.0, on_castigate=fired.append)
        self.assertEqual((m["state"], m["changed"], len(fired)), ("castigated", "castigated", 1))
        m = CG.settle(self.home, now=1003.0, on_castigate=fired.append)
        self.assertEqual((m["state"], m["changed"], len(fired)), ("castigated", None, 1))   # once, not every turn
        self.assertIn("THIS TURN MUST END WITH AN ORDER", CG.castigation_text(m))
        self.spend(CG.USD_PER_WINDOW * 0.25, 1004.0)
        m = CG.settle(self.home, now=1005.0)
        self.assertEqual((m["state"], m["changed"]), ("stopped", "stopped"))
        self.assertTrue((self.home / "state" / CG.STOP_MARKER).exists())          # training's own marker...
        self.assertFalse((self.home / "state" / "STOP").exists())                 # ...never the one live shares
        self.assertFalse((self.home / "state" / "MANUAL_PAUSE").exists())
        self.assertIn("CASTIGATION STOP", (self.home / "errors.jsonl").read_text())

    def test_a_resume_gives_one_more_quarter_still_castigated(self):
        CG.meter(self.home, now=1000.0)
        self.spend(CG.USD_PER_WINDOW * 0.6, 1001.0)
        CG.settle(self.home, now=1002.0)
        m = CG.settle(self.home, now=1003.0)
        self.assertEqual(m["state"], "stopped")
        (self.home / "state" / CG.STOP_MARKER).unlink()  # the operator pressed Resume
        m = CG.meter(self.home, now=2000.0)
        self.assertEqual(m["state"], "castigated")
        self.assertAlmostEqual(m["pct"], CG.STOP_PCT - CG.CASTIGATE_PCT, places=0)

    def test_an_order_resets_the_meter_and_lifts_the_castigation(self):
        CG.meter(self.home, now=1000.0)
        self.spend(CG.USD_PER_WINDOW * 0.3, 1001.0)
        self.assertEqual(CG.settle(self.home, now=1002.0)["state"], "castigated")
        with open(self.ledger, "a") as f:
            f.write(json.dumps({"type": "order_placed", "bar": 9, "id": 2}) + "\n")
        m = CG.meter(self.home, now=1003.0)
        self.assertEqual((m["state"], m["usd"], m["strikes"]), ("ok", 0.0, 0))

    def test_paper_tags_an_order_placed_while_castigated(self):
        import paper as P
        p = P.Paper.__new__(P.Paper)
        p.home = self.home
        self.assertFalse(p._castigated())
        (self.home / "state" / "castigation.json").write_text(json.dumps({"state": "castigated"}))
        self.assertTrue(p._castigated())


class TrainingRailsAndGateTest(unittest.TestCase):
    """2026-11-01: the halt that silently ended trading, and the order gate."""
    def test_a_first_target_under_1_5R_is_refused_and_says_why(self):
        import paper as P
        self.assertIn("required", P.rr_refusal("buy", 100.0, 99.0, None))
        self.assertIn("1.20R", P.rr_refusal("buy", 100.0, 99.0, 101.2))
        self.assertIsNone(P.rr_refusal("sell", 100.0, 101.0, 98.5))

    def test_only_a_loss_halt_silences_the_desk(self):
        import paper as P
        p = P.Paper.__new__(P.Paper)
        for text, blocked in (("day-loss halt: x", True), ("peak-giveback halt: x", True), ("max concurrent positions: x", False), (None, False)):
            p._own_rails_block = lambda text=text: text
            self.assertEqual(p._rails_blocked_now(), blocked)

    def test_a_mover_mid_range_wakes_no_scout(self):
        import paper as P
        p = P.Paper.__new__(P.Paper)
        rows = [{"h": 110.0, "l": 100.0, "c": 105.0}] * 20
        p.recent_bars = lambda s, n, tf: rows + [{"h": 109.0, "l": 108.0, "c": 109.0}]
        self.assertTrue(p._at_extreme({"symbol": "X", "pct_move": 0.4}))
        self.assertFalse(p._at_extreme({"symbol": "X", "pct_move": -0.4}))


class LiveIsNeverCastigatedTest(unittest.TestCase):
    """Operator, 2026-11-01: castigation and the push to trade are training's only; live trades without rushing."""
    def test_no_live_prompt_carries_the_training_push_or_the_castigation(self):
        import live_routing as LR
        import desk_shared as DS
        for text in (DS.SCOUT_TASK, DS.DECIDER_TASK, DS.ALERT_TASK, LR.live_context({"snapshot": {}})):
            for word in ("CASTIGAT", "ON THE TRAINING DESK", "usage", "never learns"):
                self.assertNotIn(word, text)
        import inspect
        self.assertNotIn("castigation", inspect.getsource(LR))


class TwinScreenTest(unittest.TestCase):
    def test_one_correlated_move_wakes_one_scout(self):
        import desk_shared as DS
        got = DS.collapse_twins([{"symbol": "XAUEUR", "pct_move": -0.31}, {"symbol": "XAUUSD", "pct_move": -0.45},
                                 {"symbol": "EURUSD", "pct_move": 0.3}, {"symbol": "UKOIL.cash", "pct_move": 0.9}])
        self.assertEqual([c["symbol"] for c in got], ["XAUUSD", "EURUSD", "UKOIL.cash"])


class BoardWorkerTest(unittest.TestCase):
    def test_a_symbol_trader_may_post_as_itself(self):
        import board as TB
        self.assertEqual(TB._worker("trade_us100_cash"), "trade")
        self.assertEqual(TB._worker("scout"), "scout")


if __name__ == "__main__":
    unittest.main()
