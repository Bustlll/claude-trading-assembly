"""CODE GATE (driver.py code_gate, 2026-10-18): after an evolve, a failing self-test gets ONE repair; still
failing after it -> the pre-evolve backups come back. Runs on a temp home; never touches state/ or the board."""
import json
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOME))
import driver as D  # noqa: E402


class FakeEngine:
    def __init__(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.execute("CREATE TABLE demands (id TEXT, kind TEXT, status TEXT, started REAL, finished REAL, answer TEXT)")
        self.store = SimpleNamespace(db=db)
        self.enqueued = []

    def add(self, did, kind, status, started, finished, answer="{}"):
        self.store.db.execute("INSERT INTO demands VALUES (?,?,?,?,?,?)", (did, kind, status, started, finished, answer))

    def enqueue(self, kind, prompt, source=""):
        self.enqueued.append((kind, prompt))
        did = f"r{len(self.enqueued)}"
        self.add(did, kind, "queued", None, None)
        return did


class CodeGateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "agent_code" / "versions").mkdir(parents=True)
        (self.tmp / "state").mkdir()
        self.code = self.tmp / "agent_code" / "routing.py"
        self.code.write_text("GOOD = 1\n")
        self.notes = []
        self.patches = [mock.patch.object(D, "HOME", self.tmp),
                        mock.patch.object(D, "CODE_GATE", self.tmp / "state" / "code_gate.json"),
                        mock.patch.object(D, "_gate_record", lambda paper, note: self.notes.append(note))]
        for p in self.patches:
            p.start()
        D.CODE_GATE.write_text(json.dumps({"checked_until": time.time() - 100}))
        self.eng = FakeEngine()
        t0 = time.time() - 50
        bak = self.tmp / "agent_code" / "versions" / "routing.py.20261018T030000.bak"
        bak.write_text("GOOD = 1\n")
        self.code.write_text("GOOD = 1\nBROKEN = 1\n")   # what the evolve wrote
        self.eng.add("ev1", "evolve", "done", t0, time.time(), json.dumps({"summary": "added a checklist"}))

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_green_self_test_does_nothing(self):
        with mock.patch.object(D, "_selftest", return_value=(True, "OK")):
            self.assertIsNone(D.code_gate(self.eng, None))
        self.assertEqual(self.eng.enqueued, [])

    def test_a_break_queues_one_repair_with_the_failures_and_the_diff(self):
        out = "FAIL: test_x (t.T)\n----\nAssertionError: boom\n\n----------------------------------------------------------------------"
        with mock.patch.object(D, "_selftest", return_value=(False, out)):
            res = D.code_gate(self.eng, None)
            self.assertEqual(res["gate"], "repair_queued")
            self.assertIsNone(D.code_gate(self.eng, None))     # repair still queued: wait, no second repair
        kind, prompt = self.eng.enqueued[0]
        self.assertEqual(kind, "repair")
        self.assertIn("added a checklist", prompt)
        self.assertIn("+BROKEN = 1", prompt)
        self.assertEqual(len(self.eng.enqueued), 1)

    def test_still_broken_after_the_repair_restores_the_pre_evolve_file(self):
        with mock.patch.object(D, "_selftest", return_value=(False, "FAIL")):
            D.code_gate(self.eng, None)
            self.eng.store.db.execute("UPDATE demands SET status='done' WHERE id='r1'")
            res = D.code_gate(self.eng, None)
        self.assertEqual(res["gate"], "closed")
        self.assertIn("REVERTED", res["note"])
        self.assertEqual(self.code.read_text(), "GOOD = 1\n")
        self.assertTrue(list((self.tmp / "agent_code" / "versions").glob("routing.py.*.broken.bak")))

    def test_a_good_repair_keeps_the_upgrade(self):
        with mock.patch.object(D, "_selftest", side_effect=[(False, "FAIL"), (True, "OK")]):
            D.code_gate(self.eng, None)
            self.eng.store.db.execute("UPDATE demands SET status='done' WHERE id='r1'")
            res = D.code_gate(self.eng, None)
        self.assertIn("fixed", res["note"])
        self.assertIn("BROKEN = 1", self.code.read_text())   # the upgrade stays


if __name__ == "__main__":
    unittest.main()
