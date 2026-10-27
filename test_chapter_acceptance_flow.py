# -*- coding: utf-8 -*-
"""Flow tests for the chapter acceptance transaction; no provider calls."""
import hashlib
import json
import shutil
import sys
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import engine as engine_module
from chapter_state import ChapterStatus
from chapter_contract import planning_hash


INITIAL_TEXT = "Alpha beta gamma cited wrong eta theta iota kappa lambda."
REPAIRED_TEXT = "Alpha beta gamma cited fixed eta theta iota kappa lambda."
REQUIREMENTS = [{"id": "ch0001-requirement-01", "requirement": "The required event occurs."}]


class _FakeLLM:
    models = {}
    repair_patches = [{"find": "cited wrong", "replace": "cited fixed"}]
    repair_patch_batches = None

    def __init__(self):
        self.repair_calls = 0

    def set_run_context(self, *_args, **_kwargs):
        pass

    def set_chapter_context(self, *_args, **_kwargs):
        pass

    def set_call_context(self, *_args, **_kwargs):
        pass

    def chat(self, role, _messages, **_kwargs):
        if role == "writer":
            patches = self.repair_patches
            if self.repair_patch_batches is not None:
                index = min(self.repair_calls, len(self.repair_patch_batches) - 1)
                patches = self.repair_patch_batches[index]
            self.repair_calls += 1
            return {"content": json.dumps({"patches": patches})}
        return {"content": "handoff"}


class _FakeEngine(engine_module.WritingEngine):
    word_min = 10
    word_max = 10
    generated_extra_rules = []
    compression_result = None
    compression_calls = 0

    def __init__(self):
        self.cfg = {"paths": {"output": "shorts"}}
        self.w = {
            "default_chapters": 2, "default_words": 10, "word_band": 0,
            "word_min": self.word_min, "word_max": self.word_max, "parallel_blocks": 1,
            "max_rewrite": 0, "recovery_rounds": 0, "recovery_wait_seconds": 0,
        }
        self.llm = _FakeLLM()
        self.anchor_examples = []
        self.experience_runtime = type("Experience", (), {"enabled": True})()

    def set_genre(self, _direction, _genre):
        return None

    def experience_block(self, _chapter_no):
        return "", []

    def write_outline(self, _direction):
        return {"content": "Chapter 1\n- The required event occurs.", "elapsed": 0}

    def write_world_bible(self, _direction, _outline):
        return {"content": "test bible", "elapsed": 0}

    def write_with_retry(self, *_args, **_kwargs):
        type(self).generated_extra_rules.append(_args[-1])
        return {"text": INITIAL_TEXT, "words": 10, "elapsed": 0, "rewrites": 0}

    def compact_chapter_to_word_limit(self, _text, _minimum_words, _maximum_words, _protected_quotes):
        type(self).compression_calls += 1
        return {"content": type(self).compression_result, "words": 9}

    def ai_tell_check(self, _text):
        return [], {}

    def generate_handoff(self, _text):
        return {"content": "handoff"}

    def generate_cn_summary(self, *_args):
        return {"content": "test summary"}

    def compile_chapter_contracts(self, direction, outline, bible, story_id, chapter_numbers):
        return {"content": {
            "schema_version": "chapter_acceptance_contract_v1",
            "story_id": story_id,
            "source_sha256": planning_hash(direction, outline, bible),
            "chapters": {
                f"{chapter_no:04d}": {
                    "chapter": chapter_no,
                    "requirements": [{
                        "id": f"ch{chapter_no:04d}-contract-01",
                        "type": "event",
                        "requirement": "The required event occurs.",
                        "source_quote": "test outline",
                    }],
                }
                for chapter_no in chapter_numbers
            },
        }}


class _FakeStateStore:
    def __init__(self):
        self.rows = {}
        self.finished = []

    def start_run(self, *_args, **_kwargs):
        pass

    def finish_run(self, *args, **_kwargs):
        self.finished.append(args)

    def upsert_chapter_task(self, run_id, story_id, chapter_no, status, **fields):
        row = self.rows.setdefault(chapter_no, {"run_id": run_id, "story_id": story_id, "chapter_no": chapter_no})
        row.update({"status": status, **fields})

    def chapter_tasks(self, _run_id):
        return [self.rows[number].copy() for number in sorted(self.rows)]


class _FakeBookMemory:
    def __init__(self, *_args):
        pass

    def context_block(self, _chapter_no):
        return ""

    def save_chapter(self, *_args, **_kwargs):
        pass


class _Judge:
    def __init__(self, *_args):
        pass

    def blind(self, _text):
        return 5, "verdict: human", "test"


class _ExplodingJudge:
    def __init__(self, *_args):
        raise AssertionError("production writing must not instantiate QualityJudge")


def _review(text, findings):
    return {
        "source_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "outcomes": [{"id": REQUIREMENTS[0]["id"], "status": "met"}],
        "findings": findings,
    }


class ChapterAcceptanceFlowTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(".chapter-acceptance-flow-" + uuid.uuid4().hex)
        self.root.mkdir()
        self.store = _FakeStateStore()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _run_flow(self, reviews, chapters, repair_patches=None, repair_patch_batches=None,
                  word_bounds=(10, 10), chapter_contracts=False, max_repair_rounds=1,
                  run_id=None, summary_result=None, judge_class=_Judge, compression_result=None):
        self.reviewed_names = []
        self.reviewed_requirements = []
        self.reviewed_ledgers = []
        _FakeEngine.compression_result = compression_result
        _FakeEngine.compression_calls = 0
        _FakeEngine.generated_extra_rules = []

        def fake_review(name, text, _requirements, _reviewers, issue_ledger=None,
                        editorial_findings=True, contract_evidence_only=False):
            self.assertFalse((self.root / "shorts" / "flow" / "ch0001.md").exists())
            self.reviewed_names.append(name)
            self.reviewed_requirements.append(_requirements)
            self.reviewed_ledgers.append(issue_ledger)
            return reviews.pop(0)(text)

        with patch.object(engine_module, "HERE", str(self.root)), \
             patch.object(_FakeLLM, "repair_patches", repair_patches or _FakeLLM.repair_patches), \
             patch.object(_FakeLLM, "repair_patch_batches", repair_patch_batches), \
             patch.object(_FakeEngine, "word_min", word_bounds[0]), \
             patch.object(_FakeEngine, "word_max", word_bounds[1]), \
             patch.object(_FakeEngine, "generate_cn_summary", return_value=summary_result or {"content": "test summary"}), \
             patch.object(engine_module, "WritingEngine", _FakeEngine), \
             patch.object(engine_module, "StateStore", return_value=self.store), \
             patch.object(engine_module, "BookMemory", _FakeBookMemory), \
             patch.object(engine_module, "load_env"), \
             patch.object(engine_module, "load_json", return_value={"chapter_acceptance": {"enabled": True, "max_repair_rounds": max_repair_rounds}}), \
             patch.object(engine_module, "load_lessons", return_value=[]), \
             patch.object(engine_module.progress, "update"), \
             patch("native_check.QualityJudge", judge_class), \
             patch("native_check.build_acceptance_reviewers", return_value=[]), \
             patch("native_check.extract_chapter_requirements", return_value=REQUIREMENTS), \
             patch("native_check.review_chapter_acceptance", side_effect=fake_review), \
             patch.object(sys, "argv", ["engine.py", "--direction", "test", "--story-id", "flow", "--chapters", str(chapters), "--words", "10"] + (["--chapter-contracts"] if chapter_contracts else []) + (["--run-id", run_id] if run_id else [])):
            return engine_module.main()

    def test_production_flow_does_not_instantiate_blind_judge(self):
        result = self._run_flow(
            [lambda text: _review(text, [])], chapters=1,
            judge_class=_ExplodingJudge,
        )
        self.assertEqual(result, 0)

    def test_summary_failure_marks_the_run_failed_and_returns_nonzero(self):
        result = self._run_flow(
            [lambda text: _review(text, [])], chapters=1,
            summary_result={"error": "summary provider unavailable"},
        )

        self.assertEqual(result, 2)
        self.assertFalse((self.root / "shorts" / "flow" / "summary_cn.md").exists())
        self.assertEqual(self.store.finished[-1][3], "failed")

    def test_acceptance_history_is_retained_for_each_run(self):
        for run_id in ("acceptance-run-one", "acceptance-run-two"):
            result = self._run_flow([
                lambda _text: {"error": "reviewer unavailable"},
            ], chapters=1, run_id=run_id)
            self.assertEqual(result, 2)

        root = self.root / "out" / "flow" / "chapter_acceptance"
        first = json.loads((root / "runs" / "acceptance-run-one" / "ch0001.json").read_text(encoding="utf-8"))
        second = json.loads((root / "runs" / "acceptance-run-two" / "ch0001.json").read_text(encoding="utf-8"))
        latest = json.loads((root / "ch0001.json").read_text(encoding="utf-8"))
        self.assertEqual(first["run_id"], "acceptance-run-one")
        self.assertEqual(second["run_id"], "acceptance-run-two")
        self.assertEqual(latest["run_id"], "acceptance-run-two")

    def test_acceptance_provider_timeout_is_recorded_as_infrastructure_failure(self):
        result = self._run_flow([
            lambda _text: {"error": "reviewer unavailable: TimeoutError: The read operation timed out"},
        ], chapters=1)

        self.assertEqual(result, 2)
        self.assertEqual(self.store.rows[1]["failure_type"], "upstream_timeout")

    def test_multi_provider_acceptance_outage_is_not_labeled_as_the_last_fallback(self):
        result = self._run_flow([
            lambda _text: {
                "error": "acceptance reviewer unavailable",
                "provider_failures": [
                    {"provider": "glm", "model": "glm-test", "failure_type": "upstream_timeout"},
                    {"provider": "gpt", "model": "gpt-5.6-terra", "failure_type": "gateway_timeout"},
                    {"provider": "gemini", "model": "gemini-test", "failure_type": "proxy_tls_error"},
                ],
            },
        ], chapters=1)

        self.assertEqual(result, 2)
        self.assertEqual(self.store.rows[1]["failure_type"], "acceptance_infrastructure_failure")
        self.assertEqual(self.store.finished[-1][3], "failed_infra")
        artifact = json.loads((
            self.root / "out" / "flow" / "chapter_acceptance" / "ch0001.json"
        ).read_text(encoding="utf-8"))
        self.assertEqual(
            artifact["history"][0]["review"]["provider_failures"][1]["model"],
            "gpt-5.6-terra",
        )

    def test_repair_then_recheck_passes_before_writing_chapter(self):
        findings = [{
            "category": "clarity_idiomaticity", "reason": "wording is wrong",
            "repair_scope": "sentence", "evidence_quotes": ["cited wrong"],
        }]
        result = self._run_flow([
            lambda text: _review(text, findings),
            lambda text: _review(text, []),
        ], chapters=1)

        chapter = self.root / "shorts" / "flow" / "ch0001.md"
        artifact = self.root / "out" / "flow" / "chapter_acceptance" / "ch0001.json"
        self.assertEqual(result, 0)
        self.assertTrue(chapter.is_file())
        self.assertEqual(chapter.read_text(encoding="utf-8"), REPAIRED_TEXT)
        history = json.loads(artifact.read_text(encoding="utf-8"))["history"]
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["gate"]["decision"], "repair_required")
        self.assertEqual(history[1]["gate"]["decision"], "passed")
        self.assertEqual(self.store.rows[1]["status"], ChapterStatus.COMPLETED.value)
        self.assertEqual(self.reviewed_names, ["ch0001.md", "ch0001.md"])

    def test_length_only_overflow_uses_one_bounded_compaction_then_rechecks(self):
        compacted = "Alpha beta gamma cited wrong eta theta iota kappa."

        def accepted(text):
            return {
                "source_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "outcomes": [{
                    "id": REQUIREMENTS[0]["id"], "status": "met",
                    "evidence_quotes": ["Alpha beta gamma"],
                }],
                "findings": [],
            }

        result = self._run_flow(
            [accepted, accepted], chapters=1, word_bounds=(8, 9),
            compression_result=compacted,
        )

        chapter = self.root / "shorts" / "flow" / "ch0001.md"
        artifact = self.root / "out" / "flow" / "chapter_acceptance" / "ch0001.json"
        history = json.loads(artifact.read_text(encoding="utf-8"))["history"]
        self.assertEqual(result, 0)
        self.assertEqual(chapter.read_text(encoding="utf-8"), compacted)
        self.assertEqual(_FakeEngine.compression_calls, 1)
        self.assertEqual(history[0]["length_compaction"], {
            "before_words": 10, "after_words": 9, "status": "accepted",
        })
        self.assertEqual(history[1]["gate"]["decision"], "passed")

    def test_unresolved_recheck_does_not_write_and_blocks_following_chapter(self):
        findings = [{
            "category": "clarity_idiomaticity", "reason": "wording is wrong",
            "repair_scope": "sentence", "evidence_quotes": ["cited wrong"],
        }]
        result = self._run_flow([
            lambda text: _review(text, findings),
            lambda text: _review(text, [{
                "category": "clarity_idiomaticity", "reason": "still wrong",
                "repair_scope": "sentence", "evidence_quotes": ["cited fixed"],
            }]),
        ], chapters=2)

        story_dir = self.root / "shorts" / "flow"
        self.assertEqual(result, 2)
        self.assertFalse((story_dir / "ch0001.md").exists())
        self.assertFalse((story_dir / "ch0002.md").exists())
        self.assertEqual(self.store.rows[1]["status"], ChapterStatus.FAILED.value)
        self.assertEqual(self.store.rows[2]["status"], ChapterStatus.BLOCKED.value)
        self.assertEqual(self.reviewed_names, ["ch0001.md", "ch0001.md"])

    def test_unclear_outcome_inserts_at_verified_anchor_then_rechecks_before_write(self):
        def unclear_review(text):
            return {
                "source_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "outcomes": [{
                    "id": REQUIREMENTS[0]["id"], "status": "unclear",
                    "requirement": REQUIREMENTS[0]["requirement"],
                    "reason": "The required event is not explicit.",
                    "evidence_quotes": [INITIAL_TEXT],
                    "insertion_anchor": INITIAL_TEXT,
                }],
                "findings": [],
            }

        result = self._run_flow(
            [unclear_review, lambda text: _review(text, [])],
            chapters=1,
            repair_patches=[{
                "insert_after": INITIAL_TEXT,
                "insert": "The required event now occurs.",
            }],
            word_bounds=(10, 20),
        )

        chapter = self.root / "shorts" / "flow" / "ch0001.md"
        artifact = self.root / "out" / "flow" / "chapter_acceptance" / "ch0001.json"
        history = json.loads(artifact.read_text(encoding="utf-8"))["history"]
        self.assertEqual(result, 0)
        self.assertTrue(chapter.is_file())
        self.assertEqual(chapter.read_text(encoding="utf-8"), INITIAL_TEXT + "\n\nThe required event now occurs.")
        self.assertEqual(history[0]["repair"]["patches"][0]["insert_after"], INITIAL_TEXT)
        self.assertEqual(history[1]["gate"]["decision"], "passed")

    def test_frozen_contract_guides_writer_and_acceptance_input(self):
        result = self._run_flow([
            lambda text: _review(text, []),
        ], chapters=1, chapter_contracts=True)

        contract_path = self.root / "shorts" / "flow" / "chapter_acceptance_contract.json"
        self.assertEqual(result, 0)
        self.assertTrue(contract_path.is_file())
        self.assertEqual(self.reviewed_requirements[0][0]["id"], "ch0001-contract-01")
        self.assertIn("Frozen chapter contract", _FakeEngine.generated_extra_rules[0])

    def test_contract_recheck_repairs_new_finding_before_writing(self):
        original_finding = {
            "category": "clarity_idiomaticity", "reason": "wording is wrong",
            "repair_scope": "sentence", "evidence_quotes": ["cited wrong"],
        }
        new_observation = {
            "category": "abstract_template", "reason": "new style observation",
            "repair_scope": "sentence", "evidence_quotes": ["cited fixed"],
        }
        result = self._run_flow([
            lambda text: _review(text, [original_finding]),
            lambda text: {
                "source_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "outcomes": [{"id": "ch0001-contract-01", "status": "met"}],
                "findings": [new_observation],
                "ledger_assessment": [{
                    "id": "ch0001-ledger-01", "status": "resolved",
                    "evidence_quotes": [], "reason": "The original wording is fixed.",
                }],
            },
            lambda text: {
                "source_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "outcomes": [{"id": "ch0001-contract-01", "status": "met"}],
                "findings": [],
                "ledger_assessment": [{
                    "id": "ch0001-ledger-01", "status": "resolved",
                    "evidence_quotes": [], "reason": "The original wording is fixed.",
                }],
            },
        ], chapters=1, chapter_contracts=True, max_repair_rounds=2,
           repair_patch_batches=[
               [{"find": "cited wrong", "replace": "cited fixed"}],
               [{"find": "cited fixed", "replace": "cited rechecked"}],
           ])

        artifact = self.root / "out" / "flow" / "chapter_acceptance" / "ch0001.json"
        saved = json.loads(artifact.read_text(encoding="utf-8"))
        self.assertEqual(result, 0)
        self.assertTrue((self.root / "shorts" / "flow" / "ch0001.md").is_file())
        self.assertEqual(self.reviewed_ledgers[0], None)
        self.assertEqual(self.reviewed_ledgers[1]["items"][0]["id"], "ch0001-ledger-01")
        self.assertEqual(saved["history"][1]["gate"]["repair_findings"], [new_observation])
        self.assertEqual(saved["history"][2]["gate"]["decision"], "passed")

    def test_contract_recheck_new_finding_exhausts_budget_and_blocks_writing(self):
        original_finding = {
            "category": "clarity_idiomaticity", "reason": "wording is wrong",
            "repair_scope": "sentence", "evidence_quotes": ["cited wrong"],
        }
        new_finding = {
            "category": "abstract_template", "reason": "new wording is wrong",
            "repair_scope": "sentence", "evidence_quotes": ["cited fixed"],
        }
        result = self._run_flow([
            lambda text: _review(text, [original_finding]),
            lambda text: {
                "source_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "outcomes": [{"id": "ch0001-contract-01", "status": "met"}],
                "findings": [new_finding],
                "ledger_assessment": [{
                    "id": "ch0001-ledger-01", "status": "resolved",
                    "evidence_quotes": [], "reason": "The original wording is fixed.",
                }],
            },
            lambda text: {
                "source_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "outcomes": [{"id": "ch0001-contract-01", "status": "met"}],
                "findings": [new_finding],
                "ledger_assessment": [{
                    "id": "ch0001-ledger-01", "status": "resolved",
                    "evidence_quotes": [], "reason": "The original wording is fixed.",
                }],
            },
        ], chapters=2, chapter_contracts=True, max_repair_rounds=2,
           repair_patch_batches=[
               [{"find": "cited wrong", "replace": "cited fixed"}],
               [{"find": "cited fixed", "replace": "cited rechecked"}],
           ])

        story_dir = self.root / "shorts" / "flow"
        artifact = self.root / "out" / "flow" / "chapter_acceptance" / "ch0001.json"
        saved = json.loads(artifact.read_text(encoding="utf-8"))
        self.assertEqual(result, 2)
        self.assertFalse((story_dir / "ch0001.md").exists())
        self.assertFalse((story_dir / "ch0002.md").exists())
        self.assertEqual(len(saved["history"]), 3)
        self.assertIn("abstract_template: new wording is wrong", saved["history"][2]["gate"]["blockers"])
        self.assertEqual(self.store.rows[2]["status"], ChapterStatus.BLOCKED.value)

    def test_contract_recheck_blocks_when_initial_issue_remains_unresolved(self):
        original_finding = {
            "category": "clarity_idiomaticity", "reason": "wording is wrong",
            "repair_scope": "sentence", "evidence_quotes": ["cited wrong"],
        }
        result = self._run_flow([
            lambda text: _review(text, [original_finding]),
            lambda text: {
                "source_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "outcomes": [{"id": "ch0001-contract-01", "status": "met"}],
                "findings": [{
                    "category": "abstract_template", "reason": "new style observation",
                    "repair_scope": "sentence", "evidence_quotes": ["cited fixed"],
                }],
                "ledger_assessment": [{
                    "id": "ch0001-ledger-01", "status": "unresolved",
                    "evidence_quotes": ["cited fixed"], "reason": "The original wording problem remains.",
                }],
            },
        ], chapters=2, chapter_contracts=True)

        story_dir = self.root / "shorts" / "flow"
        artifact = self.root / "out" / "flow" / "chapter_acceptance" / "ch0001.json"
        saved = json.loads(artifact.read_text(encoding="utf-8"))
        self.assertEqual(result, 2)
        self.assertFalse((story_dir / "ch0001.md").exists())
        self.assertFalse((story_dir / "ch0002.md").exists())
        self.assertIn("initial issue ch0001-ledger-01 is unresolved", saved["history"][1]["gate"]["blockers"][0])
        self.assertEqual(self.store.rows[2]["status"], ChapterStatus.BLOCKED.value)


if __name__ == "__main__":
    unittest.main()
