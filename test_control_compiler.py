import copy
import json
import unittest
from unittest.mock import patch

from control_compiler import (
    _read_text,
    build_compile_prompt,
    build_bundle,
    compile_bundle,
    parse_compiler_output,
    write_bundle,
)
from control_contracts import ControlContractError, source_hash
from test_control_contracts import valid_documents


def compiler_payload():
    return valid_documents()


class ControlCompilerTests(unittest.TestCase):
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

    def test_compiler_prompt_requires_scene_completion_and_fact_slots(self):
        prompt = build_compile_prompt("direction", "outline", "bible", 2)
        self.assertIn("completion_assertion", prompt)
        self.assertIn("fact_slots", prompt)
        self.assertIn("Do not add incidental locations", prompt)

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
        payload["chapter_contracts"]["chapters"]["0001"]["end_assertions"][0]["claim"] = "x" * 2200
        with self.assertRaisesRegex(ControlContractError, "context budget"):
            build_bundle(payload, "trial", "a" * 64, "2026-09-11T00:00:00Z")

    def test_build_bundle_reserves_state_handoff_headroom(self):
        payload = compiler_payload()
        payload["chapter_contracts"]["chapters"]["0001"]["end_assertions"][0]["claim"] = "x" * 1750
        with self.assertRaisesRegex(ControlContractError, "context budget"):
            build_bundle(payload, "trial", "a" * 64, "2026-09-11T00:00:00Z")

    def test_build_bundle_preflights_bounded_prior_state_for_later_chapters(self):
        payload = compiler_payload()
        second = copy.deepcopy(payload["chapter_contracts"]["chapters"]["0001"])
        second["chapter"] = 2
        second["end_assertions"][0]["id"] = "mara-signs-two"
        second["must_not_regress"][0]["id"] = "lab-closed-two"
        second["end_assertions"][0]["claim"] = "x" * 1900
        second["facts"][0]["id"] = "lab_closed_two"
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
