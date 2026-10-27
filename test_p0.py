import os
import sqlite3
import tempfile
import unittest

from chapter_state import (
    classify_failure, is_auto_retryable, is_infrastructure_failure,
    is_recoverable, is_transient_infrastructure_failure,
)
from chapter_tasks import ChapterTaskStore
from state_store import StateStore


class P0StateAndAuditTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.path)
        self.store = StateStore(self.path)

    def tearDown(self):
        self.store.close()
        if os.path.exists(self.path):
            os.unlink(self.path)

    def test_chapter_state_and_failure_type_are_preserved(self):
        self.store.upsert_chapter_task("r", "s", 1, "pending")
        self.store.upsert_chapter_task("r", "s", 1, "writing", attempt=1)
        self.store.upsert_chapter_task("r", "s", 1, "retrying", attempt=2,
                                       failure_type="word_count_failure")
        row = self.store.chapter_tasks("r")[0]
        self.assertEqual(row["status"], "retrying")
        self.assertEqual(row["attempt"], 2)
        self.assertEqual(row["failure_type"], "word_count_failure")

    def test_completed_chapter_clears_transient_retry_failure(self):
        self.store.upsert_chapter_task(
            "r", "s", 1, "retrying", attempt=1, error="HTTP 504",
            failure_type="network_error", warning="retrying",
        )
        self.store.upsert_chapter_task(
            "r", "s", 1, "completed", attempt=2, quality=4,
            words=2500, output_path="ch0001.md", error=None,
            failure_type=None, warning=None,
        )
        row = self.store.chapter_tasks("r")[0]
        self.assertEqual(row["status"], "completed")
        self.assertIsNone(row["error"])
        self.assertIsNone(row["failure_type"])
        self.assertIsNone(row["warning"])

    def test_new_writing_attempt_clears_a_previous_terminal_error(self):
        self.store.upsert_chapter_task(
            "r", "s", 1, "retry_wait", attempt=1, error="HTTP 504",
            failure_type="network_error",
        )
        self.store.upsert_chapter_task("r", "s", 1, "writing", attempt=2)
        row = self.store.chapter_tasks("r")[0]
        self.assertEqual(row["status"], "writing")
        self.assertIsNone(row["error"])

    def test_llm_audit_fields(self):
        self.store.record_llm_call("r", "s", "writer", "m", "relay", "error",
                                   error="HTTP 504", chapter_no=2, attempt=3,
                                   purpose="chapter_generation", retry_index=2)
        row = self.store.db.execute("SELECT * FROM llm_calls").fetchone()
        self.assertEqual(row["attempt"], 3)
        self.assertEqual(row["purpose"], "chapter_generation")
        self.assertEqual(row["retry_index"], 2)

    def test_failure_taxonomy(self):
        self.assertEqual(classify_failure("HTTP 503"), "network_error")
        self.assertEqual(classify_failure("HTTP 502: upstream access forbidden"), "route_forbidden")
        self.assertEqual(classify_failure("HTTP 404: model_not_found"), "model_not_found")
        self.assertEqual(classify_failure("HTTP 404: 404 page not found"), "network_error")
        self.assertEqual(classify_failure("HTTP 504: Gateway Time-out"), "gateway_timeout")
        self.assertEqual(
            classify_failure("StreamInterruptedError: stream ended without a terminal event"),
            "network_error",
        )
        self.assertEqual(classify_failure("URLError: SSL UNEXPECTED_EOF_WHILE_READING"), "proxy_tls_error")
        self.assertEqual(classify_failure("TimeoutError: The read operation timed out"), "upstream_timeout")
        self.assertEqual(classify_failure(content=""), "empty_output")
        self.assertEqual(classify_failure(content="text", word_count=10, word_min=20, word_max=30), "word_count_failure")
        self.assertEqual(classify_failure(content="text", quality=2, quality_min=4), "quality_failure")
        self.assertEqual(classify_failure(file_error=True), "file_failure")
        self.assertTrue(is_recoverable("network_error"))
        self.assertFalse(is_recoverable("file_failure"))
        self.assertTrue(is_auto_retryable("network_error"))
        self.assertTrue(is_auto_retryable("gateway_timeout"))
        self.assertTrue(is_auto_retryable(classify_failure("StreamInterruptedError: stream ended without a terminal event")))
        self.assertFalse(is_auto_retryable("route_forbidden"))
        self.assertFalse(is_auto_retryable("model_error"))
        self.assertFalse(is_auto_retryable("word_count_failure"))
        self.assertFalse(is_auto_retryable("quality_failure"))
        self.assertTrue(is_infrastructure_failure("proxy_tls_error"))
        self.assertTrue(is_infrastructure_failure("acceptance_infrastructure_failure"))
        self.assertTrue(is_transient_infrastructure_failure("gateway_timeout"))
        self.assertFalse(is_transient_infrastructure_failure("upstream_timeout"))

    def test_task_facade_keeps_sql_out_of_callers(self):
        tasks = ChapterTaskStore(self.store, "r", "s")
        tasks.update(1, "pending")
        self.assertEqual(tasks.list()[0]["status"], "pending")

    def test_cycle_recommendation_query_ignores_older_pending_items(self):
        self.store.add_recommendation({"genre": "old", "concept": "old high confidence", "confidence": 0.99})
        boundary = self.store.latest_recommendation_id()
        self.store.add_recommendation({"genre": "new-low", "concept": "new low", "confidence": 0.7})
        self.store.add_recommendation({"genre": "new-top", "concept": "new top", "confidence": 0.9})

        selected = self.store.get_top_recommendation_after(boundary)

        self.assertEqual(selected["genre"], "new-top")


if __name__ == "__main__":
    unittest.main()
