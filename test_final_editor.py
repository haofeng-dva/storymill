import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from final_editor import (
    _build_verified_plan,
    _build_verified_plan_by_chapter,
    _load_reusable_packets,
    call_json,
    dossier_request,
    dossier_chapter_request,
    run_auto,
    verify_existing_candidate,
)
from final_editor_contract import build_contract


CHAPTERS = {
    "ch0001.md": "Mara's palm flashed green at the ward gate. She tucked the warning card inside her coat and entered alone.",
    "ch0002.md": "Soren passed Mara the records key. Inside the ledger, her mother's collar number had been crossed out in red.",
}


class FakeLLM:
    def __init__(self, include_protected_facts=True):
        self.include_protected_facts = include_protected_facts
        self.calls = []
        self.payloads = []

    def chat(self, role, messages, **kwargs):
        self.assert_role(role)
        payload = json.loads(messages[-1]["content"])
        self.calls.append(payload["task"])
        self.payloads.append(payload)
        if payload["task"].startswith("Return chapter evidence"):
            chapter = payload["chapter"]
            filename = payload["file"]
            result = {
                "chapter": chapter,
                "chapter_role": {"chapter": chapter, "function": "This chapter establishes a concrete investigative turn for Mara.", "evidence_quotes": [{"file": filename, "text": CHAPTERS[filename]}]},
                "findings": [{"id": f"packet-{chapter}", "root_cause": "The scene needs a clearer active investigative consequence.", "impact": "The current beat risks slowing the specific family-record investigation.", "chapters": [chapter], "evidence_quotes": [{"file": filename, "text": CHAPTERS[filename]}], "repair_scope": "scene", "requires_human_decision": False}],
            }
            if self.include_protected_facts:
                result["protected_facts"] = [{"id": f"packet-fact-{chapter}", "claim": "This chapter's cited source event must remain established in the candidate.", "chapters": [chapter], "evidence_quotes": [{"file": filename, "text": CHAPTERS[filename]}]}]
            return {"content": json.dumps(result)}
        if payload["task"].startswith("Return a whole-book dossier"):
            result = {
                "story_id": payload["story_id"],
                "book_promise": "A forensic daughter follows a family record that the ward wants erased.",
                "voice_profile": "Close third person uses concrete sensory evidence before restrained inference.",
                "chapter_roles": [
                    {"chapter": 1, "function": "The ward entry establishes Mara's unauthorized-looking investigation.", "evidence_quotes": [{"file": "ch0001.md", "text": CHAPTERS["ch0001.md"]}]},
                    {"chapter": 2, "function": "The ledger grants a tangible route into Mara's family mystery.", "evidence_quotes": [{"file": "ch0002.md", "text": CHAPTERS["ch0002.md"]}]},
                ],
                "findings": [
                    {"id": "fe-001", "root_cause": "The entry beat leaves Mara's purpose too exposed and static.", "impact": "The opening risks making the investigation feel passive before the records lead arrives.", "chapters": [1], "evidence_quotes": [{"file": "ch0001.md", "text": CHAPTERS["ch0001.md"]}], "repair_scope": "scene", "requires_human_decision": False},
                    {"id": "fe-002", "root_cause": "The ledger clue is stated without a sufficiently active investigative implication.", "impact": "The second chapter's evidence route does not land with enough immediate forward momentum.", "chapters": [2], "evidence_quotes": [{"file": "ch0002.md", "text": CHAPTERS["ch0002.md"]}], "repair_scope": "scene", "requires_human_decision": False},
                ],
            }
            if self.include_protected_facts:
                result["protected_facts"] = [
                    {"id": "fact-ward", "claim": "Mara enters the ward after her palm flashes green at the gate.", "chapters": [1], "evidence_quotes": [{"file": "ch0001.md", "text": CHAPTERS["ch0001.md"]}]},
                    {"id": "fact-ledger", "claim": "Mara's mother's collar number is crossed out in the ledger.", "chapters": [2], "evidence_quotes": [{"file": "ch0002.md", "text": CHAPTERS["ch0002.md"]}]},
                ]
            return {"content": json.dumps(result)}
        if payload["task"].startswith("Return a repair plan"):
            checks = payload["required_verifications"]
            return {"content": json.dumps({
                "story_id": payload["story_id"],
                "dossier_contract_sha256": payload["dossier_contract_sha256"],
                "items": [
                    {"id": "fp-001", "finding_ids": ["fe-001"], "chapter_targets": [1], "allowed_word_deltas": {"1": 0.10}, "editorial_goal": "Clarify Mara's active purpose without adding a new plot event.", "expected_function_after": "The entry beat shows Mara choosing a controlled investigative approach.", "editor_instructions": "Preserve the green scan and warning card, but sharpen Mara's deliberate entry.", "editorial_decision": "Keep Mara entering alone and sharpen the motive in the established ward setting.", "decision_basis": "The cited entry and ward evidence establish her choice without adding a different investigation.", "decision_confidence": "high", "validation_checks": checks},
                    {"id": "fp-002", "finding_ids": ["fe-002"], "chapter_targets": [2], "allowed_word_deltas": {"2": 0.10}, "editorial_goal": "Give the ledger clue a clear immediate investigative charge.", "expected_function_after": "The ledger detail leaves Mara with a concrete and urgent next inference.", "editor_instructions": "Keep the crossed-out collar number and strengthen its investigative force.", "editorial_decision": "Keep the red ledger mark and make it directly motivate Mara's next evidence action.", "decision_basis": "The cited chapter evidence establishes the record key and the crossed-out number as the route forward.", "decision_confidence": "high", "validation_checks": checks},
                ],
                "deferred": [],
            })}
        if payload["task"].startswith("Return {plan_id, chapters}"):
            target = payload["targets"][0]
            text = target["text"]
            revised = text.replace("entered alone.", "entered quietly.").replace("crossed out in red.", "marked missing in red.")
            return {"content": json.dumps({"plan_id": payload["plan_id"], "chapters": {target["file"]: {"source_sha256": target["source_sha256"], "revised_text": revised}}})}
        if payload["task"].startswith("Return {reviewer_id, finding_assessments"):
            return {"content": json.dumps({
                "reviewer_id": "fresh-independent-chapter-reviewer",
                "blocking_findings": [],
                "finding_assessments": [
                    {"finding_id": finding["id"], "status": "resolved"}
                    for finding in payload["findings"]
                ],
                "protected_fact_assessments": [
                    {"fact_id": fact["id"], "status": "preserved"}
                    for fact in payload["protected_facts"]
                ],
            })}
        if payload["task"].startswith("Return {reviewer_id"):
            return {"content": json.dumps({
                "reviewer_id": "fresh-independent-reviewer",
                "candidate_chapter_sha256": payload["candidate_chapter_sha256"],
                "blocking_findings": [],
                "finding_assessments": [{"finding_id": finding_id, "status": "resolved"} for finding_id in payload["planned_finding_ids"]],
                "protected_fact_assessments": [{"fact_id": fact_id, "status": "preserved"} for fact_id in payload["protected_fact_ids"]],
            })}
        raise AssertionError(f"unexpected task: {payload['task']}")

    @staticmethod
    def assert_role(role):
        if role not in {"writer", "final_editor", "reviewer", "cn_summary"}:
            raise AssertionError(f"unexpected role {role}")


class FinalEditorRunTests(unittest.TestCase):
    def test_call_json_retries_invalid_json_then_returns_valid_response(self):
        class RetryJsonLLM:
            def __init__(self):
                self.calls = 0

            def chat(self, role, messages, **kwargs):
                self.calls += 1
                return {"content": "{not json" if self.calls == 1 else '{"ok": true}'}

        llm = RetryJsonLLM()
        self.assertEqual(call_json(llm, "final_editor", "system", {"task": "test"}, 20), {"ok": True})
        self.assertEqual(llm.calls, 2)

    def test_chapter_request_includes_only_its_review_leads(self):
        artifacts = {
            "review_report": json.dumps([
                {"file": "ch0001.md", "editorial_findings": ["First anchored lead", 12]},
                {"file": "ch0002.md", "editorial_findings": ["Other chapter lead"]},
            ])
        }
        contract = build_contract("trial", "run", CHAPTERS, created_at="now")

        payload = dossier_chapter_request(contract, "ch0001.md", CHAPTERS["ch0001.md"], artifacts)

        self.assertEqual(payload["external_review_leads"], ["First anchored lead"])

    def test_final_editor_requests_all_anchored_chapter_findings_without_a_cap(self):
        contract = build_contract("trial", "run", CHAPTERS, created_at="now")
        chapter_payload = dossier_chapter_request(contract, "ch0001.md", CHAPTERS["ch0001.md"], {})
        book_payload = dossier_request(contract, [])

        rules = " ".join(chapter_payload["rules"])
        self.assertIn("every material finding", rules)
        self.assertIn("Do not cap", rules)
        self.assertNotIn("at most six material findings", " ".join(book_payload["rules"]))

    def test_final_editor_dossier_receives_automatic_editorial_dimensions_and_planning_authority(self):
        contract = build_contract("trial", "run", CHAPTERS, created_at="now")
        payload = dossier_chapter_request(
            contract, "ch0001.md", CHAPTERS["ch0001.md"],
            {"planning_authority": '{"canonical_characters":["Mara Voss"]}'},
        )
        self.assertIn("Mara Voss", payload["planning_authority"])
        self.assertIn("requires_human_decision must be false", " ".join(payload["rules"]))
        self.assertIn("opening pressure", " ".join(dossier_request(contract, [], {"planning_authority": "{}"})["rules"]))

    def test_per_chapter_plans_merge_with_json_word_delta_keys(self):
        class PerChapterPlanLLM(FakeLLM):
            def chat(self, role, messages, **kwargs):
                payload = json.loads(messages[-1]["content"])
                result = json.loads(super().chat(role, messages, **kwargs)["content"])
                if payload["task"].startswith("Return a repair plan"):
                    allowed = {finding["id"] for finding in payload["dossier"]["findings"]}
                    result["items"] = [
                        item for item in result["items"] if set(item["finding_ids"]) <= allowed
                    ]
                return {"content": json.dumps(result)}

        llm = PerChapterPlanLLM()
        contract = build_contract("trial", "run", CHAPTERS, created_at="now")
        dossier = json.loads(llm.chat("final_editor", [
            {"role": "user", "content": json.dumps({"task": "Return a whole-book dossier", "story_id": "trial"})}
        ])["content"])
        from final_editor_dossier import verify_dossier
        verified = verify_dossier(dossier, CHAPTERS, contract)

        plan = _build_verified_plan_by_chapter(llm, "final_editor", contract, verified)

        self.assertEqual(len(plan["items"]), 2)
        self.assertEqual(set(plan["aggregate_allowed_word_deltas"]), {1, 2})

    def test_reuse_accepts_same_source_from_an_earlier_run(self):
        old_contract = build_contract("trial", "old-run", CHAPTERS, created_at="now")
        new_contract = build_contract("trial", "new-run", CHAPTERS, created_at="now")
        packets = [{"chapter": 1}, {"chapter": 2}]
        with patch.object(Path, "is_file", return_value=True), \
             patch.object(Path, "read_text", side_effect=[json.dumps(old_contract), json.dumps(packets)]):
            reused = _load_reusable_packets(Path("old-artifacts"), new_contract, [1, 2])
        self.assertEqual(reused, packets)

    def test_reuse_rejects_a_changed_source_contract(self):
        old_contract = build_contract("trial", "old-run", CHAPTERS, created_at="now")
        changed = dict(CHAPTERS)
        changed["ch0002.md"] += " The evidence was burned."
        new_contract = build_contract("trial", "new-run", changed, created_at="now")
        packets = [{"chapter": 1}, {"chapter": 2}]
        with patch.object(Path, "is_file", return_value=True), \
             patch.object(Path, "read_text", side_effect=[json.dumps(old_contract), json.dumps(packets)]):
            reused = _load_reusable_packets(Path("old-artifacts"), new_contract, [1, 2])
        self.assertIsNone(reused)

    def test_plan_schema_retry_reuses_verified_dossier(self):
        class RetryPlanLLM(FakeLLM):
            def __init__(self):
                super().__init__()
                self.plan_calls = 0

            def chat(self, role, messages, **kwargs):
                payload = json.loads(messages[-1]["content"])
                if payload["task"].startswith("Return a repair plan"):
                    self.plan_calls += 1
                    response = json.loads(super().chat(role, messages, **kwargs)["content"])
                    if self.plan_calls == 1:
                        response["items"][0]["finding_ids"] = list(reversed(response["items"][0]["finding_ids"]))
                        response["items"][0]["finding_ids"].append(response["items"][0]["finding_ids"][0])
                    return {"content": json.dumps(response)}
                return super().chat(role, messages, **kwargs)

        llm = RetryPlanLLM()
        with patch("final_editor.read_chapters", return_value=copy.deepcopy(CHAPTERS)), \
             patch("final_editor.optional_artifacts", return_value={}), \
             patch("final_editor.write_json"), \
             patch("final_editor.copy_candidate_story", return_value=Path("candidate-story")), \
             patch("final_editor.fact_report", return_value={"findings": [], "status": "checked"}):
            detected = run_auto("trial", Path("source-story"), "run1", Path("artifacts"), llm)

        self.assertEqual(detected["status"], "accepted")
        self.assertEqual(llm.plan_calls, 2)

    def test_auto_accepts_isolated_unchanged_candidate_when_no_repairs_are_safe(self):
        class NoRepairLLM(FakeLLM):
            def chat(self, role, messages, **kwargs):
                payload = json.loads(messages[-1]["content"])
                if payload["task"].startswith("Return a whole-book dossier"):
                    result = json.loads(super().chat(role, messages, **kwargs)["content"])
                    result["findings"] = []
                    return {"content": json.dumps(result)}
                if payload["task"].startswith("Return a repair plan"):
                    return {"content": json.dumps({
                        "story_id": payload["story_id"],
                        "dossier_contract_sha256": payload["dossier_contract_sha256"],
                        "items": [],
                        "deferred": [],
                    })}
                return super().chat(role, messages, **kwargs)

        copied = {}
        with patch("final_editor.read_chapters", return_value=copy.deepcopy(CHAPTERS)), \
             patch("final_editor.optional_artifacts", return_value={}), \
             patch("final_editor.write_json"), \
             patch("final_editor.copy_candidate_story", side_effect=lambda source, target, texts: copied.update({"source": source, "target": target, "texts": copy.deepcopy(texts)}) or Path(target)), \
             patch("final_editor.fact_report", return_value={"findings": [], "status": "checked"}):
            result = run_auto("trial", Path("source-story"), "run1", Path("artifacts"), NoRepairLLM())

        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["verification"]["decision"], "accepted")
        self.assertEqual(
            result["verification"]["independent_review"]["protected_fact_assessments"],
            {"fact-ledger": "preserved", "fact-ward": "preserved"},
        )
        self.assertFalse(any(row["changed"] for row in result["verification"]["change_inventory"]))
        self.assertEqual(copied["texts"], CHAPTERS)

    def test_no_repair_candidate_still_fails_a_fact_contract_gate(self):
        class NoRepairLLM(FakeLLM):
            def chat(self, role, messages, **kwargs):
                payload = json.loads(messages[-1]["content"])
                if payload["task"].startswith("Return a whole-book dossier"):
                    result = json.loads(super().chat(role, messages, **kwargs)["content"])
                    result["findings"] = []
                    return {"content": json.dumps(result)}
                if payload["task"].startswith("Return a repair plan"):
                    return {"content": json.dumps({
                        "story_id": payload["story_id"],
                        "dossier_contract_sha256": payload["dossier_contract_sha256"],
                        "items": [],
                        "deferred": [],
                    })}
                return super().chat(role, messages, **kwargs)

        with patch("final_editor.read_chapters", return_value=copy.deepcopy(CHAPTERS)), \
             patch("final_editor.optional_artifacts", return_value={}), \
             patch("final_editor.write_json"), \
             patch("final_editor.copy_candidate_story", return_value=Path("candidate-story")), \
             patch("final_editor.fact_report", return_value={"findings": [{"severity": "blocking"}]}):
            result = run_auto("trial", Path("source-story"), "run1", Path("artifacts"), NoRepairLLM())

        self.assertEqual(result["status"], "rejected")
        self.assertIn("fact_contract_failed", result["verification"]["blockers"])

    def test_auto_builds_and_accepts_isolated_candidate_for_multiple_waves(self):
        original = copy.deepcopy(CHAPTERS)
        written = {}
        copied = {}
        llm = FakeLLM()

        def capture_candidate(source_dir, candidate_dir, candidate_texts):
            copied["source_dir"] = source_dir
            copied["candidate_dir"] = candidate_dir
            copied["texts"] = copy.deepcopy(candidate_texts)
            return Path(candidate_dir)

        with patch("final_editor.read_chapters", return_value=copy.deepcopy(CHAPTERS)), \
             patch("final_editor.optional_artifacts", return_value={}), \
             patch("final_editor.write_json", side_effect=lambda path, value: written.__setitem__(Path(path).name, copy.deepcopy(value))), \
             patch("final_editor.copy_candidate_story", side_effect=capture_candidate), \
             patch("final_editor.fact_report", return_value={"findings": [], "status": "checked"}):
            result = run_auto("trial", Path("source-story"), "run1", Path("artifacts"), llm)

        self.assertEqual(result["status"], "accepted")
        self.assertEqual(CHAPTERS, original)
        self.assertNotEqual(copied["texts"]["ch0001.md"], CHAPTERS["ch0001.md"])
        self.assertNotEqual(copied["texts"]["ch0002.md"], CHAPTERS["ch0002.md"])
        self.assertEqual(copied["source_dir"], Path("source-story"))
        self.assertEqual(copied["candidate_dir"], Path("trial_final_edit_run1"))
        self.assertEqual(written["verification.json"]["decision"], "accepted")
        self.assertEqual(len(written["candidate_audit.json"]["waves"]), 2)
        self.assertEqual(len(written["chapter_evidence_packets.json"]), 2)
        self.assertEqual(len(llm.calls), 8)
        revision_payloads = [payload for payload in llm.payloads if payload["task"].startswith("Return {plan_id, chapters}")]
        self.assertEqual([payload["adjacent_candidate_context"][0]["file"] for payload in revision_payloads], ["ch0002.md", "ch0001.md"])

    def test_remediation_rechecks_only_the_changed_unresolved_chapter(self):
        class RemediationLLM(FakeLLM):
            def __init__(self):
                super().__init__()
                self.reviewed_chapters = []

            def chat(self, role, messages, **kwargs):
                payload = json.loads(messages[-1]["content"])
                if payload["task"].startswith("Return {plan_id, chapters}") and payload.get("previous_validation_error"):
                    target = payload["targets"][0]
                    revised = target["text"].replace("entered quietly.", "entered alertly.")
                    return {"content": json.dumps({
                        "plan_id": payload["plan_id"],
                        "chapters": {
                            target["file"]: {
                                "source_sha256": target["source_sha256"],
                                "revised_text": revised,
                            }
                        },
                    })}
                result = super().chat(role, messages, **kwargs)
                if payload["task"].startswith("Return {reviewer_id, finding_assessments"):
                    chapter = payload["chapter"]
                    self.reviewed_chapters.append(chapter)
                    response = json.loads(result["content"])
                    if chapter == 1 and self.reviewed_chapters.count(1) == 1:
                        response["finding_assessments"][0]["status"] = "unresolved"
                    return {"content": json.dumps(response)}
                return result

        written = {}
        llm = RemediationLLM()
        original_is_file = Path.is_file
        original_read_text = Path.read_text

        def cached_is_file(path):
            if path.name == "independent_review_chapters.json":
                return path.name in written
            return original_is_file(path)

        def cached_read_text(path, *args, **kwargs):
            if path.name == "independent_review_chapters.json":
                return json.dumps(written[path.name])
            return original_read_text(path, *args, **kwargs)

        with patch("final_editor.read_chapters", return_value=copy.deepcopy(CHAPTERS)), \
             patch("final_editor.optional_artifacts", return_value={}), \
             patch("final_editor.write_json", side_effect=lambda path, value: written.__setitem__(Path(path).name, copy.deepcopy(value))), \
             patch("final_editor.copy_candidate_story", return_value=Path("candidate-story")), \
             patch.object(Path, "is_file", new=cached_is_file), \
             patch.object(Path, "read_text", new=cached_read_text), \
             patch.object(Path, "write_text", return_value=0), \
             patch("final_editor.fact_report", return_value={"findings": [], "status": "checked"}):
            result = run_auto("trial", Path("source-story"), "run1", Path("artifacts"), llm)

        self.assertEqual(result["status"], "accepted")
        self.assertEqual(llm.reviewed_chapters, [1, 2, 1])
        self.assertEqual(len([task for task in llm.calls if "native_check" in task]), 0)
        self.assertEqual(written["remediation_rounds.json"], [{"round": 1, "finding_ids": ["fe-001"], "decision": "accepted"}])
        self.assertEqual(len(result["verification"]["change_inventory"]), 2)
        self.assertEqual(result["verification"]["independent_review"]["assessments"]["fe-001"], "resolved")

    def test_auto_refuses_to_edit_without_verified_protected_facts(self):
        llm = FakeLLM(include_protected_facts=False)
        with patch("final_editor.read_chapters", return_value=copy.deepcopy(CHAPTERS)), \
             patch("final_editor.optional_artifacts", return_value={}), \
             patch("final_editor.write_json"):
            result = run_auto("trial", Path("source-story"), "run1", Path("artifacts"), llm)

        self.assertEqual(result["status"], "dossier_incomplete")
        self.assertEqual(result["reason"], "no_verified_protected_facts")
        self.assertEqual(len(llm.calls), 3)


if __name__ == "__main__":
    unittest.main()
