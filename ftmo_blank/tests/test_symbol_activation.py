"""Symbol activation for the live assembly (2026-10-12, operator: workers must be able to enable a
symbol that is not active on the broker -- even a stock they only want to read).

ensure_symbol activates a symbol that is not in the terminal's Market Watch and waits for a quote;
bars reads ANY broker symbol and activates + retries when the broker sends nothing; symbols --find
searches the broker's whole list; the move screen activates a symbol that returned no bars. Ordering
stays limited to guard/rules.py INSTRUMENTS. No test reaches the real bridge (FakeMT5 only)."""
import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
from live_fakes import FakeMT5, PINNED_NOW, m5_rates, point_live_broker_at  # noqa: E402
import live_broker as L  # noqa: E402
import live_market as LM  # noqa: E402
import live_loop as LL  # noqa: E402


class MarketWatchMT5(FakeMT5):
    """A terminal whose Market Watch matters: `visible` maps symbol -> active?; a symbol missing from
    it is not offered by the broker at all. An inactive symbol has no quote and no bars. After
    activation, the first `history_lag` bar fetches still come back empty (history downloading)."""

    def __init__(self, visible, rates=None, refuse=(), history_lag=0, never_quotes=(), **kw):
        super().__init__(rates=rates, **kw)
        self.visible = dict(visible)
        self.refuse = set(refuse)
        self.never_quotes = set(never_quotes)
        self.history_lag = history_lag
        self.selected = []

    def symbol_info(self, symbol):
        if symbol not in self.visible:
            return None
        si = super().symbol_info(symbol)
        si.visible = self.visible[symbol]
        si.description = f"{symbol} description"
        si.path = f"Path\\{symbol}"
        si.name = symbol
        return si

    def symbol_select(self, symbol, enable):
        if symbol not in self.visible or symbol in self.refuse:
            return False
        self.selected.append(symbol)
        self.visible[symbol] = True
        return True

    def symbol_info_tick(self, symbol):
        if not self.visible.get(symbol) or symbol in self.never_quotes:
            return None
        return super().symbol_info_tick(symbol)

    def copy_rates_from_pos(self, symbol, timeframe, start_pos, count):
        if not self.visible.get(symbol):
            return None
        if self.history_lag > 0:
            self.history_lag -= 1
            return []
        return super().copy_rates_from_pos(symbol, timeframe, start_pos, count)

    def symbols_get(self, group=None):
        names = sorted(self.visible)
        if group:
            needle = group.strip("*")
            names = [n for n in names if needle in n]
        return tuple(self.symbol_info(n) for n in names)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "state").mkdir()
        self._restore = point_live_broker_at(self.tmp)
        self._sleeps = (L.time.sleep, LM.time.sleep)
        L.time.sleep = LM.time.sleep = lambda s: None   # the waits are real seconds on the bridge

    def tearDown(self):
        L.time.sleep, LM.time.sleep = self._sleeps
        self._restore()


class EnsureSymbol(Base):
    def test_inactive_symbol_is_activated_and_quoted(self):
        m = MarketWatchMT5({"USDSGD": False})
        res = L.ensure_symbol(m, "USDSGD")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["status"], "activated")
        self.assertEqual(m.selected, ["USDSGD"])
        self.assertIn("bid", res)
        self.assertTrue(res["tradable_here"])

    def test_active_symbol_is_left_alone(self):
        m = MarketWatchMT5({"EURUSD": True})
        res = L.ensure_symbol(m, "EURUSD")
        self.assertEqual(res["status"], "already_active")
        self.assertEqual(m.selected, [])

    def test_symbol_the_broker_does_not_offer(self):
        res = L.ensure_symbol(MarketWatchMT5({}), "NOPE")
        self.assertFalse(res["ok"])
        self.assertEqual(res["status"], "not_at_broker")
        self.assertIn("--find", res["reason"])

    def test_terminal_refuses_activation(self):
        res = L.ensure_symbol(MarketWatchMT5({"USDMXN": False}, refuse={"USDMXN"}), "USDMXN")
        self.assertFalse(res["ok"])
        self.assertEqual(res["status"], "select_failed")

    def test_activated_but_no_quote_yet(self):
        res = L.ensure_symbol(MarketWatchMT5({"AAPL": False}, never_quotes={"AAPL"}), "AAPL", wait_s=0)
        self.assertFalse(res["ok"])
        self.assertEqual(res["status"], "no_quote_yet")
        self.assertFalse(res["tradable_here"])

    def test_tick_helper_activates_on_an_empty_quote(self):
        m = MarketWatchMT5({"USDSGD": False})
        self.assertIsNotNone(L._tick(m, "USDSGD"))
        self.assertEqual(m.selected, ["USDSGD"])


class Bars(Base):
    def rates(self, sym, n=30):
        return {(sym, FakeMT5.TIMEFRAME_M5): m5_rates(100.0, 0.1, n, PINNED_NOW)}

    def test_reads_a_symbol_outside_the_tradable_set(self):
        m = MarketWatchMT5({"NVDA": True}, rates=self.rates("NVDA"))
        rows = LM.bars("NVDA", n=5, m=m)
        self.assertEqual(len(rows), 5)
        self.assertNotIn("error", rows[0])

    def test_inactive_symbol_is_activated_then_read(self):
        # bars' own up-front select activates it; the history then lags two fetches
        m = MarketWatchMT5({"NVDA": False}, rates=self.rates("NVDA"), history_lag=2)
        rows = LM.bars("NVDA", n=5, m=m)
        self.assertEqual(len(rows), 5, rows)
        self.assertIn("NVDA", m.selected)

    def test_symbol_not_at_broker_says_so(self):
        rows = LM.bars("NOPE", n=5, m=MarketWatchMT5({}))
        self.assertEqual(len(rows), 1)
        self.assertIn("not offered by this broker", rows[0]["error"])

    def test_history_that_never_arrives_is_an_error_not_an_empty_list(self):
        m = MarketWatchMT5({"COCOA.c": True}, rates={})
        rows = LM.bars("COCOA.c", n=5, m=m)
        self.assertEqual(len(rows), 1)
        self.assertIn("no M5 bars for COCOA.c", rows[0]["error"])


class FindAndScreen(Base):
    def test_find_searches_the_whole_broker_list(self):
        m = MarketWatchMT5({"NVDA": False, "EURUSD": True, "AAPL": True})
        got = LM.find_symbols("NVDA", m=m)
        self.assertEqual([g["symbol"] for g in got], ["NVDA"])
        self.assertFalse(got[0]["active"])
        self.assertFalse(got[0]["tradable_here"])
        self.assertTrue(LM.find_symbols("EURUSD", m=m)[0]["tradable_here"])

    def test_find_nothing(self):
        self.assertIn("error", LM.find_symbols("ZZZ", m=MarketWatchMT5({"EURUSD": True}))[0])

    def test_screen_activates_a_symbol_with_no_bars(self):
        vis = {s: True for s in LL.R.INSTRUMENTS}
        vis["USDSGD"] = False
        m = MarketWatchMT5(vis)
        LL.scan_candidates(m, PINNED_NOW, flagged=[])
        self.assertIn("USDSGD", m.selected)
        self.assertTrue(m.visible["USDSGD"])

    def test_orders_stay_limited_to_the_tradable_set(self):
        ok, why = L._risk_check({"equity": 10_000.0, "balance": 10_000.0, "day_start_equity": 10_000.0,
                                 "initial_balance": 10_000.0, "risk_pct": 0.003, "currency": "USD"},
                                "NVDA", "buy", 1.0, 90.0, None, ref_price=100.0, open_risk=0.0,
                                pending_risk=0.0, risk=10.0, vmin=0.01, vstep=0.01)
        self.assertFalse(ok)
        self.assertIn("no instrument spec for NVDA", why)


if __name__ == "__main__":
    unittest.main()
