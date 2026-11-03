"""Tests for the entries replay log (decision vs fill vs close), tools/fills_view.py:entries()."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import fills_view as FV  # noqa: E402


class EntriesTest(unittest.TestCase):
    def test_joins_decision_fill_and_close_by_id(self):
        rows = [
            {"type": "order_placed", "id": 1, "symbol": "EURUSD", "side": "buy", "lots": 0.1,
             "utc": "2026-01-05 10:00 UTC", "ref": 1.1000, "sl": 1.0950, "tp": 1.1100, "why": "test"},
            {"type": "trade_opened", "id": 1, "symbol": "EURUSD", "side": "buy", "lots": 0.1,
             "entry": 1.10005, "sl": 1.0950, "tp": 1.1100, "why": "test", "risk_usd": 50.0,
             "opened_utc": "2026-01-05 11:00 UTC", "utc": "2026-01-05 11:00 UTC"},
            {"type": "trade_closed", "id": 1, "closed_utc": "2026-01-06 09:00 UTC", "exit": 1.1100,
             "reason": "tp", "net_usd": 95.0, "result": "win", "r_price": 1.99, "r_net": 1.9},
        ]
        out = FV.entries(rows)
        self.assertEqual(len(out), 1)
        r = out[0]
        self.assertEqual(r["decision_utc"], "2026-01-05 10:00 UTC")
        self.assertEqual(r["ref_price"], 1.1000)
        self.assertEqual(r["fill_utc"], "2026-01-05 11:00 UTC")
        self.assertAlmostEqual(r["slippage"], 1.10005 - 1.1000, places=6)
        self.assertEqual(r["exit_price"], 1.1100)
        self.assertEqual(r["result"], "win")

    def test_rejected_order_has_no_fill_but_shows_the_reason(self):
        rows = [
            {"type": "order_placed", "id": 2, "symbol": "EURUSD", "side": "buy", "lots": 1.0,
             "utc": "2026-01-05 10:00 UTC", "ref": 1.1000, "sl": 1.0000, "tp": None, "why": "oversized"},
            {"type": "order_rejected", "id": 2, "reason": "stop risk exceeds per-trade cap"},
        ]
        out = FV.entries(rows)
        self.assertEqual(out[0]["fill_utc"], None)
        self.assertEqual(out[0]["result"], "rejected")
        self.assertEqual(out[0]["rejected_reason"], "stop risk exceeds per-trade cap")

    def test_still_open_position_has_no_exit_yet(self):
        rows = [
            {"type": "order_placed", "id": 3, "symbol": "EURUSD", "side": "sell", "lots": 0.1,
             "utc": "2026-01-05 10:00 UTC", "ref": 1.1000, "sl": 1.1050, "tp": None, "why": "test"},
            {"type": "trade_opened", "id": 3, "symbol": "EURUSD", "side": "sell", "lots": 0.1,
             "entry": 1.09995, "sl": 1.1050, "tp": None, "why": "test", "risk_usd": 50.0,
             "opened_utc": "2026-01-05 11:00 UTC", "utc": "2026-01-05 11:00 UTC"},
        ]
        out = FV.entries(rows)
        self.assertEqual(out[0]["result"], "open")
        self.assertIsNone(out[0]["exit_price"])


if __name__ == "__main__":
    unittest.main()
