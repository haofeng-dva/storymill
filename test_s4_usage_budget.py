import contextlib
import io
import os
import sys
import unittest
from unittest.mock import patch

import orchestrator
from engine import start_writing_run
from state_store import StateStore, check_token_budget, today


class S4UsageBudgetTests(unittest.TestCase):
    def test_successful_llm_tokens_match_llm_calls_sum_and_runs_are_counted(self):
        db_path = os.path.join(os.path.dirname(__file__), f".test_s4_{os.getpid()}.db")
        try:
            store = StateStore(db_path)
            self.assertTrue(store.start_run("run-1", "story-1"))
            store.record_llm_call("run-1", "story-1", "writer", "m", "b", "completed", tokens=17)
            store.record_llm_call("run-1", "story-1", "writer", "m", "b", "error")
            row = store.db.execute(
                "SELECT tokens, llm_calls, runs FROM metrics WHERE day=?", (today(),)
            ).fetchone()
            sql_tokens = store.db.execute(
                "SELECT COALESCE(SUM(tokens), 0) FROM llm_calls WHERE created_at LIKE ?",
                (today() + "%",),
            ).fetchone()[0]
            self.assertEqual(row[0], sql_tokens)
            self.assertEqual(row[1:], (1, 1))
            store.close()
        finally:
            if "store" in locals():
                store.close()
            if os.path.exists(db_path):
                os.remove(db_path)

    def test_budget_gate_rejects_at_limit_with_both_numbers(self):
        class Store:
            def metrics_today(self):
                return {"stories": 0, "tokens": 12}

        ok, reason = orchestrator.check_limits(
            Store(), {"switches": {"new_short_enabled": True}, "limits": {"max_stories_per_day": 2, "daily_token_budget": 12}},
        )
        self.assertFalse(ok)
        self.assertIn("已用 12", reason)
        self.assertIn("上限 12", reason)

    def test_budget_gate_rejects_predicted_single_run_overage(self):
        class Store:
            def metrics_today(self):
                return {"tokens": 1_900_000}

        ok, reason = check_token_budget(Store(), 2_000_000, 500_000)
        self.assertFalse(ok)
        self.assertIn("预计 500000", reason)

    def test_engine_start_path_rejects_before_start_run(self):
        class Store:
            def __init__(self):
                self.closed = False
                self.started = False

            def metrics_today(self):
                return {"tokens": 1_900_000}

            def start_run(self, *_args):
                self.started = True

            def close(self):
                self.closed = True

        store = Store()
        result = start_writing_run(
            store, "run-1", "story-1", 0,
            {"daily_token_budget": 2_000_000, "approx_tokens_per_story": 500_000},
        )
        self.assertIsNone(result)
        self.assertFalse(store.started)

    def test_migration_removes_metrics_days_without_llm_calls(self):
        db_path = os.path.join(os.path.dirname(__file__), f".test_s4_orphan_{os.getpid()}.db")
        try:
            store = StateStore(db_path)
            store.db.execute("INSERT INTO metrics (day, tokens) VALUES ('2026-08-13', 500000)")
            store.db.commit()
            store.close()
            reopened = StateStore(db_path)
            self.assertIsNone(reopened.db.execute(
                "SELECT 1 FROM metrics WHERE day='2026-08-13'"
            ).fetchone())
            reopened.close()
        finally:
            if "reopened" in locals():
                reopened.close()
            if os.path.exists(db_path):
                os.remove(db_path)

    def test_package_failure_still_records_story(self):
        class Store:
            def __init__(self):
                self.recorded = 0

            def metrics_today(self):
                return {"stories": 0, "tokens": 0}

            def latest_recommendation_id(self):
                return 0

            def get_top_recommendation_after(self, _boundary):
                return {"id": 1}

            def record_story(self):
                self.recorded += 1

        cfg = {
            "limits": {"max_stories_per_day": 2, "daily_token_budget": 2_000_000},
            "switches": {"new_short_enabled": True, "auto_replace_enabled": False},
            "quality": {}, "radar": {"sources": "x", "list": 1, "recommend_count": 1, "portfolio": "weighted", "no_chapters": True},
            "final_editor": {"enabled": False},
        }
        store = Store()
        def step(name, *_args, **_kwargs):
            return (False, "package failed") if name == "package" else (True, "ok")
        with patch.object(orchestrator, "run_step", side_effect=step), patch.object(orchestrator.progress, "update"), \
             patch.object(orchestrator, "load_status", return_value={"status": "completed"}), \
             patch.object(orchestrator, "is_deliverable", return_value=True):
            result = orchestrator.run_cycle(store, cfg)
        self.assertEqual(result["status"], "package_failed")
        self.assertEqual(store.recorded, 1)

    def test_usage_report_matches_sql(self):
        db_path = os.path.join(os.path.dirname(__file__), f".test_s4_usage_{os.getpid()}.db")
        try:
            store = StateStore(db_path)
            store.start_run("run-1", "story-1")
            store.record_llm_call("run-1", "story-1", "writer", "m", "b", "completed", tokens=23)
            store.close()
            with patch.object(sys, "argv", ["usage.py"]), patch("tools.usage.DB_PATH", db_path):
                import tools.usage as usage
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    usage.main()
            self.assertIn("今日 tokens: 23", out.getvalue())
            self.assertIn("本月 tokens: 23", out.getvalue())
            self.assertIn("run-1", out.getvalue())
        finally:
            if "store" in locals():
                store.close()
            if os.path.exists(db_path):
                os.remove(db_path)


if __name__ == "__main__":
    unittest.main()
