import json
import unittest
from pathlib import Path
from unittest.mock import patch

from control_runtime import ControlledGenerationRuntime
from control_contracts import ControlContractError, sha256_text
from test_control_contracts import valid_documents


class ControlRuntimeReplayTests(unittest.TestCase):
    def _runtime(self, root):
        return ControlledGenerationRuntime(
            "trial", root / "control", root / "out", {"artifacts": valid_documents()}
        )

    def _response(self, text, evidence):
        return {"content": json.dumps({
            "chapter": 1,
            "source_sha256": sha256_text(text),
            "assertion_statuses": [{"assertion_id": "mara-signs", "status": "met", "evidence": evidence[0]}],
            "non_regression_statuses": [{"assertion_id": "lab-closed", "status": "preserved", "evidence": evidence[1]}],
            "scene_statuses": [{"scene_id": "ch1-contract", "status": "completed", "occurrence_count": 1, "evidence": evidence[0]}],
            "fact_statuses": [{"fact_id": "ch1-lab-status", "status": "consistent", "value": "sealed", "evidence": evidence[1]}],
            "new_information": [], "relationship_changes": [], "open_hooks": [],
            "conflicts_with_contract": [],
        })}

    def test_completed_scene_once_persists_a_passing_delta(self):
        text = "Mara accepts the contract. Mara finds the sealed laboratory inaccessible."
        root = Path("runtime-test")
        runtime = self._runtime(root)
        with patch("control_runtime.write_state_delta") as write_delta, patch("control_runtime.append_control_findings") as append:
            self.assertTrue(runtime.record_state_delta(1, text, self._response(text, (
                "Mara accepts the contract.", "Mara finds the sealed laboratory inaccessible."
            ))))
        stored_delta = write_delta.call_args.args[1]
        self.assertEqual(stored_delta["new_information"], [])
        append.assert_not_called()

    def test_scene_replay_persists_failed_delta_and_blocking_card(self):
        text = "Mara accepts the contract. Mara accepts the contract again. Mara finds the sealed laboratory inaccessible."
        # This is test-only output under the project's ignored runtime root.
        root = Path("out") / "control_runtime_scene_test_output_v2"
        runtime = self._runtime(root)
        response = self._response(text, (
            "Mara accepts the contract.", "Mara finds the sealed laboratory inaccessible."
        ))
        payload = json.loads(response["content"])
        payload["scene_statuses"][0].update({"status": "repeated", "occurrence_count": 2})
        response["content"] = json.dumps(payload)

        self.assertFalse(runtime.record_state_delta(1, text, response))

        stored = json.loads((root / "control" / "state" / "state_delta_ch0001.json").read_text(encoding="utf-8"))
        findings = json.loads((root / "out" / "control_findings.json").read_text(encoding="utf-8"))["findings"]
        self.assertEqual(stored["validation_status"], "failed")
        self.assertEqual([item["rule_id"] for item in findings], ["scene-not-once"])
        self.assertEqual(findings[0]["severity"], "blocking")

    def test_fact_conflict_persists_values_and_quote_in_blocking_card(self):
        text = "Mara accepts the contract. Mara finds the laboratory unsealed."
        root = Path("out") / "control_runtime_fact_test_output_v2"
        runtime = self._runtime(root)
        response = self._response(text, (
            "Mara accepts the contract.", "Mara finds the laboratory unsealed."
        ))
        payload = json.loads(response["content"])
        payload["fact_statuses"][0].update({"status": "contradicted", "value": "unsealed"})
        response["content"] = json.dumps(payload)

        self.assertFalse(runtime.record_state_delta(1, text, response))

        findings = json.loads((root / "out" / "control_findings.json").read_text(encoding="utf-8"))["findings"]
        self.assertEqual([item["rule_id"] for item in findings], ["fact-value-conflict"])
        self.assertEqual(findings[0]["expected_value"], "sealed")
        self.assertEqual(findings[0]["observed_value"], "unsealed")
        self.assertEqual(findings[0]["evidence"], "Mara finds the laboratory unsealed.")

    def test_free_form_conflict_creates_review_card_without_blocking_handoff(self):
        text = "Mara accepts the contract. Mara finds the sealed laboratory inaccessible."
        root = Path("out") / "control_runtime_manual_conflict_test_output"
        runtime = self._runtime(root)
        response = self._response(text, (
            "Mara accepts the contract.", "Mara finds the sealed laboratory inaccessible."
        ))
        payload = json.loads(response["content"])
        payload["conflicts_with_contract"] = ["Possible issue without an atomic evidence contract."]
        response["content"] = json.dumps(payload)

        self.assertTrue(runtime.record_state_delta(1, text, response))
        findings = json.loads((root / "out" / "control_findings.json").read_text(encoding="utf-8"))["findings"]
        self.assertEqual(findings[0]["severity"], "manual_review_required")
        stored = json.loads((root / "control" / "state" / "state_delta_ch0001.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["validation_status"], "passed")

    def test_fixed_chapter_replay_rejects_quote_decoration(self):
        text = "Mara accepts the contract. Mara finds the sealed laboratory inaccessible."
        root = Path("runtime-test")
        runtime = self._runtime(root)
        with patch("control_runtime.write_state_delta") as write_delta, patch("control_runtime.append_control_findings") as append:
            self.assertFalse(runtime.record_state_delta(1, text, self._response(text, (
                "\"Mara accepts the contract.\"", "Mara finds the sealed laboratory inaccessible."
            ))))
        self.assertTrue(write_delta.called)
        findings = append.call_args.args[1]
        self.assertIn("end-assertion-evidence-not-found", [item["rule_id"] for item in findings])

    def test_explicit_scene_card_builds_a_minimal_repair_request(self):
        root = Path("out") / "control_runtime_repair_test_output"
        runtime = self._runtime(root)
        chapter = "Mara accepts the contract. Mara finds the sealed laboratory inaccessible."
        (root / "out").mkdir(parents=True, exist_ok=True)
        (root / "out" / "ch0001.md").write_text(chapter, encoding="utf-8")
        findings = {"findings": [{
            "card_id": "ch0001-scene-not-once-test", "chapter": 1,
            "rule_id": "scene-not-once", "scene_id": "ch1-contract",
            "message": "Scene repeated.", "story_id": "trial",
            "source_sha256": sha256_text(chapter),
            "evidence": "Mara accepts the contract.",
        }]}
        runtime.findings_path.write_text(json.dumps(findings), encoding="utf-8")
        path = runtime.write_repair_request("ch0001-scene-not-once-test")
        request = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(request["scope"], "scene")
        self.assertEqual(request["scene"]["scene_id"], "ch1-contract")
        self.assertEqual(request["source"]["anchor"], "Mara accepts the contract.")
        self.assertNotIn("chapter_text", request["source"])
        self.assertTrue(str(path).endswith("ch0001-scene-not-once-test.json"))

    def test_repair_request_rejects_a_card_after_the_chapter_changes(self):
        root = Path("out") / "control_runtime_repair_hash_test_output"
        runtime = self._runtime(root)
        original = "Mara accepts the contract."
        chapter_path = root / "out" / "ch0001.md"
        chapter_path.parent.mkdir(parents=True, exist_ok=True)
        chapter_path.write_text("Mara accepts a different contract.", encoding="utf-8")
        findings = {"findings": [{
            "card_id": "ch0001-hash-test", "chapter": 1, "story_id": "trial",
            "source_sha256": sha256_text(original), "message": "Old finding.",
        }]}
        runtime.findings_path.write_text(json.dumps(findings), encoding="utf-8")
        with self.assertRaisesRegex(ControlContractError, "source hash does not match"):
            runtime.write_repair_request("ch0001-hash-test")


if __name__ == "__main__":
    unittest.main()
