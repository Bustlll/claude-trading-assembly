"""Tests for the live-trading worker (manual/LIVE_TRADING_IDEA.md section 3). As of 2026-10-10
connected() is a REAL probe against the shared MT5 bridge, not a permanent stub -- so these tests
monkeypatch L._mt5_client (the one seam every broker call goes through) with either a function
that raises (simulating "no broker") or a _FakeMT5 double (simulating a connected one), never a
real bridge. That keeps every gate -- rationale, armed, connected, breach, self-hedge, the real
worst-case risk math, order_send's retcode handling -- provably correct and hermetic, exactly the
standard this project already holds itself to for the paper broker."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOME / "tools"))
sys.path.insert(0, str(HOME))
import live_broker as L  # noqa: E402


from live_fakes import FakeMT5 as _FakeMT5, pos as _pos, deal as _deal, no_broker as _no_broker  # noqa: E402
from live_fakes import point_live_broker_at, PINNED_NOW  # noqa: E402
import datetime as dt  # noqa: E402


class LiveBrokerGateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "state").mkdir()
        self._restore = point_live_broker_at(self.tmp)
        self.set_mode("LIVE")  # the master switch is part of the gate since 2026-10-11; tests start in LIVE

    def tearDown(self):
        self._restore()

    def set_mode(self, mode):
        (self.tmp / "state" / "MODE").write_text(mode)

    def arm(self):
        (self.tmp / "state" / "LIVE_ARMED").write_text("armed by the test\n")

    def use_fake(self, fake: _FakeMT5):
        L._mt5_client = lambda: fake

    def use_no_broker(self):
        L._mt5_client = _no_broker

    # -- connected() is now a real probe through the one seam, mocked both ways --------------
    def test_connected_reports_false_when_the_broker_is_unreachable(self):
        self.use_no_broker()
        self.assertFalse(L.connected())

    def test_connected_reports_true_when_the_broker_answers(self):
        self.use_fake(_FakeMT5())
        self.assertTrue(L.connected())

    def test_unarmed_refuses_before_even_checking_connected(self):
        self.use_fake(_FakeMT5())  # a reachable broker changes nothing: armed is checked first
        self.assertFalse(L.armed())
        r = L.place_order("EURUSD", "buy", 0.1, 1.0950, None, "unarmed order attempt here")
        self.assertFalse(r["ok"])
        self.assertIn("not armed", r["reason"])

    def test_armed_but_unconnected_still_refuses(self):
        """armed is not enough -- connected matters too, simulated here (not relying on whatever
        is actually logged into the real shared bridge right now)."""
        self.arm()
        self.use_no_broker()
        r = L.place_order("EURUSD", "buy", 0.1, 1.0950, None, "armed but no broker exists yet")
        self.assertFalse(r["ok"])
        self.assertIn("no broker connected", r["reason"])

    def test_a_rationale_is_required_even_before_the_armed_check(self):
        r = L.place_order("EURUSD", "buy", 0.1, 1.0950, None, "short")
        self.assertFalse(r["ok"])
        self.assertIn("rationale", r["reason"])

    def test_self_hedge_blocks_an_opposite_side_position_in_the_same_symbol(self):
        ok, reason = L._self_hedge_ok("EURUSD", "buy", [{"symbol": "EURUSD", "side": "sell"}])
        self.assertFalse(ok)
        self.assertIn("self-hedge", reason)

    def test_self_hedge_allows_the_same_side_or_a_different_symbol(self):
        self.assertTrue(L._self_hedge_ok("EURUSD", "buy", [{"symbol": "EURUSD", "side": "buy"}])[0])
        self.assertTrue(L._self_hedge_ok("EURUSD", "buy", [{"symbol": "XAUUSD", "side": "sell"}])[0])

    def test_self_hedge_is_checked_before_the_order_is_refused_for_the_right_reason(self):
        self.arm()
        self.use_fake(_FakeMT5())  # armed+connected so self-hedge, not an earlier gate, fires
        r = L.place_order("EURUSD", "buy", 0.1, 1.0950, None, "would clash with an existing short",
                          open_positions=[{"symbol": "EURUSD", "side": "sell"}])
        self.assertFalse(r["ok"])
        self.assertIn("self-hedge", r["reason"])

    def test_close_and_modify_also_refuse_with_no_broker(self):
        self.arm()
        self.use_no_broker()
        self.assertFalse(L.close_position("EURUSD", 1, "closing a real position attempt")["ok"])
        self.assertFalse(L.modify_stop("EURUSD", 1, 1.0980, "moving a real stop attempt")["ok"])

    def test_every_refusal_is_logged_to_its_own_ledger_not_the_paper_ones(self):
        L.place_order("EURUSD", "buy", 0.1, 1.0950, None, "logged refusal check entry")
        rows = [json.loads(l) for l in L.LEDGER_PATH.read_text().splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["type"], "order_refused")
        self.assertEqual(rows[0]["source"], L.SOURCE_TAG)

    def test_status_reports_both_switches_and_the_live_reason(self):
        self.use_no_broker()
        s = L.status()
        self.assertFalse(s["armed"])
        self.assertFalse(s["connected"])
        self.assertIn("not armed", s["would_refuse"])
        self.arm()
        self.assertIn("no broker connected", L.status()["would_refuse"])

    def test_status_reports_the_real_account_numbers_when_connected(self):
        self.use_fake(_FakeMT5(balance=9606.52, equity=9606.52, login=1000000001, currency="EUR"))
        s = L.status()
        self.assertTrue(s["connected"])
        self.assertEqual(s["account"]["login"], 1000000001)
        self.assertEqual(s["account"]["currency"], "EUR")
        self.assertAlmostEqual(s["account"]["balance"], 9606.52)
        # first time this login is seen, its own current balance becomes its anchor
        self.assertAlmostEqual(s["account"]["initial_balance"], 9606.52)

    def test_the_live_lever_is_its_own_and_ignores_the_paper_lever(self):
        """Since 2026-10-11 live has its own lever (state/live_risk.json), tuned by the live_risk kind.
        A paper risk_pct must not move real-money sizing."""
        (self.tmp / "state" / "account.json").write_text(json.dumps({"risk_pct": 0.009}))
        self.assertAlmostEqual(L._live_risk_pct(), L.LIVE_RISK_INITIAL_PCT)

    def test_setting_the_live_lever_changes_what_status_shows_and_what_sizing_uses(self):
        r = L.set_risk_factor(0.005, "three weeks of clean +R trades, n=40")
        self.assertTrue(r["ok"], r)
        self.assertAlmostEqual(L._live_risk_pct(), 0.005)
        self.use_fake(_FakeMT5(balance=10_000.0, equity=10_000.0))
        self.assertAlmostEqual(L.status()["account"]["risk_pct"], 0.005)
        self.assertEqual(L.risk_factor()["history"][-1]["new"], 0.005)
        row = json.loads(L.LEDGER_PATH.read_text().splitlines()[-1])
        self.assertEqual((row["type"], row["new"]), ("risk_factor_changed", 0.005))

    def test_the_live_lever_refuses_outside_the_guard_range_or_without_a_reason(self):
        # 2026-10-20: the range is FTMO-bounded only (0.1% .. the 5% daily line)
        self.assertFalse(L.set_risk_factor(0.0005, "below the floor on purpose")["ok"])
        self.assertFalse(L.set_risk_factor(0.06, "above the daily line on purpose")["ok"])
        self.assertFalse(L.set_risk_factor(0.004, "short")["ok"])
        self.assertAlmostEqual(L._live_risk_pct(), L.LIVE_RISK_INITIAL_PCT)

    def test_the_own_rails_are_risk_levers_bounded_only_by_the_ftmo_lines(self):
        self.assertEqual(L._lever("day_halt_pct"), L.DAY_LOSS_HALT_PCT)          # the old defaults until set
        self.assertEqual(L._lever("max_positions"), L.MAX_CONCURRENT_POSITIONS)
        r = L.set_risk_levers("evidence says the 1.5% halt cuts good days short", day_halt=0.03, giveback=0.05,
                              max_positions=12)
        self.assertTrue(r["ok"], r)
        self.assertEqual((L._lever("day_halt_pct"), L._lever("giveback_halt_pct"), L._lever("max_positions")),
                         (0.03, 0.05, 12))
        self.assertFalse(L.set_risk_levers("past the FTMO daily line on purpose", day_halt=0.06)["ok"])
        self.assertFalse(L.set_risk_levers("past the FTMO total line on purpose", giveback=0.11)["ok"])
        self.assertFalse(L.set_risk_levers("short", max_positions=3)["ok"])
        self.assertEqual(L.risk_factor()["levers"]["day_halt_pct"]["value"], 0.03)

    def test_a_hand_edited_lever_file_is_still_clamped_to_the_guard_range(self):
        L.LIVE_RISK_PATH.write_text(json.dumps({"risk_pct": 0.5}))  # absurd, must clamp
        self.assertAlmostEqual(L._live_risk_pct(), L.R.MAX_RISK_PER_TRADE)

    # -- the three individually-tuned safety halts (operator's explicit numbers, 2026-10-10) ----
    def test_day_loss_halt_blocks_new_orders_once_this_workers_own_day_pnl_is_past_the_threshold(self):
        self.arm()
        # anchor $10,000 -> 1.5% halt = $150. A realized loss of $160 today, filtered to OUR
        # magic only, must trip it even though an unrelated deal (different magic) also lost money.
        deals = [_deal("EURUSD", -160.0, magic=L.MAGIC), _deal("XAUUSD", -500.0, magic=313700)]
        fake = _FakeMT5(balance=10_000.0, equity=10_000.0, deals=deals)
        self.use_fake(fake)
        r = L.place_order("EURUSD", "buy", 0.01, 1.0980, 1.1020, "should be blocked by the day-loss halt")
        self.assertFalse(r["ok"])
        self.assertIn("day-loss halt", r["reason"])
        self.assertEqual(fake.sent, [])

    def test_day_loss_halt_ignores_other_workers_losses_on_the_shared_account(self):
        self.arm()
        # Same account, a loss past this worker's own 1.5% halt ($150) but on a DIFFERENT magic (e.g.
        # deltaForceFTMO's own runner) -- must NOT block this worker's own order. Kept under the
        # account-wide FTMO 5% day limit on purpose: since 2026-10-11 the day's start is rebuilt from
        # deal history, so a loss taken today (by anyone) does count toward THAT ceiling, as it must.
        deals = [_deal("XAUUSD", -300.0, magic=313700)]
        fake = _FakeMT5(balance=10_000.0, equity=10_000.0, deals=deals, tick_bid=1.09990, tick_ask=1.10000)
        self.use_fake(fake)
        r = L.place_order("EURUSD", "buy", 0.01, 1.0980, 1.1020, "unrelated loss must not block this")
        self.assertTrue(r["ok"], r)

    def test_peak_giveback_halt_blocks_once_this_workers_own_equity_gives_back_enough_from_its_peak(self):
        self.arm()
        # Deal history is cumulative over real time -- same fake, same broker-side history growing
        # between calls, not two unrelated snapshots. First call: realized +$400 -> records a new
        # peak (anchor + 400 = 10400), well clear of the 3% halt.
        fake = _FakeMT5(balance=10_000.0, equity=10_000.0, deals=[_deal("EURUSD", 400.0, magic=L.MAGIC)])
        self.use_fake(fake)
        self.assertTrue(L.place_order("EURUSD", "buy", 0.01, 1.0980, 1.1020, "banks the peak at +400")["ok"])
        # More history accrues on top: a further realized -$350 brings cumulative realized to
        # +50 (own equity 10050) -- giveback from the recorded peak (10400) is $350, past the 3%
        # of $10,000 = $300 halt.
        fake._deals.append(_deal("EURUSD", -350.0, magic=L.MAGIC))
        r = L.place_order("EURUSD", "buy", 0.01, 1.0980, 1.1020, "should now be blocked by giveback")
        self.assertFalse(r["ok"])
        self.assertIn("peak-giveback halt", r["reason"])

    def test_max_concurrent_positions_refuses_the_sixth_of_this_workers_own(self):
        self.arm()
        five = [_pos(i, "EURUSD", "buy", 0.01, 1.1000, sl=1.0950, magic=L.MAGIC) for i in range(5)]
        fake = _FakeMT5(balance=10_000.0, equity=10_000.0, positions=five, tick_bid=1.24980, tick_ask=1.25000)
        self.use_fake(fake)
        r = L.place_order("GBPUSD", "buy", 0.01, 1.2480, 1.2520, "the sixth concurrent position")
        self.assertFalse(r["ok"])
        self.assertIn("max concurrent", r["reason"])
        self.assertEqual(fake.sent, [])

    def test_max_concurrent_ignores_other_workers_positions_on_the_shared_account(self):
        self.arm()
        five_theirs = [_pos(i, "EURUSD", "buy", 0.01, 1.1000, sl=1.0950, magic=313700) for i in range(5)]
        # per-symbol quotes: open risk is now measured from each position's CURRENT price, so EURUSD
        # must be quoted at a EURUSD price, not GBPUSD's
        fake = _FakeMT5(balance=10_000.0, equity=10_000.0, positions=five_theirs,
                        ticks={"EURUSD": (1.09990, 1.10000), "GBPUSD": (1.24980, 1.25000)})
        self.use_fake(fake)
        r = L.place_order("GBPUSD", "buy", 0.01, 1.2480, 1.2520, "not our own concurrent count")
        self.assertTrue(r["ok"], r)

    def test_the_live_lever_starts_at_the_operators_0_3_percent(self):
        """Operator, 2026-10-11: 0.3% until the live_risk worker decides otherwise -- the guard floor,
        so the number shown and the number sized with are the same (the old 0.1% start was below the
        floor and silently clamped up to it)."""
        self.assertAlmostEqual(L._live_risk_pct(), 0.003)
        self.assertEqual(L.risk_factor()["set_by"], "initial default")

    def test_the_singleton_lock_refuses_a_second_concurrent_holder(self):
        with L._SingletonLock():
            with self.assertRaises(RuntimeError):
                with L._SingletonLock():
                    pass  # pragma: no cover -- must never actually enter

    def test_cli_status_runs_and_is_valid_json(self):
        import contextlib
        import io
        self.use_no_broker()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(L.main(["status"]), 0)
        json.loads(buf.getvalue())  # must not raise

    # -- the REAL risk gate, end to end against a fake but realistic account ------------------
    def test_order_within_the_risk_cap_is_placed_for_real(self):
        self.arm()
        fake = _FakeMT5(balance=10_000.0, equity=10_000.0, tick_bid=1.09990, tick_ask=1.10000)
        self.use_fake(fake)
        # 0.01 lot, 20-pip stop on EURUSD (spec: usd_per_price_unit_per_lot ~100000) -> ~$20 risk,
        # comfortably inside the 0.7% default cap of $70.
        r = L.place_order("EURUSD", "buy", 0.01, 1.0980, 1.1020, "small order inside every cap")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["ticket"], 555)
        self.assertEqual(len(fake.sent), 1)
        self.assertEqual(fake.sent[0]["magic"], L.MAGIC)
        self.assertEqual(fake.sent[0]["comment"], L.SOURCE_TAG[:31])

    def test_order_past_the_per_trade_risk_cap_is_refused_not_placed(self):
        self.arm()
        fake = _FakeMT5(balance=10_000.0, equity=10_000.0, tick_bid=1.09990, tick_ask=1.10000)
        self.use_fake(fake)
        # 1.0 lot, 20-pip stop -> ~$2000 risk, far past the 0.7% ($70) cap.
        r = L.place_order("EURUSD", "buy", 1.0, 1.0980, 1.1020, "too large for the real cap")
        self.assertFalse(r["ok"])
        self.assertIn("per-trade cap", r["reason"])
        self.assertEqual(fake.sent, [])  # never reached the broker

    def test_order_refused_by_the_broker_reports_the_retcode_not_a_false_ok(self):
        self.arm()
        fake = _FakeMT5(balance=10_000.0, equity=10_000.0, tick_bid=1.09990, tick_ask=1.10000,
                        send_retcode=10013)  # TRADE_RETCODE_INVALID, an arbitrary non-success code
        self.use_fake(fake)
        r = L.place_order("EURUSD", "buy", 0.01, 1.0980, 1.1020, "broker will refuse this one")
        self.assertFalse(r["ok"])
        self.assertIn("retcode=10013", r["reason"])

    def test_order_send_exception_is_reported_and_never_retried(self):
        self.arm()
        fake = _FakeMT5(balance=10_000.0, equity=10_000.0, tick_bid=1.09990, tick_ask=1.10000,
                        send_raises=ConnectionError("rpyc link dropped"))
        self.use_fake(fake)
        r = L.place_order("EURUSD", "buy", 0.01, 1.0980, 1.1020, "bridge drops mid-send")
        self.assertFalse(r["ok"])
        self.assertIn("NOT retried", r["reason"])
        self.assertEqual(len(fake.sent), 1)  # attempted exactly once

    def test_self_hedge_is_checked_account_wide_against_a_real_fetched_position(self):
        self.arm()
        existing = _pos(1, "EURUSD", "sell", 0.2, 1.1000, sl=1.1050)
        fake = _FakeMT5(balance=10_000.0, equity=10_000.0, positions=[existing],
                        tick_bid=1.09990, tick_ask=1.10000)
        self.use_fake(fake)
        r = L.place_order("EURUSD", "buy", 0.01, 1.0980, 1.1020, "clashes with a real fetched position")
        self.assertFalse(r["ok"])
        self.assertIn("self-hedge", r["reason"])
        self.assertEqual(fake.sent, [])

    def test_close_sends_the_opposite_side_order_against_the_real_position(self):
        self.arm()
        existing = _pos(42, "XAUUSD", "buy", 0.1, 4100.0, sl=4080.0, magic=L.MAGIC)
        fake = _FakeMT5(positions=[existing], tick_bid=4105.0, tick_ask=4105.5)
        self.use_fake(fake)
        r = L.close_position("XAUUSD", 42, "closing the real fetched position")
        self.assertTrue(r["ok"], r)
        self.assertEqual(fake.sent[0]["type"], fake.ORDER_TYPE_SELL)
        self.assertEqual(fake.sent[0]["position"], 42)
        self.assertEqual(fake.sent[0]["volume"], 0.1)

    def test_modify_rejects_widening_the_stop_via_the_shared_guard_rule(self):
        self.arm()
        existing = _pos(7, "EURUSD", "buy", 0.1, 1.1000, sl=1.0950, magic=L.MAGIC)
        fake = _FakeMT5(positions=[existing])
        self.use_fake(fake)
        r = L.modify_stop("EURUSD", 7, 1.0900, "widening a buy stop downward, must be rejected")
        self.assertFalse(r["ok"])
        self.assertIn("widen", r["reason"])
        self.assertEqual(fake.sent, [])


    # -- 2026-10-11: the live-loop round -------------------------------------------------------
    def test_mode_other_than_live_refuses_even_when_armed_and_connected(self):
        self.arm()
        fake = _FakeMT5(tick_bid=1.09990, tick_ask=1.10000)
        self.use_fake(fake)
        for mode in ("TRAINING", "OFF"):
            self.set_mode(mode)
            r = L.place_order("EURUSD", "buy", 0.01, 1.0980, 1.1020, "an order outside LIVE mode")
            self.assertFalse(r["ok"])
            self.assertIn(f"mode is {mode}", r["reason"])
            self.assertFalse(L.close_position("EURUSD", 1, "a close outside LIVE mode")["ok"])
        self.assertEqual(fake.sent, [])

    def test_no_new_entry_past_the_friday_flatten_cutoff_but_closing_still_works(self):
        self.arm()
        existing = _pos(42, "XAUUSD", "buy", 0.1, 4100.0, sl=4080.0, magic=L.MAGIC)
        fake = _FakeMT5(positions=[existing], tick_bid=1.09990, tick_ask=1.10000)
        self.use_fake(fake)
        L._now_utc = lambda: dt.datetime(2026, 10, 9, L.FRI_FLAT_HOUR_UTC, 5, tzinfo=dt.timezone.utc)  # Friday
        r = L.place_order("EURUSD", "buy", 0.01, 1.0980, 1.1020, "a Friday-evening entry attempt")
        self.assertFalse(r["ok"])
        self.assertIn("Friday flatten cutoff", r["reason"])
        self.assertEqual(fake.sent, [])
        self.assertTrue(L.close_position("XAUUSD", 42, "closing is never blocked by the cutoff")["ok"])

    def test_risk_is_measured_in_the_account_currency(self):
        """A EUR account: a 20-pip 0.4-lot EURUSD stop is $80 but only ~EUR 73.4 at 1.09. With the
        cap at 0.75% of EUR 10,000 = EUR 75, the USD figure would refuse and the EUR one passes."""
        self.arm()
        L.set_risk_factor(0.0075, "a cap that only the EUR figure fits under")
        fake = _FakeMT5(balance=10_000.0, equity=10_000.0, currency="EUR", fx=1 / 1.09,
                        tick_bid=1.09990, tick_ask=1.10000)
        self.use_fake(fake)
        r = L.place_order("EURUSD", "buy", 0.4, 1.0980, 1.1050, "sized against the EUR balance")
        self.assertTrue(r["ok"], r)
        placed = [json.loads(l) for l in L.LEDGER_PATH.read_text().splitlines()][-1]
        self.assertEqual(placed["currency"], "EUR")
        self.assertAlmostEqual(placed["risk_ccy"], 80.0 / 1.09, places=1)

    def test_deal_windows_use_true_utc_not_the_broker_clock(self):
        """The bridge's own date bounds are broker-clock; a deal 1h ago must count toward TODAY
        and one from yesterday evening (true UTC) must not, whatever its raw broker epoch says."""
        fresh = _deal("EURUSD", -100.0, magic=L.MAGIC, when=PINNED_NOW - dt.timedelta(hours=1))
        prague_midnight = dt.datetime(2026, 10, 6, 22, 0, tzinfo=dt.timezone.utc)
        yesterday = _deal("EURUSD", -999.0, magic=L.MAGIC, when=prague_midnight - dt.timedelta(minutes=30))
        fake = _FakeMT5(deals=[fresh, yesterday])
        self.assertAlmostEqual(L._own_realized_pnl_usd(fake, L._prague_day_start_utc()), -100.0)

    def test_day_start_is_the_balance_at_the_prague_reset_rebuilt_from_deal_history(self):
        """First read of the day happens late, after a -200 loss closed this morning (any magic):
        the day's start must be balance + 200, not whatever equity is at that first read."""
        morning = _deal("XAUUSD", -200.0, magic=313700, when=PINNED_NOW - dt.timedelta(hours=3))
        fake = _FakeMT5(balance=9_800.0, equity=9_750.0, deals=[morning])
        st = L._account_state(fake)
        self.assertAlmostEqual(st["day_start_equity"], 10_000.0)

    def test_journal_writes_to_the_live_ledger_and_needs_a_real_note(self):
        self.assertFalse(L.journal("short")["ok"])
        r = L.journal("skipped today: no clean structure on gold")
        self.assertTrue(r["ok"])
        row = json.loads(L.LEDGER_PATH.read_text().splitlines()[-1])
        self.assertEqual(row["type"], "journal")
        self.assertEqual(row["day"], "2026-10-07")

    def test_order_is_tagged_consulted_when_a_pre_trade_post_exists_on_the_live_board_today(self):
        import live_board
        self.arm()
        fake = _FakeMT5(tick_bid=1.09990, tick_ask=1.10000)
        self.use_fake(fake)
        r1 = L.place_order("EURUSD", "buy", 0.01, 1.0980, 1.1020, "no analysis posted before this one")
        self.assertFalse(r1["consulted_analysis"])
        live_board.post("chart", "pre_trade", "EURUSD holding 1.0980 support", "EURUSD", home=self.tmp)
        r2 = L.place_order("EURUSD", "buy", 0.01, 1.0980, 1.1020, "after the chart post this time")
        self.assertTrue(r2["consulted_analysis"])

    def test_order_placed_records_what_reconciliation_needs(self):
        self.arm()
        fake = _FakeMT5(tick_bid=1.09990, tick_ask=1.10000)
        self.use_fake(fake)
        L.place_order("EURUSD", "buy", 0.01, 1.0980, 1.1020, "record every field once")
        row = json.loads(L.LEDGER_PATH.read_text().splitlines()[-1])
        for key in ("ticket", "entry", "sl", "tp", "side", "lots", "risk_ccy", "currency", "why", "utc", "day",
                    "ref_price", "consulted_analysis"):
            self.assertIn(key, row)

    def test_snapshot_reports_the_account_own_positions_and_limits(self):
        mine = _pos(9, "EURUSD", "buy", 0.1, 1.1000, sl=1.0950, magic=L.MAGIC, profit=12.5)
        theirs = _pos(10, "XAUUSD", "sell", 0.2, 4100.0, sl=4150.0, magic=313700)
        fake = _FakeMT5(balance=10_000.0, equity=10_012.5, positions=[mine, theirs], currency="EUR")
        snap = L.snapshot(fake)
        self.assertEqual([p["ticket"] for p in snap["own_positions"]], [9])
        self.assertEqual(snap["other_runners_positions"], {"XAUUSD sell": 1})
        self.assertEqual(snap["currency"], "EUR")
        self.assertAlmostEqual(snap["daily_loss_limit"], 500.0)
        self.assertTrue(snap["market_open"])
        self.assertIn("EURUSD", snap["last_close"])
        self.assertIsNone(snap["entry_block"])


    def test_a_crypto_quote_alone_does_not_make_the_market_open(self):
        """ETHUSD quotes all weekend; found on the real bridge 2026-10-11 (a Sunday read open)."""
        stale = L.utc_to_broker_epoch(PINNED_NOW - dt.timedelta(hours=40))
        fake = _FakeMT5(tick_time=stale)
        fresh = SimpleNamespace(bid=2500.0, ask=2501.0, time=L.utc_to_broker_epoch(PINNED_NOW))
        real_tick = fake.symbol_info_tick
        fake.symbol_info_tick = lambda sym: fresh if sym == "ETHUSD" else real_tick(sym)
        self.assertFalse(L.snapshot(fake)["market_open"])


    def test_a_placed_order_is_written_to_the_delta_dashboards_ledger_with_its_own_tag(self):
        self.arm()
        self.use_fake(_FakeMT5(tick_bid=1.09990, tick_ask=1.10000))
        L.place_order("EURUSD", "sell", 0.01, 1.1050, 1.0950, "a short for the dashboard row")
        row = json.loads(L.DELTA_LEDGER_PATH.read_text().splitlines()[-1])
        self.assertEqual((row["source"], row["comment"], row["direction"], row["ticket"]),
                         ("ftmo_blank_live", "ftmo_blank_live", "SHORT", "555"))

    def test_a_refused_order_writes_no_dashboard_row(self):
        self.use_fake(_FakeMT5())   # not armed
        L.place_order("EURUSD", "buy", 0.01, 1.0980, 1.1020, "refused before the broker")
        self.assertFalse(L.DELTA_LEDGER_PATH.exists())


if __name__ == "__main__":
    unittest.main()


class LiveBrokerManageVerbsTest(LiveBrokerGateTest):
    """2026-10-19: the position manager's verbs -- partial close, target moves, the risk worker's one widen -- and
    the ownership rule under all of them: only this assembly's own positions (MAGIC) are ever touched."""

    def _one(self, side="buy", lots=0.1, entry=1.1000, sl=1.0950, tp=1.1100, magic=None, bid=1.10200, ask=1.10210):
        self.arm()
        p = _pos(7, "EURUSD", side, lots, entry, sl=sl, tp=tp, magic=L.MAGIC if magic is None else magic)
        fake = _FakeMT5(positions=[p], tick_bid=bid, tick_ask=ask)
        self.use_fake(fake)
        return fake

    def test_another_runners_position_is_never_touched(self):
        fake = self._one(magic=313700)
        for r in (L.close_position("EURUSD", 7, "closing a delta runner's position by ticket"),
                  L.close_position("EURUSD", 7, "part of a delta runner's position", volume=0.05),
                  L.modify_position("EURUSD", 7, 1.0990, None, "moving a delta runner's stop"),
                  L.modify_position("EURUSD", 7, None, 1.1200, "moving a delta runner's target"),
                  L.widen_stop("EURUSD", 7, 1.0930, "widening a delta runner's stop past the sweep zone")):
            self.assertFalse(r["ok"])
            self.assertIn("not this assembly's", r["reason"])
        self.assertEqual(fake.sent, [])

    def test_partial_close_sends_only_that_volume_against_the_position(self):
        fake = self._one(lots=0.58)
        r = L.close_position("EURUSD", 7, "bank half at the first level, let the rest run", volume=0.29)
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["partial"])
        self.assertAlmostEqual(r["remaining_lots"], 0.29)
        self.assertAlmostEqual(fake.sent[0]["volume"], 0.29)
        self.assertEqual(fake.sent[0]["position"], 7)
        self.assertEqual(fake.sent[0]["type"], fake.ORDER_TYPE_SELL)

    def test_a_split_below_the_minimum_is_refused_with_the_numbers(self):
        fake = self._one(lots=0.01)
        r = L.close_position("EURUSD", 7, "half of the minimum size cannot be closed", volume=0.005)
        self.assertFalse(r["ok"])
        self.assertEqual(fake.sent, [])
        fake = self._one(lots=0.02)
        r = L.close_position("EURUSD", 7, "0.015 rounds DOWN to the 0.01 step, never up to all of it", volume=0.015)
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["partial"])
        self.assertAlmostEqual(fake.sent[-1]["volume"], 0.01)

    def test_a_volume_at_or_above_the_position_closes_all_of_it(self):
        fake = self._one(lots=0.1)
        r = L.close_position("EURUSD", 7, "close everything, the plan is invalid", volume=0.5)
        self.assertTrue(r["ok"], r)
        self.assertFalse(r["partial"])
        self.assertAlmostEqual(fake.sent[0]["volume"], 0.1)

    def test_target_moves_either_way_but_never_behind_price(self):
        fake = self._one()
        self.assertTrue(L.modify_position("EURUSD", 7, None, 1.1150, "extend to the next H1 level")["ok"])
        self.assertEqual(fake.sent[-1]["sl"], 1.0950)      # the stop rides along unchanged
        self.assertTrue(L.modify_position("EURUSD", 7, None, 1.1030, "bring it in front of the level")["ok"])
        r = L.modify_position("EURUSD", 7, None, 1.1010, "a target the price is already past")
        self.assertFalse(r["ok"])
        self.assertIn("behind the current price", r["reason"])

    def test_stop_to_break_even_and_never_past_price(self):
        fake = self._one()
        r = L.modify_position("EURUSD", 7, 1.1000, None, "break-even at +0.5R as the plan said")
        self.assertTrue(r["ok"], r)
        self.assertEqual(fake.sent[-1]["tp"], 1.1100)       # the target rides along unchanged
        r = L.modify_position("EURUSD", 7, 1.1030, None, "a stop above the bid is a close, not a modify")
        self.assertFalse(r["ok"])
        self.assertIn("wrong side", r["reason"])

    def test_modify_cannot_widen_and_names_the_risk_workers_verb(self):
        self._one()
        r = L.modify_position("EURUSD", 7, 1.0930, None, "a wider stop through modify is refused")
        self.assertFalse(r["ok"])
        self.assertIn("widen", r["reason"])

    def test_widen_once_within_twice_the_entry_risk(self):
        fake = self._one(bid=1.10000, ask=1.10010)
        r = L.widen_stop("EURUSD", 7, 1.0890, "past twice the risk at entry: a different trade, refused")
        self.assertFalse(r["ok"])
        self.assertIn("2x", r["reason"])
        r = L.widen_stop("EURUSD", 7, 1.0930, "the plan's INV is a close below 1.0950; the sweeps reach 1.0935")
        self.assertTrue(r["ok"], r)
        self.assertEqual(fake.sent[-1]["sl"], 1.0930)
        self.assertEqual(fake.sent[-1]["tp"], 1.1100)
        rows = [json.loads(l) for l in (self.tmp / "state" / "live_ledger.jsonl").read_text().splitlines()]
        self.assertTrue(any(r["type"] == "widen_placed" and r["id"] == 7 for r in rows))
        r = L.widen_stop("EURUSD", 7, 1.0920, "a second widen on the same trade must be refused")
        self.assertFalse(r["ok"])
        self.assertIn("already widened once", r["reason"])

    def test_widen_must_be_wider_and_not_during_the_rollover_hold(self):
        self._one(bid=1.10000, ask=1.10010)
        r = L.widen_stop("EURUSD", 7, 1.0960, "tighter than the stop: that is a modify, refused here")
        self.assertFalse(r["ok"])
        self.assertIn("not wider", r["reason"])
        (self.tmp / "state" / "live_rollover.json").write_text(json.dumps({"7": {"orig": 1.0950, "wide": 1.0940}}))
        r = L.widen_stop("EURUSD", 7, 1.0930, "the watchdog holds the cushion now, refused until it restores")
        self.assertFalse(r["ok"])
        self.assertIn("rollover", r["reason"])
