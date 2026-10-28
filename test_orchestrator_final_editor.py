import copy
import os
import unittest
from unittest.mock import patch

import orchestrator


class FakeStore:
    def metrics_today(self):
        return {"stories": 0, "tokens": 0}

    def latest_recommendation_id(self):
        return 0


def config(enabled=False):
    return {
        "limits": {"max_stories_per_day": 2, "daily_token_budget": 2_000_000, "approx_tokens_per_story": 500_000},
        "switches": {"new_short_enabled": True, "serial_enabled": False, "auto_replace_enabled": False},
        "quality": {"quality_min": 4},
        "radar": {"sources": "amazon", "list": 1, "recommend_count": 1, "portfolio": "weighted", "no_chapters": True},
        "final_editor": {"enabled": enabled, "timeout_seconds": 900},
    }


class OrchestratorFinalEditorTests(unittest.TestCase):
    def _run(self, enabled, final_editor_ok=True):
        calls = []

        def step(name, cmd, dry_run=False, timeout=1800):
            calls.append({"name": name, "cmd": cmd, "timeout": timeout})
            if name == "ai-repair" and not final_editor_ok:
                return False, "candidate rejected"
            return True, "ok"

        with patch("orchestrator.run_step", side_effect=step), \
             patch("orchestrator.today", return_value="2026-09-18"), \
             patch("orchestrator.final_editor_run_id", return_value="final_test1"), \
             patch("orchestrator.progress.update"):
            result = orchestrator.run_cycle(FakeStore(), copy.deepcopy(config(enabled)), dry_run=True)
        return result, calls

    def test_disabled_keeps_existing_package_target_and_steps(self):
        result, calls = self._run(enabled=False)
        self.assertEqual([call["name"] for call in calls], ["radar", "adopt", "write", "verify", "package"])
        self.assertIn("--require-quality-gate", calls[3]["cmd"])
        self.assertIn("--fail-on-editorial-findings", calls[3]["cmd"])
        self.assertIn("--require-quality-gate", calls[-1]["cmd"])
        self.assertIn("story_20260918_0", calls[-1]["cmd"])
        self.assertEqual(calls[-1]["cmd"][0], orchestrator.sys.executable)
        self.assertEqual(result, {"status": "done", "story_id": "story_20260918_0", "verify": True})

    def test_sampled_verify_is_rejected_before_any_step_runs(self):
        cfg = config(enabled=False)
        cfg["quality"]["verify_sample"] = 1
        with patch("orchestrator.run_step") as step, patch("orchestrator.progress.update"):
            result = orchestrator.run_cycle(FakeStore(), cfg, dry_run=True)
        self.assertEqual(result["status"], "config_error")
        step.assert_not_called()

    def test_enabled_runs_after_verify_and_packages_only_accepted_candidate(self):
        result, calls = self._run(enabled=True)
        self.assertEqual([call["name"] for call in calls], ["radar", "adopt", "write", "verify", "ai-detect", "ai-repair", "package"])
        self.assertEqual(calls[4]["cmd"][1], "final_editor.py")
        self.assertEqual(calls[4]["cmd"][calls[4]["cmd"].index("--mode") + 1], "detect")
        self.assertEqual(calls[4]["cmd"][calls[4]["cmd"].index("--dossier-role") + 1], "final_editor")
        self.assertEqual(calls[4]["cmd"][calls[4]["cmd"].index("--plan-role") + 1], "final_editor")
        self.assertEqual(calls[4]["cmd"][calls[4]["cmd"].index("--editor-role") + 1], "final_editor")
        self.assertEqual(calls[4]["cmd"][calls[4]["cmd"].index("--review-role") + 1], "reviewer")
        self.assertEqual(
            calls[4]["cmd"][calls[4]["cmd"].index("--review-report") + 1],
            os.path.join("out", "story_20260918_0", "native_check_result.json"),
        )
        self.assertEqual(
            calls[4]["cmd"][calls[4]["cmd"].index("--unresolved-remediation-rounds") + 1],
            "2",
        )
        self.assertEqual(calls[4]["timeout"], 900)
        self.assertEqual(calls[5]["cmd"][1], "final_editor.py")
        self.assertEqual(calls[5]["cmd"][calls[5]["cmd"].index("--mode") + 1], "repair")
        self.assertNotIn("native_check.py", calls[5]["cmd"])
        self.assertEqual([call["name"] for call in calls].count("verify"), 1)
        self.assertFalse(any(
            call["cmd"][1] == "native_check.py" and
            "story_20260918_0_final_edit" in call["cmd"]
            for call in calls
        ))
        self.assertIn("story_20260918_0_final_edit_final_test1", calls[6]["cmd"])
        self.assertEqual(result["story_id"], "story_20260918_0_final_edit_final_test1")
        self.assertEqual(result["source_story_id"], "story_20260918_0")

    def test_rejected_candidate_prevents_packaging_of_source_or_candidate(self):
        result, calls = self._run(enabled=True, final_editor_ok=False)
        self.assertEqual(result["status"], "ai_repair_failed")
        self.assertEqual([call["name"] for call in calls], ["radar", "adopt", "write", "verify", "ai-detect", "ai-repair"])

    def test_explicit_auto_replace_runs_one_repair_attempt_then_full_recheck(self):
        calls = []

        def step(name, cmd, dry_run=False, timeout=1800):
            calls.append({"name": name, "cmd": cmd, "timeout": timeout})
            if name == "verify":
                return False, "editorial findings"
            return True, "ok"

        cfg = config(enabled=False)
        cfg["switches"]["auto_replace_enabled"] = True
        with patch("orchestrator.run_step", side_effect=step), \
             patch("orchestrator.today", return_value="2026-09-18"), \
             patch("orchestrator.progress.update"):
            result = orchestrator.run_cycle(FakeStore(), cfg, dry_run=True)

        self.assertEqual(
            [call["name"] for call in calls],
            ["radar", "adopt", "write", "verify", "repair-cards", "repair-plan-draft", "repair-apply", "repair-verify", "package"],
        )
        self.assertTrue(all(flag in calls[5]["cmd"] for flag in ("--resume", "--plan", "--draft")))
        self.assertIn("apply_repair_drafts.py", calls[6]["cmd"])
        self.assertIn("native_check_result.json", " ".join(calls[7]["cmd"]))
        self.assertEqual(result["status"], "done")

if __name__ == "__main__":
    unittest.main()
