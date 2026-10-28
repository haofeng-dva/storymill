import time
import unittest
from unittest.mock import patch

import engine
from state_store import StateStore


class RunFinalizerTests(unittest.TestCase):
    def setUp(self):
        self.store = StateStore(":memory:")

    def tearDown(self):
        self.store.db.close()

    def _finalizer(self, run_id="test-run"):
        finalizer = engine.RunFinalizer(self.store, run_id, "test-story", time.time())
        finalizer.start_run(run_id, "test-story")
        return finalizer

    def _row(self, run_id="test-run"):
        return self.store.db.execute(
            "SELECT status, elapsed_s, error FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()

    def test_main_finally_persists_declared_outcome_once(self):
        def run_body():
            finalizer = self._finalizer()
            finalizer.set_outcome("completed")
            engine._ACTIVE_RUN_FINALIZER.set(finalizer)
            self.assertEqual(self._row()["status"], "running")
            return 0

        with patch.object(engine, "_run_main", side_effect=run_body):
            self.assertEqual(engine.main(), 0)

        row = self._row()
        self.assertEqual(row["status"], "completed")
        self.assertIsNotNone(row["elapsed_s"])
        self.assertIsNone(row["error"])

    def test_main_finally_marks_unhandled_exception_failed(self):
        def run_body():
            finalizer = self._finalizer()
            engine._ACTIVE_RUN_FINALIZER.set(finalizer)
            raise RuntimeError("unexpected failure")

        with patch.object(engine, "_run_main", side_effect=run_body):
            with self.assertRaisesRegex(RuntimeError, "unexpected failure"):
                engine.main()

        row = self._row()
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error"], "unexpected failure")

    def test_main_finally_marks_nonzero_system_exit_failed(self):
        def run_body():
            finalizer = self._finalizer()
            engine._ACTIVE_RUN_FINALIZER.set(finalizer)
            raise SystemExit(1)

        with patch.object(engine, "_run_main", side_effect=run_body):
            with self.assertRaises(SystemExit):
                engine.main()

        row = self._row()
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error"], "1")

    def test_main_finally_marks_unclassified_normal_exit_aborted(self):
        def run_body():
            finalizer = self._finalizer()
            engine._ACTIVE_RUN_FINALIZER.set(finalizer)
            return 2

        with patch.object(engine, "_run_main", side_effect=run_body):
            self.assertEqual(engine.main(), 2)

        row = self._row()
        self.assertEqual(row["status"], "aborted")
        self.assertEqual(row["error"], "run exited without recording a terminal status")


if __name__ == "__main__":
    unittest.main()
