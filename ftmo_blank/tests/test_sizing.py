"""Sizing from broker truth (2026-10-12, operator: "we don't want it to just mis-lot").

The first live trade risked 2.53 EUR of an intended ~28: the worker sized 0.04 USOIL.cash lots by
hand assuming 1,000 barrels a lot (the broker's lot is 100), and the order reply never said what it
really risked. Now: order --dry-run shows the broker's stop risk per lot, the cap and the lots that
fit it, next to the runners' own valuation table; order --risk-pct sizes from that; every placed
order states its real risk and flags one far below the cap. FakeMT5 only, never the real bridge."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from live_fakes import FakeMT5, point_live_broker_at  # noqa: E402
import live_broker as L  # noqa: E402

# EURUSD buy at the fake ask 1.10000 with the stop at 1.0980: 0.002 x 100,000 = 200 USD per lot.
ASK, SL, PER_LOT = 1.10000, 1.0980, 200.0


class Sizing(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "state").mkdir()
        (self.tmp / "state" / "MODE").write_text("LIVE")
        self._restore = point_live_broker_at(self.tmp)
        self.fake = FakeMT5(balance=10_000.0, equity=10_000.0, tick_bid=1.09990, tick_ask=ASK)
        L._mt5_client = lambda: self.fake
        self._paths = L.RUNNER_SPEC_PATHS
        (self.tmp / "marlin.json").write_text(json.dumps({"EURUSD": {"usd_per_price_per_lot": 100000.0}}))
        L.RUNNER_SPEC_PATHS = [self.tmp / "marlin.json", self.tmp / "extra.json"]
        self.cap = L._cap_pct(L._live_risk_pct()) * 10_000.0

    def tearDown(self):
        L.RUNNER_SPEC_PATHS = self._paths
        self._restore()

    def arm(self):
        (self.tmp / "state" / "LIVE_ARMED").write_text("armed by the test\n")

    def cli(self, *argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            L.main(list(argv))
        return json.loads(buf.getvalue())

    def test_the_table_shows_broker_risk_per_lot_the_cap_and_the_lots_that_fit(self):
        t = L.size("EURUSD", "buy", SL)
        self.assertTrue(t["ok"], t)
        self.assertAlmostEqual(t["broker"]["stop_risk_per_lot_USD"], PER_LOT, places=2)
        self.assertAlmostEqual(t["per_trade_cap"], self.cap, places=2)
        self.assertAlmostEqual(t["max_lots_within_cap"], int(self.cap / PER_LOT / 0.01) * 0.01, places=8)
        self.assertEqual(t["runner_table"]["usd_per_1.0_price_move_per_lot"], 100000.0)
        self.assertAlmostEqual(t["runner_table"]["stop_risk_per_lot_usd"], PER_LOT, places=2)

    def test_given_lots_it_states_their_real_risk(self):
        t = L.size("EURUSD", "buy", SL, lots=0.01)
        self.assertAlmostEqual(t["risk"], 2.0, places=2)
        self.assertAlmostEqual(t["cap_used_pct"], round(200 / self.cap, 1), places=1)
        self.assertTrue(t["fits_cap"])
        self.assertFalse(L.size("EURUSD", "buy", SL, lots=1.0)["fits_cap"])

    def test_risk_pct_is_percent_of_balance(self):
        t = L.size("EURUSD", "buy", SL, risk_pct=0.1)            # 0.1% of 10,000 = 10 -> 0.05 lots
        self.assertAlmostEqual(t["lots_for_risk_pct"], 0.05, places=8)
        self.assertAlmostEqual(t["risk"], 10.0, places=2)
        self.assertFalse(L.size("EURUSD", "buy", SL, risk_pct=6)["ok"])   # past the ceiling (the 5% daily line, 2026-10-20)

    def test_a_wrong_side_stop_is_refused(self):
        self.assertFalse(L.size("EURUSD", "buy", 1.2)["ok"])

    def test_a_symbol_missing_from_the_runners_table_says_so(self):
        self.assertIn("not in the runners' table", L.size("GBPUSD", "buy", SL)["runner_table"])

    def test_order_by_risk_pct_sizes_from_the_broker_and_reports_the_risk(self):
        self.arm()
        r = L.place_order("EURUSD", "buy", None, SL, None, "sized from risk, not mental math",
                          risk_pct=L._live_risk_pct() * 100)
        self.assertTrue(r["ok"], r)
        want = int(self.cap / PER_LOT / 0.01) * 0.01
        self.assertAlmostEqual(self.fake.sent[0]["volume"], want, places=8)
        self.assertAlmostEqual(r["risk"], want * PER_LOT, places=2)
        self.assertNotIn("note", r)

    def test_an_order_far_below_the_cap_is_flagged_in_its_reply(self):
        self.arm()
        r = L.place_order("EURUSD", "buy", 0.01, SL, None, "a small order on purpose or by slip")
        self.assertTrue(r["ok"], r)
        self.assertIn("only", r["note"])
        self.assertIn("--dry-run", r["note"])

    def test_a_stop_too_wide_for_the_risk_is_refused_and_never_sent(self):
        self.arm()
        r = L.place_order("EURUSD", "buy", None, 1.0, None, "stop far too wide for the lever", risk_pct=0.3)
        self.assertFalse(r["ok"])
        self.assertIn("too wide", r["reason"])
        self.assertEqual(self.fake.sent, [])

    def test_dry_run_places_and_logs_nothing_even_unarmed(self):
        t = self.cli("order", "--symbol", "EURUSD", "--side", "buy", "--sl", str(SL), "--dry-run")
        self.assertTrue(t["ok"], t)
        self.assertEqual(self.fake.sent, [])
        self.assertFalse((self.tmp / "state" / "live_ledger.jsonl").exists())

    def test_the_cli_wants_exactly_one_of_lots_or_risk_pct(self):
        self.arm()
        both = self.cli("order", "--symbol", "EURUSD", "--side", "buy", "--sl", str(SL), "--lots", "0.01",
                        "--risk-pct", "0.3", "--why", "both given on purpose here")
        neither = self.cli("order", "--symbol", "EURUSD", "--side", "buy", "--sl", str(SL),
                           "--why", "neither given on purpose here")
        self.assertFalse(both["ok"])
        self.assertFalse(neither["ok"])
        self.assertEqual(self.fake.sent, [])


if __name__ == "__main__":
    unittest.main()
