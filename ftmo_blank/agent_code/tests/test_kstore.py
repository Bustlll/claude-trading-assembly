import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kstore  # noqa: E402

SYMBOLS = {"replayable": ["EURUSD", "US500.cash"], "specs_without_bars": ["BTCUSD"],
           "symbols": {"EURUSD": {"spec": {"contract_size": 100000.0, "digits": 5}, "caveats": ["snapshot spread"]},
                       "US500.cash": {"spec": {"contract_size": 1.0, "digits": 2}, "caveats": []},
                       "BTCUSD": {"spec": {"contract_size": 1.0}, "caveats": []}}}


def trade(i, sym, side, net, r, opened, closed, why):
    return {"type": "trade_closed", "id": i, "symbol": sym, "side": side, "net_usd": net, "r_net": r,
            "reason": "sl" if net < 0 else "tp", "risk_usd": 50.0, "opened_utc": opened, "closed_utc": closed,
            "why_open": why, "utc": closed}


LEDGER = [
    # in chronological WRITE order, as the real ledger always is: the last row is always "now"
    {"type": "note", "day": "2026-01-05", "text": "EURUSD range was narrow, no trade", "utc": "2026-01-05 10:00 UTC"},
    trade(1, "EURUSD", "buy", -50.0, -1.0, "2026-01-05 08:00 UTC", "2026-01-05 12:00 UTC", "breakout above range"),
    {"type": "day_closed", "day": "2026-01-05", "day_pnl": -50.0, "utc": "2026-01-05 23:00 UTC"},
    trade(2, "US500.cash", "sell", 80.0, 1.6, "2026-01-06 14:00 UTC", "2026-01-06 18:00 UTC", "rejection at high"),
    trade(3, "EURUSD", "buy", -45.0, -0.9, "2026-01-06 08:00 UTC", "2026-01-06 23:30 UTC", "breakout of asia range"),
]


class KStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        h = self.home = Path(self.tmp.name)
        for d in ("field", "knowledge", "papers", "manual", "state"):
            (h / d).mkdir()
        (h / "field" / "instruments_costs.md").write_text(
            "# Costs\nintro\n## Commission\nForex commission is 2.50 USD per lot per side.\n## Swap\nswap text\n")
        (h / "knowledge" / "symbols.json").write_text(json.dumps(SYMBOLS))
        (h / "knowledge" / "sessions.json").write_text(json.dumps({"daily_reset": "00:00 CE(S)T"}))
        (h / "papers" / "postmortem.md").write_text("# Postmortem\nOn 2026-01-06 the breakout lost again.\n")
        (h / "state" / "ledger.jsonl").write_text("\n".join(json.dumps(r) for r in LEDGER) + "\nnot json\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_spec_exact_case_and_prefix(self):
        self.assertEqual(kstore.spec("eurusd", self.home)["spec"]["digits"], 5)
        us = kstore.spec("us500", self.home)
        self.assertEqual(us["symbol"], "US500.cash")
        self.assertTrue(us["replayable"])
        self.assertFalse(kstore.spec("BTCUSD", self.home)["replayable"])
        self.assertIsNone(kstore.spec("XYZ", self.home)["symbol"])

    def test_last_losses_and_what_they_share(self):
        out = kstore.trades(self.home, result="loss")
        self.assertEqual([t["id"] for t in out["trades"]], [1, 3])
        c = out["common"]
        self.assertEqual(c["n"], 2)
        self.assertEqual(c["symbol"], {"EURUSD": 2})
        self.assertEqual(c["side"], {"buy": 2})
        self.assertEqual(c["exit_reason"], {"sl": 2})
        self.assertEqual(c["open_hour_utc"], {8: 2})
        self.assertAlmostEqual(c["r_net_mean"], -0.95)
        self.assertEqual(c["why_open_words_in_k_trades"]["breakout"], 2)
        self.assertEqual(c["net_usd_sum"], -95.0)
        self.assertEqual(kstore.trades(self.home, n=1)["trades"][0]["id"], 3)
        self.assertEqual(len(kstore.trades(self.home, symbol="us500.cash")["trades"]), 1)

    def test_day_uses_prague_boundary(self):
        d5 = kstore.day("2026-01-05", self.home)
        self.assertEqual(d5["journal"], ["EURUSD range was narrow, no trade"])
        self.assertEqual({e["type"] for e in d5["events"]}, {"trade_closed", "day_closed"})
        # closed 23:30 UTC on the 6th is 00:30 Prague on the 7th
        self.assertEqual([e["id"] for e in kstore.day("2026-01-07", self.home)["events"]], [3])
        self.assertEqual(kstore.day("2026-01-06", self.home)["papers"], ["papers/postmortem.md"])

    def test_find_ranks_all_words_first_and_covers_every_source(self):
        hits = kstore.find(["forex", "commission"], self.home)
        self.assertEqual(hits[0]["ref"], "field/instruments_costs.md:3")
        self.assertEqual(hits[0]["matched"], "2/2")
        refs = lambda words, where=None: [h["ref"] for h in kstore.find(words, self.home, where)]  # noqa: E731
        self.assertIn("knowledge/symbols.json#EURUSD", refs(["eurusd"], "knowledge"))
        self.assertIn("knowledge/sessions.json#daily_reset", refs(["reset"]))
        self.assertIn("papers/postmortem.md:1", refs(["postmortem"]))
        self.assertTrue(all(r.startswith("ledger:") for r in refs(["breakout"], "ledger")))
        self.assertEqual(kstore.find(["zzzz"], self.home), [])

    def test_overview_counts_and_bad_lines_skipped(self):
        o = kstore.overview(self.home)
        self.assertEqual(o["sections"]["ledger"], len(LEDGER))
        self.assertEqual(o["sections"]["field"], 3)
        self.assertEqual(o["ledger_types"]["trade_closed"], 3)

    def test_missing_sources_do_not_crash(self):
        with tempfile.TemporaryDirectory() as empty:
            self.assertEqual(kstore.trades(empty)["common"], {"n": 0})
            self.assertEqual(kstore.day("2026-01-05", empty)["events"], [])
            self.assertEqual(kstore.find(["x"], empty), [])
            self.assertIsNone(kstore.spec("EURUSD", empty)["symbol"])

    def test_cli_every_command(self):
        for argv, rc in (([], 0), (["spec", "EURUSD"], 0), (["spec", "nope"], 1), (["trades", "--result", "loss"], 0),
                         (["day", "2026-01-05"], 0), (["find", "commission", "--in", "field"], 0)):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(kstore.main(argv, self.home), rc, argv)
            self.assertTrue(buf.getvalue().strip(), argv)

    def test_real_home_indexes(self):
        o = kstore.overview()
        if (kstore.HOME / "knowledge" / "symbols.json").exists():
            self.assertGreater(o["sections"]["knowledge"], 0)
            self.assertEqual(kstore.spec("EURUSD")["symbol"], "EURUSD")
        if (kstore.HOME / "field" / "INDEX.md").exists():
            self.assertGreater(o["sections"]["field"], 0)


class BlindnessTest(unittest.TestCase):
    """After an epoch wraps, earlier rows sitting later in the FILE can describe the future relative
    to where the replay now sits. now_utc() must track write order, not calendar order, and every
    agent-facing read must hide anything after it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        h = self.home = Path(self.tmp.name)
        for d in ("field", "knowledge", "papers", "manual", "state"):
            (h / d).mkdir()
        # epoch 1 reaches 2026-06-01 (a "future" trade), THEN epoch 2 wraps back to 2026-01-05
        wrapped = [
            trade(1, "EURUSD", "buy", 500.0, 5.0, "2026-06-01 08:00 UTC", "2026-06-01 12:00 UTC", "epoch1 future trade"),
            {"type": "epoch_review", "utc": "2026-06-01 12:05 UTC", "epoch": 0},
            trade(2, "EURUSD", "sell", -40.0, -0.8, "2026-01-05 08:00 UTC", "2026-01-05 12:00 UTC", "epoch2 current trade"),
        ]
        (h / "state" / "ledger.jsonl").write_text("\n".join(json.dumps(r) for r in wrapped) + "\n")
        (h / "papers" / "future.md").write_text("# Future note\nOn 2026-06-01 EURUSD ran hard; buy the dip early.\n")
        (h / "papers" / "current.md").write_text("# Current note\nOn 2026-01-05 EURUSD looked rangebound.\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_now_is_the_last_written_row_not_the_latest_date(self):
        now = kstore.now_utc(self.home)
        self.assertEqual(now.strftime("%Y-%m-%d %H:%M"), "2026-01-05 12:00")

    def test_visible_ledger_hides_the_future_trade(self):
        ids = [r["id"] for r in kstore.visible_ledger(self.home) if r.get("type") == "trade_closed"]
        self.assertEqual(ids, [2])

    def test_trades_and_find_never_surface_the_future_trade(self):
        self.assertEqual([t["id"] for t in kstore.trades(self.home)["trades"]], [2])
        refs = [h["ref"] for h in kstore.find(["trade"], self.home, "ledger")]
        self.assertTrue(all("future" not in r for r in refs))

    def test_a_paper_naming_a_future_day_is_not_found(self):
        hits = {h["ref"] for h in kstore.find(["eurusd"], self.home, "papers")}
        self.assertIn("papers/current.md:1", hits)
        self.assertNotIn("papers/future.md:1", hits)

    def test_day_does_not_list_the_future_paper(self):
        self.assertEqual(kstore.day("2026-06-01", self.home)["papers"], [])


if __name__ == "__main__":
    unittest.main()
