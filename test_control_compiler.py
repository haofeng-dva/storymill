import copy
import json
import unittest
from unittest.mock import patch

from control_compiler import (
    ControlTransportError,
    _read_text,
    _chapter_batch_prompt,
    _max_execution_view,
    _single_chapter_artifact_prompt,
    _synchronize_generated_facts,
    _shared_engine_model_call,
    build_compile_prompt,
    build_bundle,
    compile_bundle,
    compile_bundle_batched,
    parse_compiler_output,
    write_bundle,
)
from control_context import MAX_PRIOR_FACT_CHARS, MAX_PRIOR_FACTS_PER_CATEGORY, build_control_context
from control_contracts import ControlContractError, MAX_CONTEXT_BYTES, source_hash
from test_control_contracts import valid_documents


def compiler_payload():
    return valid_documents()


class ControlCompilerTests(unittest.TestCase):
    def test_compiler_synchronizes_contract_facts_from_scene_slots(self):
        payload = compiler_payload()
        payload["chapter_contracts"]["chapters"]["0001"]["facts"] = []
        result = _synchronize_generated_facts(payload)
        self.assertEqual(
            result["chapter_contracts"]["chapters"]["0001"]["facts"],
            result["scene_ledger"]["chapters"]["0001"][0]["fact_slots"],
        )

    def test_compiler_rejects_contract_fact_without_a_matching_scene_slot(self):
        payload = compiler_payload()
        payload["chapter_contracts"]["chapters"]["0001"]["facts"] = [{
            "id": "missing-slot", "claim": "A discrete value", "value": "sealed",
        }]
        payload["scene_ledger"]["chapters"]["0001"][0]["fact_slots"] = []
        with self.assertRaisesRegex(ControlContractError, "must match scene fact_slots"):
            _synchronize_generated_facts(payload)

    def test_compiler_normalizes_only_direction_not_planning_file_bytes(self):
        with patch("control_compiler.Path.read_text", return_value="value\n"):
            self.assertEqual(_read_text("direction", "direction", strip=True), "value")
            self.assertEqual(_read_text("outline", "outline"), "value\n")

    def test_compiler_uses_one_call_and_attaches_common_provenance(self):
        calls = []
        raw = json.dumps(compiler_payload())

        def model_call(prompt):
            calls.append(prompt)
            return {"content": raw}

        bundle = compile_bundle("direction", "outline", "bible", "trial", 1, model_call)
        self.assertEqual(len(calls), 1)
        expected_hash = source_hash("direction", "outline", "bible")
        for document in bundle.values():
            self.assertEqual(document["story_id"], "trial")
            self.assertEqual(document["source_sha256"], expected_hash)
            self.assertEqual(document["review_status"], "confirmed")

    def test_batched_compiler_reuses_source_matched_checkpoint_cache(self):
        documents = compiler_payload()
        global_payload = {
            name: copy.deepcopy(documents[name])
            for name in ("opening_contract", "character_voice_cards", "prose_budget")
        }
        chapter_payload = {
            "chapter_contracts": copy.deepcopy(documents["chapter_contracts"]),
            "scene_ledger": copy.deepcopy(documents["scene_ledger"]),
        }
        responses = iter([
            {"content": json.dumps(global_payload)},
            {"content": json.dumps({"chapter_contracts": chapter_payload["chapter_contracts"]})},
            {"content": json.dumps({"scene_ledger": chapter_payload["scene_ledger"]})},
        ])
        calls = []

        def model_call(_prompt, _max_tokens):
            calls.append(1)
            return next(responses)

        checkpoints = {}

        def load_checkpoint(_cache_dir, _digest, name, *_args):
            return checkpoints.get(name)

        def save_checkpoint(_cache_dir, _digest, name, payload):
            checkpoints[name] = payload

        with patch("control_compiler._cache_payload", side_effect=load_checkpoint), patch(
            "control_compiler._save_cache_payload", side_effect=save_checkpoint,
        ):
            first = compile_bundle_batched(
                "direction", "Chapter One\nMara signs the contract.", "bible", "trial", 1,
                model_call, batch_size=1, cache_dir="cache", attempts=1,
            )
            self.assertEqual(len(calls), 3)
            cached = compile_bundle_batched(
                "direction", "Chapter One\nMara signs the contract.", "bible", "trial", 1,
                lambda *_args: self.fail("cached checkpoints should avoid model calls"),
                batch_size=1, cache_dir="cache", attempts=1,
            )
        self.assertEqual(first["chapter_contracts"]["chapters"], cached["chapter_contracts"]["chapters"])

    def test_batched_compiler_discards_legacy_cached_prose_budget_rules(self):
        documents = compiler_payload()
        global_payload = {
            name: copy.deepcopy(documents[name])
            for name in ("opening_contract", "character_voice_cards", "prose_budget")
        }
        global_payload["prose_budget"] = {
            "rules": ["legacy model instruction"],
            "per_chapter_limits": {"legacy": 1},
            "book_limits": {},
        }
        chapter_payload = {
            "chapter_contracts": copy.deepcopy(documents["chapter_contracts"]),
            "scene_ledger": copy.deepcopy(documents["scene_ledger"]),
        }

        def load_checkpoint(_cache_dir, _digest, name, *_args):
            return global_payload if name == "global" else chapter_payload

        with patch("control_compiler._cache_payload", side_effect=load_checkpoint):
            bundle = compile_bundle_batched(
                "direction", "Chapter One\nMara signs the contract.", "bible", "trial", 1,
                lambda *_args: self.fail("legacy checkpoints should avoid model calls"),
                batch_size=1, cache_dir="cache", attempts=1,
            )

        self.assertEqual(bundle["prose_budget"]["rules"], [])
        self.assertEqual(bundle["prose_budget"]["per_chapter_limits"], {})

    def test_bounded_payload_retries_invalid_json(self):
        from control_compiler import _bounded_model_payload

        responses = iter([{"content": "{"}, {"content": '{"chapter_contracts":{}}'}])
        with patch("control_compiler.time.sleep"):
            payload = _bounded_model_payload(
                lambda *_args: next(responses), "prompt", 10,
                ("chapter_contracts",), "chapter contract", attempts=2,
            )
        self.assertEqual(payload, {"chapter_contracts": {}})

    def test_batched_compiler_does_not_turn_network_error_into_schema_feedback(self):
        documents = compiler_payload()
        global_payload = {
            name: copy.deepcopy(documents[name])
            for name in ("opening_contract", "character_voice_cards", "prose_budget")
        }
        calls = []
        responses = iter([
            {"content": json.dumps(global_payload)},
            {"error": "HTTP 504: gateway timeout", "failure_type": "network_error"},
        ])

        def model_call(prompt, _max_tokens):
            calls.append(prompt)
            return next(responses)

        with self.assertRaisesRegex(ControlTransportError, "HTTP 504"):
            compile_bundle_batched(
                "direction", "Chapter One\nMara signs the contract.", "bible", "trial", 1,
                model_call, batch_size=1, attempts=3,
            )

        self.assertEqual(len(calls), 2)
        self.assertFalse(any("Previous attempt rejected" in prompt for prompt in calls))

    def test_shared_engine_model_call_reuses_one_engine_and_enables_http_retries(self):
        class FakeLLM:
            def __init__(self):
                self.contexts = []
                self.calls = []

            def set_call_context(self, **kwargs):
                self.contexts.append(kwargs)

            def chat(self, *args, **kwargs):
                self.calls.append((args, kwargs))
                return {"content": "{}"}

        class FakeEngine:
            def __init__(self):
                self.llm = FakeLLM()

        engine = FakeEngine()
        with patch("control_compiler._new_compiler_engine", return_value=engine) as create:
            model_call = _shared_engine_model_call()
            model_call("first", 100)
            model_call("second", 200)

        create.assert_called_once_with("")
        self.assertEqual(len(engine.llm.calls), 2)
        self.assertTrue(all(call[1]["relay_max_attempts"] == 3 for call in engine.llm.calls))
        self.assertTrue(all(call[1]["relay_request_timeout"] == 90 for call in engine.llm.calls))

    def test_batched_compiler_retries_invalid_global_schema_with_feedback(self):
        documents = compiler_payload()
        valid_global = {
            name: copy.deepcopy(documents[name])
            for name in ("opening_contract", "character_voice_cards", "prose_budget")
        }
        invalid_global = copy.deepcopy(valid_global)
        invalid_global["character_voice_cards"]["characters"][0] = "Mara"
        calls = []
        responses = iter([
            {"content": json.dumps(invalid_global)},
            {"content": json.dumps(valid_global)},
            {"content": json.dumps({"chapter_contracts": documents["chapter_contracts"]})},
            {"content": json.dumps({"scene_ledger": documents["scene_ledger"]})},
        ])

        def model_call(prompt, _max_tokens):
            calls.append(prompt)
            return next(responses)

        with patch("control_compiler.time.sleep"):
            compile_bundle_batched(
                "direction", "Chapter One\nMara signs the contract.", "bible", "trial", 1,
                model_call, batch_size=1, attempts=2,
            )

        self.assertEqual(len(calls), 4)
        self.assertIn("Previous attempt rejected", calls[1])
        self.assertIn("characters[0] must be an object", calls[1])

    def test_batched_compiler_routes_contract_schema_feedback_only_to_contract(self):
        documents = compiler_payload()
        global_payload = {
            name: copy.deepcopy(documents[name])
            for name in ("opening_contract", "character_voice_cards", "prose_budget")
        }
        bad_contracts = copy.deepcopy(documents["chapter_contracts"])
        bad_contracts["chapters"]["0001"]["end_assertions"] = []
        calls = []
        responses = iter([
            {"content": json.dumps(global_payload)},
            {"content": json.dumps({"chapter_contracts": bad_contracts})},
            {"content": json.dumps({"scene_ledger": documents["scene_ledger"]})},
            {"content": json.dumps({"chapter_contracts": documents["chapter_contracts"]})},
            {"content": json.dumps({"scene_ledger": documents["scene_ledger"]})},
        ])

        def model_call(prompt, _max_tokens):
            calls.append(prompt)
            return next(responses)

        with patch("control_compiler.time.sleep"):
            compile_bundle_batched(
                "direction", "Chapter One\nMara signs the contract.", "bible", "trial", 1,
                model_call, batch_size=1, attempts=2,
            )

        self.assertEqual(len(calls), 5)
        self.assertIn("end_assertions must be a non-empty assertion list", calls[3])
        self.assertNotIn("Previous attempt rejected", calls[4])

    def test_batched_compiler_routes_ledger_schema_feedback_only_to_ledger(self):
        documents = compiler_payload()
        global_payload = {
            name: copy.deepcopy(documents[name])
            for name in ("opening_contract", "character_voice_cards", "prose_budget")
        }
        bad_ledger = copy.deepcopy(documents["scene_ledger"])
        bad_ledger["chapters"]["0001"][0]["fact_slots"][0] = "bare fact"
        calls = []
        responses = iter([
            {"content": json.dumps(global_payload)},
            {"content": json.dumps({"chapter_contracts": documents["chapter_contracts"]})},
            {"content": json.dumps({"scene_ledger": bad_ledger})},
            {"content": json.dumps({"chapter_contracts": documents["chapter_contracts"]})},
            {"content": json.dumps({"scene_ledger": documents["scene_ledger"]})},
        ])

        def model_call(prompt, _max_tokens):
            calls.append(prompt)
            return next(responses)

        with patch("control_compiler.time.sleep"):
            compile_bundle_batched(
                "direction", "Chapter One\nMara signs the contract.", "bible", "trial", 1,
                model_call, batch_size=1, attempts=2,
            )

        self.assertEqual(len(calls), 5)
        self.assertNotIn("Previous attempt rejected", calls[3])
        self.assertIn("fact_slots[0] must be an object", calls[4])

    def test_batched_compiler_routes_cross_artifact_feedback_to_both_prompts(self):
        documents = compiler_payload()
        global_payload = {
            name: copy.deepcopy(documents[name])
            for name in ("opening_contract", "character_voice_cards", "prose_budget")
        }
        mismatched_ledger = copy.deepcopy(documents["scene_ledger"])
        mismatched_ledger["chapters"]["0001"][0]["participants"] = ["Alias Mara"]
        calls = []
        responses = iter([
            {"content": json.dumps(global_payload)},
            {"content": json.dumps({"chapter_contracts": documents["chapter_contracts"]})},
            {"content": json.dumps({"scene_ledger": mismatched_ledger})},
            {"content": json.dumps({"chapter_contracts": documents["chapter_contracts"]})},
            {"content": json.dumps({"scene_ledger": documents["scene_ledger"]})},
        ])

        def model_call(prompt, _max_tokens):
            calls.append(prompt)
            return next(responses)

        with patch("control_compiler.time.sleep"):
            compile_bundle_batched(
                "direction", "Chapter One\nMara signs the contract.", "bible", "trial", 1,
                model_call, batch_size=1, attempts=2,
            )

        self.assertIn("participants must exactly match", calls[3])
        self.assertIn("Previous attempt rejected", calls[4])

    def test_batched_compiler_rejects_malformed_global_cache_before_reuse(self):
        documents = compiler_payload()
        global_payload = {
            name: copy.deepcopy(documents[name])
            for name in ("opening_contract", "character_voice_cards", "prose_budget")
        }
        bad_cached_global = copy.deepcopy(global_payload)
        bad_cached_global["opening_contract"]["visible_loss"] = ""
        responses = iter([
            {"content": json.dumps(global_payload)},
            {"content": json.dumps({"chapter_contracts": documents["chapter_contracts"]})},
            {"content": json.dumps({"scene_ledger": documents["scene_ledger"]})},
        ])
        saved = {}

        def load_checkpoint(_cache_dir, _digest, name, _keys, _label, validator=None):
            if name == "global":
                self.assertIsNotNone(validator)
                with self.assertRaisesRegex(ControlContractError, "visible_loss"):
                    validator(bad_cached_global)
            return None

        with patch("control_compiler._cache_payload", side_effect=load_checkpoint), patch(
            "control_compiler._save_cache_payload", side_effect=lambda _d, _h, name, payload: saved.setdefault(name, payload),
        ), patch("control_compiler.time.sleep"):
            bundle = compile_bundle_batched(
                "direction", "Chapter One\nMara signs the contract.", "bible", "trial", 1,
                lambda *_args: next(responses), batch_size=1, cache_dir="cache", attempts=1,
            )

        self.assertEqual(bundle["opening_contract"]["visible_loss"], "Her sealed laboratory.")
        self.assertIn("global", saved)

    def test_batched_compiler_never_caches_chapter_after_all_schema_retries_fail(self):
        documents = compiler_payload()
        global_payload = {
            name: copy.deepcopy(documents[name])
            for name in ("opening_contract", "character_voice_cards", "prose_budget")
        }
        bad_ledger = copy.deepcopy(documents["scene_ledger"])
        bad_ledger["chapters"]["0001"][0]["fact_slots"][0] = "bare fact"
        responses = [{"content": json.dumps(global_payload)}]
        for _ in range(3):
            responses.extend([
                {"content": json.dumps({"chapter_contracts": documents["chapter_contracts"]})},
                {"content": json.dumps({"scene_ledger": bad_ledger})},
            ])
        saved = {}

        with patch("control_compiler._cache_payload", return_value=None), patch(
            "control_compiler._save_cache_payload", side_effect=lambda _d, _h, name, payload: saved.setdefault(name, payload),
        ), patch("control_compiler.time.sleep"):
            with self.assertRaisesRegex(ControlContractError, "fact_slots\\[0\\] must be an object"):
                compile_bundle_batched(
                    "direction", "Chapter One\nMara signs the contract.", "bible", "trial", 1,
                    lambda *_args: responses.pop(0), batch_size=1, cache_dir="cache", attempts=3,
                )
        self.assertIn("global", saved)
        self.assertNotIn("chapters_0001_0001", saved)

    def test_batched_compiler_rejects_oversized_cached_chapter_before_reuse(self):
        documents = compiler_payload()
        global_payload = {
            name: copy.deepcopy(documents[name])
            for name in ("opening_contract", "character_voice_cards", "prose_budget")
        }
        oversized = {
            "chapter_contracts": copy.deepcopy(documents["chapter_contracts"]),
            "scene_ledger": copy.deepcopy(documents["scene_ledger"]),
        }
        oversized["chapter_contracts"]["chapters"]["0001"]["end_assertions"][0]["claim"] = "x" * 5000
        responses = iter([
            {"content": json.dumps(global_payload)},
            {"content": json.dumps({"chapter_contracts": documents["chapter_contracts"]})},
            {"content": json.dumps({"scene_ledger": documents["scene_ledger"]})},
        ])
        saved = {}

        def load_checkpoint(_cache_dir, _digest, name, _keys, _label, validator=None):
            if name == "chapters_0001_0001":
                self.assertIsNotNone(validator)
                with self.assertRaisesRegex(ControlContractError, "context budget"):
                    validator(oversized)
            return None

        with patch("control_compiler._cache_payload", side_effect=load_checkpoint), patch(
            "control_compiler._save_cache_payload",
            side_effect=lambda _d, _h, name, payload: saved.setdefault(name, payload),
        ):
            compile_bundle_batched(
                "direction", "Chapter One\nMara signs the contract.", "bible", "trial", 1,
                lambda *_args: next(responses), batch_size=1, cache_dir="cache", attempts=1,
            )

        self.assertIn("chapters_0001_0001", saved)
        self.assertLess(
            len(saved["chapters_0001_0001"]["chapter_contracts"]["chapters"]["0001"]["end_assertions"][0]["claim"]),
            5000,
        )

    def test_batched_compiler_retries_fresh_oversized_chapter_before_cache_write(self):
        documents = compiler_payload()
        global_payload = {
            name: copy.deepcopy(documents[name])
            for name in ("opening_contract", "character_voice_cards", "prose_budget")
        }
        oversized_contracts = copy.deepcopy(documents["chapter_contracts"])
        oversized_contracts["chapters"]["0001"]["end_assertions"][0]["claim"] = "x" * 5000
        responses = iter([
            {"content": json.dumps(global_payload)},
            {"content": json.dumps({"chapter_contracts": oversized_contracts})},
            {"content": json.dumps({"scene_ledger": documents["scene_ledger"]})},
            {"content": json.dumps({"chapter_contracts": documents["chapter_contracts"]})},
            {"content": json.dumps({"scene_ledger": documents["scene_ledger"]})},
        ])
        prompts, saved = [], {}

        def model_call(prompt, _max_tokens):
            prompts.append(prompt)
            return next(responses)

        with patch("control_compiler._cache_payload", return_value=None), patch(
            "control_compiler._save_cache_payload",
            side_effect=lambda _d, _h, name, payload: saved.setdefault(name, payload),
        ), patch("control_compiler.time.sleep"):
            compile_bundle_batched(
                "direction", "Chapter One\nMara signs the contract.", "bible", "trial", 1,
                model_call, batch_size=1, cache_dir="cache", attempts=2,
            )

        self.assertIn("context budget", prompts[3])
        self.assertIn("context budget", prompts[4])
        self.assertIn("chapters_0001_0001", saved)

    def test_compiler_prompt_requires_scene_completion_and_fact_slots(self):
        prompt = build_compile_prompt("direction", "outline", "bible", 2)
        self.assertIn("completion_assertion", prompt)
        self.assertIn("fact_slots", prompt)
        self.assertIn('"facts": [{"id": "unique-short-id", "claim": "short literal fact", "value": "one canonical value"}]', prompt)
        self.assertIn("id-only facts", prompt)
        self.assertIn("Do not add incidental locations", prompt)

    def test_batched_chapter_prompt_is_chapter_local(self):
        prompt = _chapter_batch_prompt(
            "full direction that must not be repeated", "full bible that must not be repeated",
            {1: "Chapter One\nMara signs the contract."}, [{"name": "Mara"}],
        )
        self.assertIn("Chapter One", prompt)
        self.assertNotIn("full direction", prompt)
        self.assertNotIn("full bible", prompt)

    def test_parser_rejects_missing_artifacts_and_non_json(self):
        payload = compiler_payload()
        del payload["prose_budget"]
        with self.assertRaisesRegex(ControlContractError, "exactly"):
            parse_compiler_output(json.dumps(payload))
        with self.assertRaisesRegex(ControlContractError, "not valid JSON"):
            parse_compiler_output("not json")

    def test_build_bundle_rejects_invalid_generated_contract(self):
        payload = compiler_payload()
        payload["scene_ledger"]["chapters"]["0001"][0]["irreversible_change"] = ""
        with self.assertRaisesRegex(ControlContractError, "irreversible_change"):
            build_bundle(payload, "trial", "a" * 64, "2026-09-11T00:00:00Z")

    def test_build_bundle_rejects_scene_participant_alias(self):
        payload = compiler_payload()
        payload["scene_ledger"]["chapters"]["0001"][0]["participants"] = ["Dr. Mara"]
        with self.assertRaisesRegex(ControlContractError, "exactly match"):
            build_bundle(payload, "trial", "a" * 64, "2026-09-11T00:00:00Z")

    def test_build_bundle_rejects_a_compiled_context_over_2kb(self):
        payload = compiler_payload()
        payload["chapter_contracts"]["chapters"]["0001"]["end_assertions"][0]["claim"] = "x" * 5000
        with self.assertRaisesRegex(ControlContractError, "context budget"):
            build_bundle(payload, "trial", "a" * 64, "2026-09-11T00:00:00Z")

    def test_build_bundle_reserves_state_handoff_headroom(self):
        payload = compiler_payload()
        payload["chapter_contracts"]["chapters"]["0001"]["end_assertions"][0]["claim"] = "x" * 5000
        with self.assertRaisesRegex(ControlContractError, "context budget"):
            build_bundle(payload, "trial", "a" * 64, "2026-09-11T00:00:00Z")

    def test_compile_preflight_matches_bounded_runtime_state_projection(self):
        payload = compiler_payload()
        second = copy.deepcopy(payload["chapter_contracts"]["chapters"]["0001"])
        second["chapter"] = 2
        second["end_assertions"][0]["id"] = "mara-signs-two"
        second["must_not_regress"][0]["id"] = "lab-closed-two"
        second["facts"][0]["id"] = "ch2-lab-status"
        payload["chapter_contracts"]["chapters"]["0002"] = second
        second_scene = copy.deepcopy(payload["scene_ledger"]["chapters"]["0001"])
        second_scene[0]["scene_id"] = "ch2-contract"
        second_scene[0]["completion_assertion"]["id"] = "ch2-contract-complete"
        second_scene[0]["fact_slots"][0]["id"] = "ch2-lab-status"
        payload["scene_ledger"]["chapters"]["0002"] = second_scene
        bundle = build_bundle(payload, "trial", "a" * 64, "2026-09-11T00:00:00Z")
        prior = _max_execution_view(bundle["chapter_contracts"]["chapters"]["0001"])
        size = build_control_context(bundle, 2, prior, max_bytes=10000)["diagnostics"]["context_bytes"]

        self.assertLessEqual(size, MAX_CONTEXT_BYTES)
        with patch("control_compiler.MAX_CONTEXT_BYTES", size - 1):
            with self.assertRaisesRegex(ControlContractError, "context budget"):
                build_bundle(payload, "trial", "a" * 64, "2026-09-11T00:00:00Z")

    def test_preflight_reserves_worst_case_bytes_regardless_of_prior_fact_slots(self):
        """Preflight must reserve a byte upper bound, not the prior chapter's slots.

        Runtime facts come from the model's reported state delta, so a chapter
        whose scenes declare no fact slots can still hand over two facts.  If
        preflight only reserved what ``fact_slots`` contains, a bundle would pass
        compilation and then fail while writing prose.

        The reserved view is an upper bound, not a valid delta: its ``max-fact-*``
        ids reference no contract.  Only ``build_control_context`` ever sees it.
        """
        payload = compiler_payload()
        second = copy.deepcopy(payload["chapter_contracts"]["chapters"]["0001"])
        second["chapter"] = 2
        second["end_assertions"][0]["id"] = "mara-signs-two"
        second["must_not_regress"][0]["id"] = "lab-closed-two"
        second["facts"][0]["id"] = "ch2-lab-status"
        payload["chapter_contracts"]["chapters"]["0002"] = second
        second_scene = copy.deepcopy(payload["scene_ledger"]["chapters"]["0001"])
        second_scene[0]["scene_id"] = "ch2-contract"
        second_scene[0]["completion_assertion"]["id"] = "ch2-contract-complete"
        second_scene[0]["fact_slots"][0]["id"] = "ch2-lab-status"
        payload["scene_ledger"]["chapters"]["0002"] = second_scene
        # Chapter 1 declares no fact slots at all, exactly like the real bundle.
        payload["scene_ledger"]["chapters"]["0001"][0]["fact_slots"] = []
        payload["chapter_contracts"]["chapters"]["0001"]["facts"] = []
        bundle = build_bundle(payload, "trial", "a" * 64, "2026-09-11T00:00:00Z")

        preflight = _max_execution_view(bundle["chapter_contracts"]["chapters"]["0001"])
        self.assertEqual(len(preflight["fact_statuses"]), MAX_PRIOR_FACTS_PER_CATEGORY)
        self.assertEqual(
            len(preflight["new_information"]), MAX_PRIOR_FACTS_PER_CATEGORY
        )
        worst = {
            "assertion_statuses": preflight["assertion_statuses"],
            "non_regression_statuses": preflight["non_regression_statuses"],
            "new_information": ["x" * MAX_PRIOR_FACT_CHARS] * MAX_PRIOR_FACTS_PER_CATEGORY,
            "fact_statuses": [
                {"fact_id": f"F{index}", "status": "consistent", "value": "x" * MAX_PRIOR_FACT_CHARS}
                for index in range(MAX_PRIOR_FACTS_PER_CATEGORY)
            ],
        }
        runtime = build_control_context(bundle, 2, worst, max_bytes=10000)["diagnostics"]["context_bytes"]
        reserve = build_control_context(
            bundle, 2, preflight, max_bytes=10000
        )["diagnostics"]["context_bytes"]
        self.assertGreaterEqual(reserve, runtime)
        self.assertLessEqual(reserve, MAX_CONTEXT_BYTES)

    def test_single_chapter_prompt_uses_the_runtime_context_limit(self):
        prompt = _single_chapter_artifact_prompt(
            {1: "Chapter One\nMara signs the contract."}, [{"name": "Mara"}],
            "chapter_contracts",
        )
        self.assertIn(f"{MAX_CONTEXT_BYTES} UTF-8 byte limit", prompt)
        self.assertIn("Never emit a partial fact_slots object", prompt)

    def test_build_bundle_preflights_bounded_prior_state_for_later_chapters(self):
        payload = compiler_payload()
        second = copy.deepcopy(payload["chapter_contracts"]["chapters"]["0001"])
        second["chapter"] = 2
        second["end_assertions"][0]["id"] = "mara-signs-two"
        second["must_not_regress"][0]["id"] = "lab-closed-two"
        second["end_assertions"][0]["claim"] = "x" * 5000
        second["facts"][0]["id"] = "ch2-contract-fact"
        payload["chapter_contracts"]["chapters"]["0002"] = second
        payload["scene_ledger"]["chapters"]["0002"] = copy.deepcopy(payload["scene_ledger"]["chapters"]["0001"])
        payload["scene_ledger"]["chapters"]["0002"][0]["scene_id"] = "ch2-contract"
        payload["scene_ledger"]["chapters"]["0002"][0]["completion_assertion"]["id"] = "ch2-contract-complete"
        payload["scene_ledger"]["chapters"]["0002"][0]["fact_slots"][0]["id"] = "ch2-contract-fact"
        with self.assertRaisesRegex(ControlContractError, "context budget"):
            build_bundle(payload, "trial", "a" * 64, "2026-09-11T00:00:00Z")

    def test_fenced_json_is_accepted_without_relaxing_schema(self):
        payload = compiler_payload()
        parsed = parse_compiler_output("```json\n" + json.dumps(payload) + "\n```")
        self.assertEqual(parsed["opening_contract"]["visible_loss"], "Her sealed laboratory.")

    def test_parser_discards_model_invented_prose_budget_rules(self):
        payload = compiler_payload()
        payload["prose_budget"] = {
            "rules": [{"id": "invented", "pattern": "x", "evidence_ids": ["none"]}],
            "per_chapter_limits": {"invented": 0}, "book_limits": {"invented": 0},
        }
        parsed = parse_compiler_output(json.dumps(payload))
        self.assertEqual(parsed["prose_budget"], {"rules": [], "per_chapter_limits": {}, "book_limits": {}})

    def test_source_hash_uses_normalized_direction_file_content(self):
        self.assertEqual(source_hash("direction", "outline", "bible"), source_hash("direction\n", "outline", "bible"))

    def test_writer_refuses_overwrite_and_writes_all_artifacts_when_allowed(self):
        bundle = build_bundle(compiler_payload(), "trial", "a" * 64, "2026-09-11T00:00:00Z")
        with patch("control_compiler.Path.exists", return_value=True):
            with self.assertRaisesRegex(ControlContractError, "already exist"):
                write_bundle(bundle, "control", replace=False)

        with patch("control_compiler.Path.exists", return_value=False), patch(
            "control_compiler.Path.mkdir"
        ) as mkdir, patch("control_compiler.Path.write_text") as write_text:
            root = write_bundle(bundle, "control", replace=False)
        self.assertEqual(str(root), "control")
        mkdir.assert_called_once_with(parents=True, exist_ok=True)
        self.assertEqual(write_text.call_count, 5)


if __name__ == "__main__":
    unittest.main()
