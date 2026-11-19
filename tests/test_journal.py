"""Independent tests for the public, standalone evidence-journal demo."""
import json
import multiprocessing as mp
import tempfile
import unittest
from pathlib import Path

from agent_reliability.journal import IntegrityError, Journal, OutcomeUnknown


def writer(root: str, slot: int, start, queue):
    j = Journal(Path(root))
    start.wait()
    for index in range(6):
        j.append(operation=f"worker_{slot}_{index}", event="intent", payload={"input_digest": str(slot * 6 + index)})
    queue.put(slot)


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.j = Journal(self.root)

    def test_restart_reads_verified_state(self):
        data = b"request"
        self.assertEqual(self.j.begin("op1", data)["status"], "admitted")
        self.j.complete("op1", data, b"finished")
        result = Journal(self.root).begin("op1", data)
        self.assertEqual(result["status"], "completed")
        self.assertFalse(result["invoked"])
        self.assertEqual(len(Journal(self.root).inspect()), 2)

    def test_uncertain_intent_refuses_replay(self):
        self.j.begin("op2", b"same")
        with self.assertRaises(OutcomeUnknown):
            Journal(self.root).begin("op2", b"same")

    def test_changed_request_is_rejected(self):
        self.j.begin("op3", b"original")
        with self.assertRaises(IntegrityError):
            self.j.begin("op3", b"new")

    def test_conflicting_result_is_refused(self):
        self.j.begin("op4", b"input")
        self.j.complete("op4", b"input", b"a")
        with self.assertRaises(IntegrityError):
            self.j.complete("op4", b"input", b"b")

    def test_identical_result_is_idempotent(self):
        self.j.begin("op5", b"input")
        self.j.complete("op5", b"input", b"a")
        again = self.j.complete("op5", b"input", b"a")
        self.assertTrue(again["duplicate"])
        self.assertEqual(len(self.j.inspect()), 2)

    def test_result_without_intent_refused(self):
        with self.assertRaises(IntegrityError):
            self.j.complete("op6", b"data", b"result")

    def test_torn_tail_detected_and_blocks_append(self):
        self.j.begin("op7", b"input")
        with self.j.path.open("ab") as file:
            file.write(b'{"partial":')
        with self.assertRaises(IntegrityError):
            self.j.append(operation="op8", event="intent", payload={"input_digest": "x"})

    def test_modified_record_detected(self):
        self.j.begin("op8", b"data")
        data = json.loads(self.j.path.read_text())
        data["payload"]["input_digest"] = "forged"
        self.j.path.write_text(json.dumps(data) + "\n")
        with self.assertRaises(IntegrityError):
            self.j.inspect()

    def test_missing_record_breaks_sequence(self):
        self.j.begin("one", b"a")
        self.j.begin("two", b"b")
        lines = self.j.path.read_bytes().splitlines()
        self.j.path.write_bytes(lines[1] + b"\n")
        with self.assertRaises(IntegrityError):
            self.j.inspect()

    def test_multiple_processes_keep_one_chain(self):
        ctx = mp.get_context("fork")
        start = ctx.Event()
        q = ctx.Queue()
        processes = [ctx.Process(target=writer, args=(str(self.root), i, start, q)) for i in range(5)]
        for p in processes:
            p.start()
        start.set()
        for p in processes:
            p.join(10)
            self.assertEqual(p.exitcode, 0)
        self.assertEqual(sorted(q.get(timeout=5) for _ in processes), list(range(5)))
        events = Journal(self.root).inspect()
        self.assertEqual(len(events), 30)
        self.assertEqual([e["seq"] for e in events], list(range(1, 31)))

    def test_invalid_operation_identity_refused(self):
        with self.assertRaises(ValueError):
            self.j.begin("../sneaky", b"x")


if __name__ == "__main__":
    unittest.main()
