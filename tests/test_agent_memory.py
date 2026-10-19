import os
from pathlib import Path
import tempfile
import unittest

from agent.memory import Memory
from agent.models import AmbiguousOperation, BudgetExceeded


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "agent.sqlite3"
        self.memory = Memory(self.path)

    def tearDown(self):
        self.memory.close()
        self.temp.cleanup()

    def test_confirmed_operation_is_reused_after_restart(self):
        calls = []
        self.assertEqual(self.memory.operation("request1", "SEND", {"text": "hello"},
            lambda: calls.append(1) or {"message_id": "verified"}), {"message_id": "verified"})
        self.memory.close()
        self.memory = Memory(self.path)
        value = self.memory.operation("request1", "SEND", {"text": "hello"},
            lambda: calls.append(2))
        self.assertEqual(value["message_id"], "verified")
        self.assertEqual(calls, [1])

    def test_interrupted_send_is_not_replayed(self):
        def interrupted():
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.memory.operation("request1", "SEND", {"text": "hello"}, interrupted)
        with self.assertRaises(AmbiguousOperation):
            self.memory.operation("request1", "SEND", {"text": "hello"}, lambda: self.fail("replayed"))
        self.assertEqual(self.memory.counts()["operations"], {"UNKNOWN": 1})

    def test_process_death_checkpoint_is_ambiguous(self):
        from agent.memory import canonical
        import hashlib
        digest = hashlib.sha256(canonical({"x": 1}).encode()).hexdigest()
        self.memory.db.execute("INSERT INTO operations VALUES('k','SEND',?,'STARTED',NULL,1)", (digest,))
        with self.assertRaises(AmbiguousOperation):
            self.memory.operation("k", "SEND", {"x": 1}, lambda: self.fail("replayed"))

    def test_receipt_reconciliation_cannot_reset_or_replay(self):
        with self.assertRaises(RuntimeError):
            self.memory.operation("request1", "SEND", {}, lambda: (_ for _ in ()).throw(RuntimeError()))
        with self.assertRaises(AmbiguousOperation):
            self.memory.reconcile("request1", "SEND", {}, {"id": "m1"})
        self.memory.reconcile("request1", "SEND", {}, {"id": "m1"}, receipt_verified=True)
        self.assertEqual(self.memory.operation("request1", "SEND", {}, lambda: self.fail()), {"id": "m1"})

    def test_operation_key_cannot_represent_different_request(self):
        self.memory.operation("key", "SEND", {"x": 1}, lambda: {})
        with self.assertRaisesRegex(ValueError, "REUSE_MISMATCH"):
            self.memory.operation("key", "SEND", {"x": 2}, lambda: {})

    def test_budget_is_persistent_and_reservation_survives_failure(self):
        limits = dict(max_requests=1, max_input_tokens=100, max_output_tokens=100)
        self.memory.reserve_budget("2026-10-19", 40, 30, **limits)
        with self.assertRaises(BudgetExceeded):
            self.memory.reserve_budget("2026-10-19", 1, 1, **limits)
        self.memory.close()
        self.memory = Memory(self.path)
        with self.assertRaises(BudgetExceeded):
            self.memory.reserve_budget("2026-10-19", 1, 1, **limits)
        self.assertEqual(self.memory.reserve_budget("2026-10-20", 2, 3, **limits), (1, 2, 3))

    def test_history_is_bounded(self):
        for i in range(20):
            self.memory.append("c", "assistant", "m" * 100, i)
        self.assertEqual(len(self.memory.history("c", max_chars=350)), 3)

    def test_database_symlink_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "agent.sqlite3"
            path.symlink_to(self.path)
            with self.assertRaises(OSError):
                Memory(path)

    def test_shared_data_directory_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            os.chmod(root, 0o755)
            with self.assertRaisesRegex(ValueError, "DIRECTORY_UNSAFE"):
                Memory(Path(root) / "agent.sqlite3")


if __name__ == "__main__":
    unittest.main()
