"""2026-10-20: the vault -- every closed trade, its entry read by code and what happened after (tools/vault.py)."""
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
import vault as V  # noqa: E402

T0 = dt.datetime(2026, 3, 4, 10, 0, tzinfo=dt.timezone.utc).timestamp()


def walk(n, start, step, t0=T0 - 300 * 400):
    out, px = [], start
    for i in range(n):
        c = px + step
        out.append((t0 + 300 * i, px, max(px, c) + abs(step) * 0.2, min(px, c) - abs(step) * 0.2, c))
        px = c
    return out


class FeatureOutcomeTest(unittest.TestCase):
    def test_a_buy_at_the_top_of_an_uptrend_reads_as_with_extreme_and_trend_with(self):
        before = walk(400, 100.0, 0.01)
        f = V.features("buy", before[-1][4], before[-1][4] - 0.3, before[-1][4] + 0.6, T0, before)
        self.assertEqual((f["h1"], f["h4"]), ("with", "with"))
        self.assertIn(f["loc"], ("with_extreme", "breakout"))
        self.assertEqual(f["rr"], 2.0)
        self.assertIn(f["stop_vs_sweep"], ("inside", "past_median", "past_max"))

    def test_outcome_counts_r_and_a_stop_that_swept_then_reached_the_target(self):
        entry, sl, tp = 100.0, 99.0, 101.0
        after = [(T0, 100.0, 100.2, 98.9, 99.0)]                     # stopped on the first bar
        after += [(T0 + 300 * (i + 1), 99.0 + 0.1 * i, 99.1 + 0.1 * i, 98.95 + 0.1 * i, 99.1 + 0.1 * i) for i in range(30)]
        o = V.outcome("buy", entry, sl, tp, after, T0 + 300, "sl")
        self.assertFalse(o["hit_1r_before_stop"])
        self.assertTrue(o["swept_then_target"])
        self.assertGreaterEqual(o["mfe_24h"], 1.0)
        self.assertGreater(o["stop_to_survive_r"], 1.0)              # the stop it needed was wider than the one used


class DeskViewTest(unittest.TestCase):
    def test_training_sees_real_trades_without_dates_or_prices_and_its_own_only_up_to_its_clock(self):
        tmp = Path(tempfile.mkdtemp())
        saved = V.ANALYSED
        V.ANALYSED = tmp / "analysed.jsonl"
        try:
            rows = [{"id": "account:1:1", "source": "account:1", "runner": "pocketc", "symbol": "US30.cash", "side": "buy",
                     "open_utc": "2026-09-01 10:00:00", "close_utc": "2026-09-01 11:00:00", "entry": 1, "sl": 0.9,
                     "tp": 1.1, "R": 1.0, "net": 5, "plan": None, "sim": False, "f": {"rel": "flat", "atr15": 0.1},
                     "o": {}},
                    {"id": "assembly_training:1", "source": "assembly_training", "runner": "assembly", "symbol": "XAUUSD",
                     "side": "sell", "open_utc": "2025-06-01 10:00:00", "close_utc": "2025-06-01 12:00:00", "R": -1.0,
                     "sim": True, "f": {"rel": "chase"}, "o": {}},
                    {"id": "assembly_training:2", "source": "assembly_training", "runner": "assembly", "symbol": "XAUUSD",
                     "side": "sell", "open_utc": "2025-06-09 10:00:00", "close_utc": "2025-06-09 12:00:00", "R": 1.0,
                     "sim": True, "f": {"rel": "flat"}, "o": {}}]
            V.ANALYSED.write_text("".join(json.dumps(r) + "\n" for r in rows))
            seen = V.load("training", dt.datetime(2025, 6, 5, tzinfo=dt.timezone.utc).timestamp())
            real = [r for r in seen if not r.get("sim")]
            self.assertEqual(len(seen), 2)                               # the later replay trade is not shown
            self.assertTrue(all(k not in real[0] for k in ("open_utc", "entry", "sl", "tp", "net")))
            self.assertNotIn("atr15", real[0]["f"])
            self.assertEqual(len(V.load("live")), 3)
        finally:
            V.ANALYSED = saved

    def test_similar_ranks_look_alikes_and_reports_what_happened_after(self):
        rows = [{"id": str(i), "symbol": "XAUUSD", "side": "buy", "runner": "x", "R": (1.0 if i % 2 else -1.0),
                 "open_utc": "2026-09-01 10:00:00", "f": {"rel": "flat", "loc": "pullback", "h1": "with", "h4": "with",
                                                          "session": "london", "candle": "normal",
                                                          "stop_vs_sweep": "past_max"},
                 "o": {"mfe_4h": 1.2, "mae_4h": 0.4}} for i in range(6)]
        text = V.similar(dict(rows[0]["f"]), "XAUUSD", "buy", rows, desk="live")
        self.assertIn("look-alikes", text)
        self.assertIn("n=6", text)


class ConsultVaultTest(unittest.TestCase):
    def test_the_vault_answers_inside_the_turn_like_the_other_specialists(self):
        import consult as C
        from guard import policy as PO
        from selfcall.engine import Engine
        tmp = Path(tempfile.mkdtemp())
        (tmp / "state").mkdir()
        got = []

        def fake(d):
            got.append(d)
            return {"answer": {"status": "done", "summary": "VAULT VERDICT ADJUST | n=31, -0.27R | stop past the sweep",
                               "findings": [], "sources": [], "next_demands": []}, "cost_usd": 0.03, "session_id": "s",
                    "denials": []}

        eng = Engine(home=tmp, kinds=PO.LIVE_KINDS, limits=PO.LIVE_LIMITS, invoker=fake)
        res = C.consult("live", "vault", "XAUUSD", "do look-alike pullback buys pay here?", "THE TRADE: buy",
                        view_builder=lambda *a: ("VAULT CHECK XAUUSD buy x", eng), home=tmp)
        self.assertTrue(res["ok"], res)
        self.assertEqual(got[0]["kind"], "live_vault")
        self.assertIn("YOU ARE THE VAULT", got[0]["prompt"])
        post = json.loads((tmp / "state" / "live_board.jsonl").read_text().splitlines()[-1])
        self.assertEqual((post["worker"], post["topic"]), ("vault", "consult"))


if __name__ == "__main__":
    unittest.main()


class AfterCloseTest(unittest.TestCase):
    """2026-10-20: the day close walks every closed trade forward from its close (a swept stop is a lesson)."""

    def test_a_stop_then_target_is_reported_as_swept(self):
        import opportunities as OPP
        rows = [{"type": "trade_closed", "symbol": "USOIL.cash", "side": "buy", "entry": 98.768, "initial_sl": 98.2,
                 "tp": 99.55, "reason": "sl", "r_net": -1.05, "closed_utc": "2026-10-19 13:27:30", "day": "2026-10-19"}]
        bars = [{"utc": f"2026-10-19 {h:02d}:{mm:02d}", "o": 98.3, "h": 98.4 + (0.3 if h >= 16 else 0), "l": 98.1, "c": 98.3}
                for h in range(13, 18) for mm in (0, 30)]
        after = OPP.closed_after(rows, "2026-10-19", lambda r: r["day"], lambda s, tf, n: bars, "live")
        self.assertEqual(len(after), 1)
        self.assertFalse(after[0]["swept"])                        # neither the target nor +1R after the stop
        self.assertEqual(after[0]["target_after_utc"], None)       # 98.7 < 99.55: target not reached ...
        self.assertIsNone(after[0]["plus_1r_after_utc"])           # ... nor +1R (99.336)
        bars[-1]["h"] = 99.6
        after = OPP.closed_after(rows, "2026-10-19", lambda r: r["day"], lambda s, tf, n: bars, "live")
        self.assertTrue(after[0]["swept"])
        self.assertEqual(after[0]["target_after_utc"], "2026-10-19 17:30")
        self.assertIn("AFTER THE STOP PRICE REACHED THE TARGET", OPP.after_text(after))
