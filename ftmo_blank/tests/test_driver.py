"""Tests for driver.py's OFF/TRAINING/LIVE master-switch gate. mode() is a pure read of
state/MODE; turn()'s own gating on it is a trivial branch tested indirectly by trusting mode()
plus the pre-existing "halted" handling driver.py already has for other halt reasons."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOME))
sys.path.insert(0, str(HOME / "tools"))
sys.path.insert(0, str(HOME.parent / "selfcall"))
import driver as D  # noqa: E402


class ModeGateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "state").mkdir()
        self._real_home = D.HOME
        D.HOME = self.tmp

    def tearDown(self):
        D.HOME = self._real_home

    def write(self, text):
        (self.tmp / "state" / "MODE").write_text(text)

    def test_no_mode_file_defaults_to_training(self):
        self.assertEqual(D.mode(), "TRAINING")

    def test_each_real_mode_round_trips(self):
        for m in ("OFF", "TRAINING", "LIVE"):
            self.write(m)
            self.assertEqual(D.mode(), m)

    def test_garbage_in_the_file_falls_back_to_training_not_a_crash(self):
        self.write("whatever this is not a mode\n")
        self.assertEqual(D.mode(), "TRAINING")

    def test_whitespace_is_tolerated(self):
        self.write("  LIVE  \n")
        self.assertEqual(D.mode(), "LIVE")



import datetime as dt  # noqa: E402
import json  # noqa: E402
import sqlite3  # noqa: E402
import time  # noqa: E402
import live_broker as LB  # noqa: E402
import live_loop as LL  # noqa: E402
from live_fakes import FakeMT5, broker_epoch, point_live_broker_at  # noqa: E402

WED_0702 = dt.datetime(2026, 10, 7, 7, 2, tzinfo=dt.timezone.utc)


def _done(cost=0.1):
    return lambda d: {"answer": {"status": "done", "summary": "held: nothing clean", "findings": [], "sources": [],
                                 "next_demands": []}, "cost_usd": cost, "session_id": "fake", "denials": []}


class LiveTickTest(unittest.TestCase):
    """driver.py live_tick end to end, with a fake broker and a fake Claude: what the loop reports
    becomes demands of LIVE kinds on the LIVE queue -- never the replay's queue -- unless the budget,
    a usage-limit wait or a halt says otherwise, and every such refusal is recorded."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "state").mkdir()
        self._saved = (D.HOME, D.verify_guard, D._account_store, D.policy.LIVE_DAILY_MAX_CALLS, D.DELTA_ERRORS_FILE)
        D.DELTA_ERRORS_FILE = Path(tempfile.mkdtemp()) / "never_the_live_file.jsonl"
        D.HOME = self.tmp
        D.verify_guard = lambda: None  # the real guard hash check is about the real folder, not this temp one
        D._account_store = lambda: None  # never the operator's real castl registry from a test
        self._restore = point_live_broker_at(self.tmp)
        (self.tmp / "state" / "MODE").write_text("LIVE")
        LB._now_utc = lambda: WED_0702
        self.fake = FakeMT5(tick_time=broker_epoch(WED_0702))
        LB._mt5_client = lambda: self.fake
        self.calls = []

    def tearDown(self):
        D.HOME, D.verify_guard, D._account_store, D.policy.LIVE_DAILY_MAX_CALLS, D.DELTA_ERRORS_FILE = self._saved
        self._restore()

    def invoker(self, cost=0.1):
        inner = _done(cost)

        def run(d):
            self.calls.append(d)
            return inner(d)
        return run

    def test_refuses_outside_live_mode(self):
        (self.tmp / "state" / "MODE").write_text("TRAINING")
        self.assertFalse(D.live_tick(now=WED_0702, invoker=self.invoker())["ok"])
        self.assertEqual(self.calls, [])

    def test_a_decision_hour_runs_the_live_kind_on_the_live_queue_only(self):
        res = D.live_tick(now=WED_0702, invoker=self.invoker())
        self.assertEqual([e["event"] for e in res["events"]], ["live_reassess"])
        self.assertEqual([c["kind"] for c in self.calls], ["live"])
        self.assertIn("THIS IS THE REAL ACCOUNT", self.calls[0]["prompt"])
        live_db = sqlite3.connect(self.tmp / "state" / "live_selfcall.db")
        self.assertEqual(live_db.execute("SELECT kind, status FROM demands").fetchall(), [("live", "done")])
        train_db = sqlite3.connect(self.tmp / "state" / "selfcall.db")
        self.assertEqual(train_db.execute("SELECT COUNT(*) FROM demands").fetchone()[0], 0)
        self.assertTrue((self.tmp / "state" / "live_turns.jsonl").exists())
        self.assertEqual(json.loads((self.tmp / "state" / "market_feed.json").read_text())["mode"], "LIVE")

    def test_spend_never_stops_a_call_but_the_runaway_guard_does(self):
        eng = D.make_live_engine()
        did = eng.enqueue("live_timing", "an expensive earlier call today", source="test")
        eng.store.set(did, status="done", started=time.time(), finished=time.time(), cost_usd=500.0)
        D.live_tick(now=WED_0702, invoker=self.invoker())
        self.assertEqual([c["kind"] for c in self.calls], ["live"])  # $500 today: still decides
        D.policy.LIVE_DAILY_MAX_CALLS = 2  # now pretend a spawn loop already made two calls today
        LB._now_utc = lambda: WED_0702 + dt.timedelta(hours=1)
        self.fake._tick_time = broker_epoch(WED_0702 + dt.timedelta(hours=1))
        D.DELTA_ERRORS_FILE = self.tmp / "delta_errors.jsonl"
        res = D.live_tick(now=WED_0702 + dt.timedelta(hours=1), invoker=self.invoker())
        self.assertEqual(len(self.calls), 1)
        self.assertIn("runaway guard", res["skipped"][0]["reason"])
        # ...and it says so loudly: the delta Errors tab, this dashboard's marker, its own error log
        row = json.loads(D.DELTA_ERRORS_FILE.read_text().splitlines()[-1])
        self.assertEqual((row["component"], row["level"]), ("ftmo_blank.assembly", "error"))
        self.assertIn("RUNAWAY GUARD", row["message"])
        self.assertTrue((self.tmp / "state" / "live_alert.json").exists())
        self.assertEqual(len((self.tmp / "state" / "live_errors.jsonl").read_text().splitlines()), 1)
        self.assertFalse(D.runaway_alert(5, 2)["alerted"])    # once a day, not every tick

    def test_a_usage_limit_wait_queues_nothing_new(self):
        eng = D.make_live_engine()
        did = eng.enqueue("live_timing", "a call that hit the usage limit", source="test")
        eng.store.set(did, next_try=time.time() + 3600)
        res = D.live_tick(now=WED_0702, invoker=self.invoker())
        self.assertEqual(self.calls, [])
        self.assertEqual(res["drain"], "limited")
        self.assertIn("usage limit", res["skipped"][0]["reason"])

    def test_a_stale_queued_live_demand_expires_instead_of_running_late(self):
        eng = D.make_live_engine()
        did = eng.enqueue("live_scout", "a scan from hours ago", source="test")
        eng.store.db.execute("UPDATE demands SET created=? WHERE id=?", (time.time() - D.LIVE_DEMAND_TTL_S - 60, did))
        eng.store.db.commit()
        res = D.live_tick(now=WED_0702, invoker=self.invoker())
        self.assertEqual(res["expired"], 1)
        self.assertNotIn("live_scout", [c["kind"] for c in self.calls])

    def test_the_loop_sleeps_to_the_next_m5_close_on_weekdays_and_coarsely_over_the_weekend(self):
        wed = dt.datetime(2026, 10, 7, 10, 2, 30, tzinfo=dt.timezone.utc)
        self.assertAlmostEqual(D._live_sleep_s(wed), 150 + 20)
        sat = dt.datetime(2026, 10, 10, 12, 0, tzinfo=dt.timezone.utc)
        self.assertEqual(D._live_sleep_s(sat), D.LIVE_WEEKEND_SLEEP_S)
        sun = dt.datetime(2026, 10, 11, 20, 50, tzinfo=dt.timezone.utc)
        self.assertEqual(D._live_sleep_s(sun), 600)  # wakes right at the 21:00 reopen

    def test_turn_does_not_advance_the_replay_in_live(self):
        res = D.turn(24)
        self.assertEqual(res["drain"], "halted")
        self.assertIn("live-run", res["detail"])



class AccountFailoverTest(unittest.TestCase):
    """failover_invoker against castl's own AccountStore on a temp file: a usage limit on one account
    moves the SAME call to the next one; only when all wait does the engine see the limit."""

    def setUp(self):
        sys.path.insert(0, str(D.CASTL_HOME))
        from castl_lib.state import AccountStore
        self.tmp = Path(tempfile.mkdtemp())
        self.store = AccountStore(self.tmp / "accounts.json")
        self.store.add("main", str(D.DEFAULT_CLAUDE_DIR))
        self.store.add("second", str(self.tmp / "claude-b"))
        self._env = os.environ.get("CLAUDE_CONFIG_DIR")

    def tearDown(self):
        if self._env is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = self._env

    def test_a_limit_on_one_account_retries_the_same_call_on_the_other(self):
        from selfcall.engine import LimitHit
        seen = []

        def base(d):
            seen.append(os.environ.get("CLAUDE_CONFIG_DIR", "default"))
            if len(seen) == 1:
                raise LimitHit("You've hit your session limit · resets 4:50pm (Europe/Madrid)")
            return {"answer": {"status": "done"}, "cost_usd": 0.0, "session_id": "x", "denials": []}
        run = D.failover_invoker(base, store_factory=lambda: self.store)
        self.assertEqual(run({"kind": "live"})["answer"]["status"], "done")
        self.assertEqual(seen, ["default", str(self.tmp / "claude-b")])  # main = unset var, as before
        self.store.reload()
        self.assertTrue(self.store.is_exhausted("main"))
        self.assertFalse(self.store.is_exhausted("second"))

    def test_an_account_that_is_down_moves_the_call_and_stays_out_for_hours(self):
        # 2026-11-02: account_2's subscription was switched off; selfcall raises that as "account down"
        import time as _t
        from selfcall.engine import LimitHit
        seen = []

        def base(d):
            seen.append(os.environ.get("CLAUDE_CONFIG_DIR", "default"))
            if len(seen) == 1:
                raise LimitHit("account down: Your organization has disabled Claude subscription access for Claude Code")
            return {"answer": {"status": "done"}, "cost_usd": 0.0, "session_id": "x", "denials": []}
        run = D.failover_invoker(base, store_factory=lambda: self.store)
        self.assertEqual(run({"kind": "live"})["answer"]["status"], "done")
        self.store.reload()
        self.assertGreater(self.store.get("main")["exhausted_until"], _t.time() + 5 * 3600)

    def test_when_every_account_waits_the_engine_gets_a_limit_with_a_reset_time(self):
        from selfcall.engine import LimitHit, parse_reset_wait

        def base(d):
            raise LimitHit("You've hit your session limit · resets 4:50pm (Europe/Madrid)")
        run = D.failover_invoker(base, store_factory=lambda: self.store)
        with self.assertRaises(LimitHit) as ctx:
            run({"kind": "live"})
        self.assertIsNotNone(parse_reset_wait(str(ctx.exception)))

    def test_it_stays_on_the_account_it_moved_to_after_the_first_one_recovers(self):
        from selfcall.engine import LimitHit
        seen = []

        def base(d):
            seen.append(os.environ.get("CLAUDE_CONFIG_DIR", "default"))
            if len(seen) == 1:
                raise LimitHit("You've hit your session limit · resets 4:50pm (Europe/Madrid)")
            return "ok"
        run = D.failover_invoker(base, store_factory=lambda: self.store, scope="training")
        run({"kind": "x"})
        self.store.clear_exhausted("main")   # main's limit lifts
        run({"kind": "x"})
        self.assertEqual(seen, ["default", str(self.tmp / "claude-b"), str(self.tmp / "claude-b")])

    def test_when_both_wait_the_one_back_first_is_used_next_and_kept(self):
        import time as _t
        from selfcall.engine import LimitHit
        self.store.set_active("second")
        self.store.mark_exhausted("main", _t.time() + 4 * 3600)
        self.store.mark_exhausted("second", _t.time() - 1)  # second's 1 h has run out
        seen = []
        run = D.failover_invoker(lambda d: seen.append(os.environ.get("CLAUDE_CONFIG_DIR", "default")) or "ok",
                                 store_factory=lambda: self.store, scope="training")
        run({"kind": "x"})
        self.assertEqual(seen, [str(self.tmp / "claude-b")])
        self.assertEqual(self.store.get_active(), "second")

    def test_autoswitch_off_for_the_scope_is_a_pass_through(self):
        from selfcall.engine import LimitHit
        self.store.set_auto("training", False)

        def base(d):
            raise LimitHit("You've hit your session limit · resets 4:50pm (Europe/Madrid)")
        run = D.failover_invoker(base, store_factory=lambda: self.store, scope="training")
        with self.assertRaises(LimitHit):
            run({"kind": "x"})
        self.store.reload()
        self.assertFalse(self.store.is_exhausted("main"))  # untouched: training never switched
        self.assertFalse(self.store.is_exhausted("second"))

    def test_training_engine_goes_through_the_failover(self):
        eng = D.make_engine()
        self.assertEqual(eng.invoker.__qualname__, "failover_invoker.<locals>.run")

    def test_with_one_account_registered_it_is_a_pass_through(self):
        calls = []
        run = D.failover_invoker(lambda d: calls.append(d) or "ok", store_factory=lambda: None)
        self.assertEqual(run({"kind": "live"}), "ok")
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
