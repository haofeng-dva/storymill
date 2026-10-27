import unittest
import json
import shutil

from control_checks import (
    active_control_findings, append_control_findings, check_prose_budget, check_state_delta, manual_review_findings,
    parse_state_delta_output, validate_state_delta, write_state_delta,
)
from control_contracts import ControlContractError, sha256_text
from engine import WritingEngine


def delta():
    return {
        "chapter": 2,
        "source_sha256": "b" * 64,
        "assertion_statuses": [{
            "assertion_id": "mara-reaches-ward", "status": "met", "evidence": "Mara reaches the ward.",
        }],
        "non_regression_statuses": [{
            "assertion_id": "ward-danger", "status": "preserved", "evidence": "She avoids touching it again.",
        }],
        "scene_statuses": [],
        "fact_statuses": [],
        "new_information": ["The ward reacts to Mara."],
        "relationship_changes": ["Rafe trusts Mara with a sample."],
        "open_hooks": ["Who altered the ward?"],
        "conflicts_with_contract": [],
    }


class ControlStateChecksTests(unittest.TestCase):
    def test_resolved_control_cards_are_retained_but_reopened_if_reobserved(self):
        from pathlib import Path
        root = Path(".control-card-test")
        shutil.rmtree(root, ignore_errors=True)
        try:
            path = root / "control_findings.json"
            finding = {"chapter": 1, "rule_id": "state", "message": "state failed", "source": "control_state"}
            append_control_findings(path, [finding])
            report = json.loads(path.read_text(encoding="utf-8"))
            report["findings"][0]["status"] = "resolved"
            path.write_text(json.dumps(report), encoding="utf-8")
            self.assertEqual(active_control_findings(path), [])
            append_control_findings(path, [finding])
            self.assertEqual(len(active_control_findings(path)), 1)
            self.assertEqual(active_control_findings(path)[0]["status"], "detected")
        finally:
            shutil.rmtree(root, ignore_errors=True)
    def test_valid_delta_matches_atomic_contract(self):
        item = delta()
        item["non_regression_statuses"][0]["evidence"] = "Mara reaches the ward."
        self.assertEqual(validate_state_delta(item, 2, "b" * 64), item)
        contract = {
            "end_assertions": [{"id": "mara-reaches-ward", "claim": "Mara reaches the ward.", "required": True}],
            "must_not_regress": [{"id": "ward-danger", "claim": "Mara already knows the ward is dangerous.", "evidence_requirement": "Mara avoids the ward."}],
        }
        text = "Mara reaches the ward. She avoids touching it again."
        self.assertEqual(check_state_delta(contract, item, text), [])

    def test_unclear_and_missing_or_unquoted_assertions_create_blocking_findings(self):
        item = delta()
        item["assertion_statuses"][0]["status"] = "unclear"
        item["non_regression_statuses"][0]["assertion_id"] = "wrong-id"
        findings = check_state_delta({
            "end_assertions": [{"id": "mara-reaches-ward", "claim": "Mara reaches the ward.", "required": True}],
            "must_not_regress": [{"id": "ward-danger", "claim": "Mara knows the ward is dangerous.", "evidence_requirement": "Mara avoids the ward."}],
        }, item, "Mara reaches the ward. She avoids touching it again.")
        self.assertEqual({finding["rule_id"] for finding in findings}, {
            "end-assertion-not-met", "missing-non-regression-status", "unexpected-non-regression-status",
        })
        self.assertTrue(all(finding["severity"] == "blocking" for finding in findings))

    def test_evidence_must_occur_verbatim_in_chapter(self):
        item = delta()
        item["assertion_statuses"][0]["evidence"] = "Mara arrives at the ward."
        findings = check_state_delta({
            "end_assertions": [{"id": "mara-reaches-ward", "claim": "Mara reaches the ward.", "required": True}],
            "must_not_regress": [{"id": "ward-danger", "claim": "Mara knows the ward is dangerous.", "evidence_requirement": "Mara avoids the ward."}],
        }, item, "Mara reaches the ward. She avoids touching it again.")
        self.assertEqual([item["rule_id"] for item in findings], ["end-assertion-evidence-not-found"])

    def test_scene_replay_and_fact_value_conflict_are_blocking(self):
        item = delta()
        item["non_regression_statuses"][0]["evidence"] = "Mara reaches the ward."
        item["scene_statuses"] = [{
            "scene_id": "lab", "status": "repeated", "occurrence_count": 2,
            "evidence": "Mara reaches the ward.",
        }]
        item["fact_statuses"] = [{
            "fact_id": "sample-count", "status": "contradicted", "value": "six",
            "evidence": "Mara reaches the ward.",
        }]
        scenes = [{
            "scene_id": "lab", "fact_slots": [{
                "id": "sample-count", "claim": "Sample count", "value": "twelve",
            }],
        }]
        findings = check_state_delta({
            "end_assertions": [{"id": "mara-reaches-ward", "claim": "Mara reaches the ward.", "required": True}],
            "must_not_regress": [{"id": "ward-danger", "claim": "Mara knows the ward is dangerous.", "evidence_requirement": "Mara avoids the ward."}],
        }, item,
                                     "Mara reaches the ward.", scenes)
        self.assertEqual({item["rule_id"] for item in findings}, {
            "scene-not-once", "fact-value-conflict",
        })
        scene_finding = next(item for item in findings if item["rule_id"] == "scene-not-once")
        self.assertEqual(scene_finding["evidence"], "Mara reaches the ward.")

    def test_fact_status_allows_natural_language_evidence_for_a_canonical_summary(self):
        item = delta()
        item["non_regression_statuses"][0]["evidence"] = "Mara reaches the ward."
        item["scene_statuses"] = [{
            "scene_id": "lab", "status": "completed", "occurrence_count": 1,
            "evidence": "Mara reaches the ward.",
        }]
        item["fact_statuses"] = [{
            "fact_id": "sample-count", "status": "consistent", "value": "twelve",
            "evidence": "Mara sealed the final sample in its twelfth vial.",
        }]
        scenes = [{
            "scene_id": "lab", "fact_slots": [{
                "id": "sample-count", "claim": "Sample count", "value": "twelve",
            }],
        }]
        findings = check_state_delta({
            "end_assertions": [{"id": "mara-reaches-ward", "claim": "Mara reaches the ward.", "required": True}],
            "must_not_regress": [{"id": "ward-danger", "claim": "Mara knows the ward is dangerous.", "evidence_requirement": "Mara avoids the ward."}],
        }, item, "Mara reaches the ward. Mara sealed the final sample in its twelfth vial.", scenes)
        self.assertEqual(findings, [])

    def test_story_bible_fact_slot_requires_a_semantic_state_result(self):
        item = delta()
        item["non_regression_statuses"][0]["evidence"] = "Mara reaches the ward."
        item["fact_statuses"] = [{
            "fact_id": "story-bible--sample-count", "status": "contradicted", "value": "six",
            "evidence": "Mara catalogued six samples.",
        }]
        findings = check_state_delta(
            {"end_assertions": [{"id": "mara-reaches-ward", "claim": "Mara reaches the ward.", "required": True}],
             "must_not_regress": [{"id": "ward-danger", "claim": "Mara knows the ward is dangerous.", "evidence_requirement": "Mara avoids the ward."}]},
            item, "Mara reaches the ward. Mara catalogued six samples.", [],
            [{"id": "story-bible--sample-count", "claim": "Canon sample count", "value": "twelve"}],
        )
        self.assertEqual([entry["rule_id"] for entry in findings], ["fact-value-conflict"])

    def test_story_bible_context_may_be_unclear_without_becoming_a_conflict(self):
        item = delta()
        item["non_regression_statuses"][0]["evidence"] = "Mara reaches the ward."
        item["fact_statuses"] = [{
            "fact_id": "story-bible--sample-count", "status": "unclear", "value": "twelve",
            "evidence": "Mara reaches the ward.",
        }]
        findings = check_state_delta(
            {"end_assertions": [{"id": "mara-reaches-ward", "claim": "Mara reaches the ward.", "required": True}],
             "must_not_regress": [{"id": "ward-danger", "claim": "Mara knows the ward is dangerous.", "evidence_requirement": "Mara avoids the ward."}]},
            item, "Mara reaches the ward.", [],
            [{"id": "story-bible--sample-count", "claim": "Canon sample count", "value": "twelve"}],
        )
        self.assertEqual(findings, [])

    def test_failed_or_unclear_status_also_requires_verbatim_evidence(self):
        item = delta()
        item["assertion_statuses"][0].update({
            "status": "unclear", "evidence": "Mara might arrive at the ward.",
        })
        findings = check_state_delta({
            "end_assertions": [{"id": "mara-reaches-ward", "claim": "Mara reaches the ward.", "required": True}],
            "must_not_regress": [{"id": "ward-danger", "claim": "Mara knows the ward is dangerous.", "evidence_requirement": "Mara avoids the ward."}],
        }, item, "Mara reaches the ward. She avoids touching it again.")
        self.assertEqual({entry["rule_id"] for entry in findings}, {
            "end-assertion-evidence-not-found", "end-assertion-not-met",
        })

    def test_parser_rejects_missing_source_hash_or_wrong_chapter(self):
        item = delta()
        del item["source_sha256"]
        with self.assertRaisesRegex(ControlContractError, "source_sha256"):
            parse_state_delta_output(__import__("json").dumps(item), 2, "b" * 64)

        item = delta()
        with self.assertRaisesRegex(ControlContractError, "does not match"):
            parse_state_delta_output(__import__("json").dumps(item), 3, "b" * 64)

        item = delta()
        item["source_sha256"] = "not-a-sha"
        with self.assertRaisesRegex(ControlContractError, "SHA-256"):
            parse_state_delta_output(__import__("json").dumps(item), 2, "b" * 64)

    def test_observational_state_lists_may_be_empty(self):
        item = delta()
        for field in ("new_information", "relationship_changes", "open_hooks", "conflicts_with_contract"):
            item[field] = []
        self.assertEqual(validate_state_delta(item, 2, "b" * 64), item)

    def test_state_delta_accepts_only_a_single_json_object_after_model_label(self):
        item = delta()
        content = "Here is the requested state delta:\n```json\n" + json.dumps(item) + "\n```"
        self.assertEqual(parse_state_delta_output(content, 2, "b" * 64), item)

    def test_state_delta_rejects_content_after_the_json_object(self):
        item = delta()
        content = "State delta:\n" + json.dumps(item) + "\nExtra commentary"
        with self.assertRaisesRegex(ControlContractError, "content after"):
            parse_state_delta_output(content, 2, "b" * 64)

    def test_free_form_reported_conflict_requires_review_but_does_not_block_atomic_state(self):
        item = delta()
        item["non_regression_statuses"][0]["evidence"] = "Mara reaches the ward."
        item["conflicts_with_contract"] = ["A possible conflict the model cannot anchor."]
        findings = check_state_delta({
            "end_assertions": [{"id": "mara-reaches-ward", "claim": "Mara reaches the ward.", "required": True}],
            "must_not_regress": [{"id": "ward-danger", "claim": "Mara knows the ward is dangerous.", "evidence_requirement": "Mara avoids the ward."}],
        }, item, "Mara reaches the ward.")
        self.assertEqual([entry["rule_id"] for entry in findings], ["reported-contract-conflict"])
        self.assertEqual(findings[0]["severity"], "manual_review_required")

    def test_engine_uses_dedicated_route_for_structured_state_delta(self):
        text = "Mara left the ward with the sample."
        response = delta()
        response["source_sha256"] = sha256_text(text)

        class FakeLLM:
            def __init__(self):
                self.calls = []

            def chat(self, role, messages, max_tokens, temperature, json_mode=False):
                self.calls.append((role, messages, max_tokens, temperature, json_mode))
                return {"content": json.dumps(response)}

        engine = WritingEngine()
        fake = FakeLLM()
        engine.llm = fake
        result = engine.generate_state_delta(text, 2, {
            "end_assertions": [{"id": "mara-reaches-ward", "claim": "Mara reaches the ward.", "required": True}],
            "must_not_regress": [{"id": "ward-danger", "claim": "Mara knows the ward is dangerous.", "evidence_requirement": "Mara avoids the ward."}],
        })
        self.assertEqual(result["content"], json.dumps(response))
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.calls[0][0], "state_delta")
        self.assertFalse(fake.calls[0][4])
        prompt = fake.calls[0][1][1]["content"]
        self.assertIn("do not add quotation marks", prompt)
        self.assertIn("one short unresolved open hook", prompt)
        self.assertIn("scene_statuses must contain every supplied scene_id", prompt)
        self.assertIn("fact_statuses must contain every supplied fact slot", prompt)
        self.assertIn("never use fact_slot_id", prompt)

    def test_state_delta_budget_grows_for_story_bible_fact_slots(self):
        engine = object.__new__(WritingEngine)
        engine.llm = __import__("unittest").mock.Mock()
        engine.llm.chat.return_value = {"content": "{}"}
        engine.generate_state_delta("Mara reaches the ward.", 2, {
            "end_assertions": [], "must_not_regress": [],
        }, (), [{"id": f"story-bible--fact-{index}", "claim": "Canon", "value": "fixed"} for index in range(11)])
        self.assertEqual(engine.llm.chat.call_args.kwargs["max_tokens"], 1680)

    def test_state_delta_correction_requires_literal_evidence(self):
        engine = object.__new__(WritingEngine)
        engine.llm = __import__("unittest").mock.Mock()
        engine.llm.chat.return_value = {"content": "{}"}
        engine.generate_state_delta("Mara reaches the ward.", 2, {
            "end_assertions": [], "must_not_regress": [],
        }, correction="Evidence for fact slot ward is not a verbatim chapter quotation.")
        prompt = engine.llm.chat.call_args.args[1][1]["content"]
        self.assertIn("previous state JSON was rejected", prompt)
        self.assertIn("exact contiguous substring", prompt)

    def test_budget_is_local_and_manual_categories_never_auto_pass(self):
        findings, counts, totals = check_prose_budget(
            "The room narrowed. Later the room narrowed again.",
            {"rules": [{"id": "room", "pattern": r"room\s+narrow", "evidence_ids": ["ops-4"]}],
             "per_chapter_limits": {"room": 1}, "book_limits": {"room": 2}},
            1,
        )
        self.assertEqual(counts, {"room": 2})
        self.assertEqual(totals, {"room": 2})
        self.assertEqual(findings[0]["category"], "prose_budget")
        manual = manual_review_findings(1, [{"scene_id": "s1"}], [{"name": "Mara"}], {"visible_loss": "lab"})
        self.assertEqual({item["category"] for item in manual}, {"opening", "scene", "voice"})
        self.assertTrue(all(item["severity"] == "manual_review_required" for item in manual))

    def test_declared_contract_fact_without_a_scene_slot_blocks_state_validation(self):
        item = delta()
        item["non_regression_statuses"][0]["evidence"] = "Mara reaches the ward."
        findings = check_state_delta({
            "end_assertions": [{"id": "mara-reaches-ward", "claim": "Mara reaches the ward.", "required": True}],
            "must_not_regress": [{"id": "ward-danger", "claim": "Mara knows the ward is dangerous.", "evidence_requirement": "Mara avoids the ward."}],
            "facts": [{"id": "sample-count", "claim": "Sample count", "value": "twelve"}],
        }, item, "Mara reaches the ward.", [{"scene_id": "lab", "fact_slots": []}])
        self.assertIn("fact-slot-not-covered", [entry["rule_id"] for entry in findings])


if __name__ == "__main__":
    unittest.main()
