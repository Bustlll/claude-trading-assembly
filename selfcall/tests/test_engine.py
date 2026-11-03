"""Tests for the self-calling engine. They run the real call_claude path, but the
`claude` binary is tests/fake_claude.py, so no tokens are spent."""
import json
import sys
import time
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from selfcall import engine as E  # noqa: E402

FAKE = [sys.executable, str(ROOT / "tests" / "fake_claude.py")]


class EngineTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.log = self.home / "argv.log"
        import os
        os.environ["SELFCALL_FAKE_LOG"] = str(self.log)
        self.eng = E.Engine(home=self.home, claude_cmd=FAKE)

    def tearDown(self):
        self._tmp.cleanup()

    def engine_with(self, **limits):
        self.eng = E.Engine(home=self.home, claude_cmd=FAKE, limits=limits)
        return self.eng

    def rows(self):
        return self.eng.store.db.execute("SELECT * FROM demands ORDER BY created").fetchall()

    def record(self, did):
        return json.loads((self.home / "runs" / f"{did}.json").read_text())

    # --- the self-call itself ------------------------------------------
    def test_answer_spawns_child_which_runs_with_parent_context(self):
        root = self.eng.enqueue("research", "SPAWN find the things")
        what, _ = self.eng.drain()
        self.assertEqual(what, "idle")
        rows = {r["id"]: r for r in self.rows()}
        self.assertEqual(len(rows), 2)
        child = [r for r in rows.values() if r["parent"] == root][0]
        self.assertEqual((child["kind"], child["depth"], child["status"]), ("reflect", 1, "done"))
        self.assertIn("Parent summary", child["context"])
        self.assertEqual(self.record(root)["links"]["spawned"], [child["id"]])
        self.assertTrue((self.home / "runs" / f"{root}.md").exists())

    def test_tools_and_budget_come_from_kind_not_model(self):
        self.eng.enqueue("reflect", "plain reflection")
        self.eng.enqueue("research", "plain research")
        self.eng.drain()
        argv = [json.loads(l) for l in self.log.read_text().splitlines()]
        self.assertTrue(all("--strict-mcp-config" in a for a in argv))   # no claude.ai connectors (2026-10-18)
        reflect = [a for a in argv if "haiku" in a][0]
        research = [a for a in argv if "sonnet" in a and "WebSearch,WebFetch" in a][0]
        self.assertEqual(reflect[reflect.index("--tools") + 1], "")
        self.assertNotIn("--allowedTools", reflect)
        self.assertEqual(research[research.index("--allowedTools") + 1], "WebSearch,WebFetch")
        self.assertEqual(research[research.index("--permission-mode") + 1], "dontAsk")
        self.assertIn("--json-schema", research)

    def test_child_view_is_attached_to_the_childs_context_not_its_prompt(self):
        # 2026-10-18: the host attaches a view at spawn time (ftmo_blank: a symbol trader's symbol file); the
        # child's prompt -- and so its dedupe hash -- stays exactly what the parent wrote
        seen = []
        self.eng.child_view = lambda kind, prompt, parent: seen.append(kind) or f"VIEW for {kind}"
        parent = self.eng.enqueue("research", "parent")
        row = self.eng.store.get(parent)
        self.eng._spawn(row, [{"kind": "research", "prompt": "child task"}])
        child = [r for r in self.eng.store.db.execute("SELECT * FROM demands WHERE parent=?", (parent,))][0]
        self.assertEqual(child["prompt"], "child task")
        self.assertIn("VIEW for research", child["context"])
        self.eng.child_view = lambda kind, prompt, parent: 1 / 0     # a broken view never blocks the child
        self.eng._spawn(row, [{"kind": "research", "prompt": "another task"}])
        child2 = [r for r in self.eng.store.db.execute("SELECT * FROM demands WHERE prompt='another task'")][0]
        self.assertIn("could not build", child2["context"])

    def test_kind_not_in_may_spawn_is_rejected(self):
        root = self.eng.enqueue("work", "SPAWNWORK BADKIND")
        self.eng.drain()
        links = self.record(root)["links"]
        self.assertEqual(len(links["spawned"]), 1)  # the allowed work child
        self.assertEqual(links["rejected"][0]["why"], "work may not spawn research")

    def test_depth_limit_rejects_children(self):
        self.engine_with(max_depth=0)
        root = self.eng.enqueue("research", "SPAWN deep")
        self.eng.drain()
        self.assertEqual(self.record(root)["links"]["rejected"][0]["why"], "max_depth")
        self.assertEqual(len(self.rows()), 1)

    def test_duplicate_child_is_not_run_twice(self):
        self.eng.enqueue("research", "SPAWN first")
        self.eng.enqueue("research", "SPAWN second")
        self.eng.drain()
        kids = [r for r in self.rows() if r["source"] == "child"]
        self.assertEqual(len(kids), 1)
        dup = [self.record(r["id"])["links"]["rejected"] for r in self.rows() if r["source"] != "child"]
        self.assertEqual([x["why"] for x in dup[1]], ["duplicate of an earlier demand"])

    def test_blocked_answer_spawns_nothing(self):
        root = self.eng.enqueue("research", "BLOCK SPAWN can't do it")
        self.eng.drain()
        self.assertEqual(self.record(root)["status"], "blocked")
        self.assertEqual(len(self.rows()), 1)

    # --- failures are recorded, never dropped ---------------------------
    def test_error_envelope_is_recorded_as_failed(self):
        root = self.eng.enqueue("research", "FAILENV")
        self.eng.drain()
        rec = self.record(root)
        self.assertEqual(rec["status"], "failed")
        self.assertIn("error_max_budget_usd", rec["error"])

    def test_non_json_output_is_recorded_as_failed(self):
        root = self.eng.enqueue("work", "NOJSON")
        self.eng.drain()
        self.assertEqual(self.record(root)["status"], "failed")
        self.assertIn("no JSON envelope", self.record(root)["error"])

    def test_interrupted_running_demand_is_recovered(self):
        did = self.eng.enqueue("research", "orphan")
        self.eng.store.set(did, status="running", started=1.0)
        self.eng.drain()
        self.assertEqual(self.eng.store.get(did)["status"], "failed")

    # --- hard limits ------------------------------------------------------
    def test_stop_file_halts_before_any_call(self):
        self.eng.enqueue("research", "should not run")
        self.eng.stop_file.write_text("stop\n")
        self.assertEqual(self.eng.drain()[0], "halted")
        self.assertFalse(self.log.exists())
        self.eng.stop_file.unlink()
        self.assertEqual(self.eng.drain()[0], "idle")
        self.assertTrue(self.log.exists())

    def test_max_runs_halts(self):
        self.engine_with(max_runs=1)
        self.eng.enqueue("reflect", "one")
        self.eng.enqueue("reflect", "two")
        self.assertEqual(self.eng.drain()[0], "halted")
        self.assertEqual(self.eng.store.totals()[0], 1)

    def test_cost_cap_halts(self):
        self.engine_with(max_total_cost_usd=0.015)  # each fake call costs 0.01
        for i in range(3):
            self.eng.enqueue("reflect", f"r{i}")
        what, detail = self.eng.drain()
        self.assertEqual(what, "halted")
        self.assertIn("cost cap", detail)
        self.assertEqual(self.eng.store.totals()[0], 2)

    # --- admission and events --------------------------------------------
    def test_unknown_kind_is_refused(self):
        with self.assertRaises(ValueError):
            self.eng.enqueue("write_files", "nope")

    def test_event_becomes_demand_of_its_kind(self):
        did = self.eng.emit("trade_closed", {"id": 1, "r": -0.5})
        row = self.eng.store.get(did)
        self.assertEqual(row["kind"], "work")
        self.assertEqual(row["source"], "event:trade_closed")
        self.assertIn('"id": 1', row["prompt"])


class ModelEffortTest(unittest.TestCase):
    def kinds(self, model, effort):
        return {"k": {"tools": [], "model": model, "effort": effort, "budget_usd": 0.1, "may_spawn": []}}

    def test_opus_at_max_is_refused_at_start(self):
        with tempfile.TemporaryDirectory() as h:
            with self.assertRaises(ValueError) as cm:
                E.Engine(home=h, claude_cmd=FAKE, kinds=self.kinds("opus", "max"))
            self.assertIn("opus at max", str(cm.exception))

    def test_full_opus_name_at_max_is_refused_too(self):
        with self.assertRaises(ValueError):
            E.policy_check("k", {"model": "claude-opus-5-5", "effort": "max"})

    def test_opus_below_max_is_allowed(self):
        E.policy_check("k", {"model": "opus", "effort": "xhigh"})

    def test_other_models_at_max_are_allowed(self):
        E.policy_check("k", {"model": "sonnet", "effort": "max"})
        E.policy_check("k", {"model": "haiku", "effort": "max"})

    def test_unknown_effort_is_refused(self):
        with self.assertRaises(ValueError):
            E.policy_check("k", {"model": "sonnet", "effort": "ultra"})

    def test_call_rechecks_policy_even_if_table_changed_later(self):
        pol = {"tools": [], "model": "opus", "effort": "max", "budget_usd": 0.1, "may_spawn": []}
        with self.assertRaises(ValueError):
            E.call_claude({"kind": "k", "prompt": "x", "context": ""}, pol, FAKE, Path(tempfile.gettempdir()),
                          dict(E.LIMITS))

    def test_each_kind_sends_its_own_model_and_effort(self):
        with tempfile.TemporaryDirectory() as h:
            log = Path(h) / "argv.log"
            import os
            os.environ["SELFCALL_FAKE_LOG"] = str(log)
            kinds = {
                "deep": {"tools": [], "model": "sonnet", "effort": "max", "budget_usd": 0.1, "may_spawn": ["reflect"]},
                "reflect": {"tools": [], "model": "haiku", "effort": "max", "budget_usd": 0.1, "may_spawn": []},
            }
            eng = E.Engine(home=h, claude_cmd=FAKE, kinds=kinds)
            eng.enqueue("deep", "SPAWN a quick check")
            eng.drain()
            argv = [json.loads(line) for line in log.read_text().splitlines()]
            models = [a[a.index("--model") + 1] for a in argv]
            efforts = [a[a.index("--effort") + 1] for a in argv]
            self.assertEqual(models, ["sonnet", "haiku"])
            self.assertEqual(efforts, ["max", "max"])


class BillingGuardTest(unittest.TestCase):
    def test_api_key_in_env_refuses_the_call(self):
        import os
        old = dict(os.environ)
        try:
            os.environ["ANTHROPIC_API_KEY"] = "test-not-real"
            os.environ.pop("SELFCALL_ALLOW_API_BILLING", None)
            with self.assertRaises(RuntimeError) as cm:
                E.billing_guard()
            self.assertIn("would bill the API", str(cm.exception))
            os.environ["SELFCALL_ALLOW_API_BILLING"] = "1"
            E.billing_guard()  # explicit opt-in passes
        finally:
            os.environ.clear()
            os.environ.update(old)

    def test_subscription_env_passes(self):
        import os
        old = dict(os.environ)
        try:
            for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "SELFCALL_ALLOW_API_BILLING"):
                os.environ.pop(k, None)
            E.billing_guard()
        finally:
            os.environ.clear()
            os.environ.update(old)


class LimitTest(unittest.TestCase):
    def setUp(self):
        import os
        os.environ.pop("SELFCALL_FAKE_LOG", None)  # an earlier test may leave a deleted log path behind
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_limit_waits_and_is_not_counted_as_a_run(self):
        import time
        eng = E.Engine(home=self.home, claude_cmd=FAKE)
        did = eng.enqueue("reflect", "LIMIT please")
        what, when = eng.step()
        self.assertEqual(what, "limited")
        self.assertGreater(when, time.time())
        row = eng.store.get(did)
        self.assertEqual(row["status"], "queued")
        self.assertIsNone(row["started"])
        self.assertIn("usage limit", row["error"].lower())
        self.assertEqual(eng.store.totals()[0], 0)          # not counted as an invocation
        self.assertEqual(eng.store.next_queued(), None)    # not runnable until its retry time

    def test_an_account_that_is_down_waits_like_a_limit_instead_of_failing(self):
        # 2026-11-02: a switched-off subscription must not fail the demand; it waits (or, with failover, moves account)
        eng = E.Engine(home=self.home, claude_cmd=FAKE)
        did = eng.enqueue("reflect", "ACCOUNTDOWN please")
        what, _ = eng.step()
        self.assertEqual(what, "limited")
        row = eng.store.get(did)
        self.assertEqual(row["status"], "queued")
        self.assertIn("account down", row["error"].lower())

    def test_drain_returns_limited_and_backoff_grows(self):
        eng = E.Engine(home=self.home, claude_cmd=FAKE)
        eng.enqueue("reflect", "LIMIT again")
        first = eng.drain()
        self.assertEqual(first[0], "limited")
        eng.store.set(eng.store.next_queued() or eng.store.db.execute("SELECT id FROM demands").fetchone()["id"],
                      next_try=0)
        eng.limit_streak = 0
        what, t1 = eng.step()
        self.assertEqual(what, "limited")
        self.assertGreaterEqual(eng.limit_streak, 1)

    def test_a_normal_answer_resets_the_streak(self):
        eng = E.Engine(home=self.home, claude_cmd=FAKE)
        eng.limit_streak = 3
        eng.enqueue("reflect", "a normal question")
        eng.drain()
        self.assertEqual(eng.limit_streak, 0)


class ParseResetWaitTest(unittest.TestCase):
    def test_parses_the_exact_production_message(self):
        import datetime as dt
        from zoneinfo import ZoneInfo
        now = dt.datetime(2026, 10, 7, 2, 0, tzinfo=ZoneInfo("Europe/Madrid"))
        w = E.parse_reset_wait("You've hit your session limit · resets 6:50am (Europe/Madrid)", now=now)
        self.assertAlmostEqual(w, 4 * 3600 + 50 * 60, delta=1)

    def test_rolls_over_to_the_next_day_when_the_time_already_passed(self):
        import datetime as dt
        from zoneinfo import ZoneInfo
        now = dt.datetime(2026, 10, 7, 8, 0, tzinfo=ZoneInfo("UTC"))
        w = E.parse_reset_wait("resets 6:50am (UTC)", now=now)
        self.assertAlmostEqual(w, 22 * 3600 + 50 * 60, delta=1)

    def test_unknown_timezone_returns_none_not_a_guess(self):
        self.assertIsNone(E.parse_reset_wait("resets 6:50am (Mars/Colony)"))

    def test_an_on_the_hour_reset_without_minutes_is_parsed(self):
        # 2026-10-18: "resets 6pm (Europe/Madrid)" did not match (the pattern required ":MM"), the limit fell back
        # to a guessed backoff, and training resumed at 18:40 instead of 18:00
        import datetime as dt
        from zoneinfo import ZoneInfo
        now = dt.datetime(2026, 10, 18, 17, 57, tzinfo=ZoneInfo("Europe/Madrid"))
        w = E.parse_reset_wait("You've hit your session limit · resets 6pm (Europe/Madrid)", now=now)
        self.assertAlmostEqual(w, 3 * 60, delta=1)
        w = E.parse_reset_wait("resets Thursday 10pm (Europe/Madrid)", now=now)
        self.assertGreater(w, 3 * 24 * 3600)

    def test_no_reset_time_in_message_returns_none(self):
        self.assertIsNone(E.parse_reset_wait("usage limit reached, try again later"))

    def test_weekly_reset_with_a_day_name_finds_the_next_occurrence(self):
        import datetime as dt
        from zoneinfo import ZoneInfo
        now = dt.datetime(2026, 10, 7, 12, 0, tzinfo=ZoneInfo("Europe/Madrid"))  # a Wednesday
        w = E.parse_reset_wait("This week: resets Thursday 10:00 PM (Europe/Madrid)", now=now)
        self.assertAlmostEqual(w, (24 + 10) * 3600, delta=1)

    def test_weekly_reset_wraps_to_next_week_when_the_day_already_passed(self):
        import datetime as dt
        from zoneinfo import ZoneInfo
        now = dt.datetime(2026, 10, 9, 23, 0, tzinfo=ZoneInfo("Europe/Madrid"))  # Friday, after Thursday 10pm
        w = E.parse_reset_wait("resets Thursday 10:00pm (Europe/Madrid)", now=now)
        self.assertAlmostEqual(w, 6 * 24 * 3600 - 1 * 3600, delta=1)

    def test_an_unrecognised_day_name_returns_none(self):
        self.assertIsNone(E.parse_reset_wait("resets Someday 10:00pm (Europe/Madrid)"))


class ParsedResetInStepTest(unittest.TestCase):
    def setUp(self):
        import os
        os.environ.pop("SELFCALL_FAKE_LOG", None)
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_stated_reset_time_is_used_instead_of_guessed_backoff(self):
        eng = E.Engine(home=self.home, claude_cmd=FAKE)
        eng.enqueue("reflect", "SESSIONLIMIT please")
        expected = E.parse_reset_wait("resets 11:59pm (UTC)") + E.PARSED_RESET_BUFFER_S
        what, when = eng.step()
        self.assertEqual(what, "limited")
        self.assertAlmostEqual(when, time.time() + expected, delta=5)
        row = eng.store.get(eng.store.db.execute("SELECT id FROM demands").fetchone()["id"])
        self.assertIn("stated reset", row["error"])


if __name__ == "__main__":
    unittest.main()
