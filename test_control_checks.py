import unittest
import json

from control_checks import (
    check_prose_budget, check_state_delta, manual_review_findings,
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
            "scene-not-once", "fact-evidence-value-not-found", "fact-value-conflict",
        })
        scene_finding = next(item for item in findings if item["rule_id"] == "scene-not-once")
        self.assertEqual(scene_finding["evidence"], "Mara reaches the ward.")

    def test_fact_status_must_prove_the_reported_value_in_its_quote(self):
        item = delta()
        item["non_regression_statuses"][0]["evidence"] = "Mara reaches the ward."
        item["scene_statuses"] = [{
            "scene_id": "lab", "status": "completed", "occurrence_count": 1,
            "evidence": "Mara reaches the ward.",
        }]
        item["fact_statuses"] = [{
            "fact_id": "sample-count", "status": "consistent", "value": "twelve",
            "evidence": "Mara catalogued six samples.",
        }]
        scenes = [{
            "scene_id": "lab", "fact_slots": [{
                "id": "sample-count", "claim": "Sample count", "value": "twelve",
            }],
        }]
        findings = check_state_delta({
            "end_assertions": [{"id": "mara-reaches-ward", "claim": "Mara reaches the ward.", "required": True}],
            "must_not_regress": [{"id": "ward-danger", "claim": "Mara knows the ward is dangerous.", "evidence_requirement": "Mara avoids the ward."}],
        }, item, "Mara reaches the ward. Mara catalogued six samples.", scenes)
        self.assertEqual([entry["rule_id"] for entry in findings], ["fact-evidence-value-not-found"])
        self.assertEqual(findings[0]["expected_value"], "twelve")
        self.assertEqual(findings[0]["observed_value"], "twelve")
        self.assertEqual(findings[0]["evidence"], "Mara catalogued six samples.")

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
        self.assertIn("Set new_information, relationship_changes, open_hooks, and conflicts_with_contract to [] exactly", prompt)
        self.assertIn("scene_statuses must contain every supplied scene_id", prompt)
        self.assertIn("fact_statuses must contain every supplied fact slot", prompt)
        self.assertIn("never use fact_slot_id", prompt)

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


if __name__ == "__main__":
    unittest.main()
