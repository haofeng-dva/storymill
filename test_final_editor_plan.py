import copy
import unittest

from final_editor_contract import build_contract
from final_editor_dossier import verify_dossier
from final_editor_plan import FinalEditorPlanError, plan_is_usable, verify_plan


CHAPTERS = {
    "ch0001.md": "Mara crosses the ward after the green light scans her palm. Rafe watches from the gate.",
    "ch0002.md": "Mara records the collar number. Soren gives her the records-room key.",
}


def contract():
    return build_contract("trial", "run1", CHAPTERS, created_at="2026-09-18T00:00:00Z")


def dossier():
    return verify_dossier({
        "story_id": "trial",
        "book_promise": "A forensic outsider investigates a dangerous inherited bond.",
        "voice_profile": "Close third-person prose uses concrete observation before emotional inference.",
        "chapter_roles": [
            {"chapter": 1, "function": "ward admission", "evidence_quotes": [{"file": "ch0001.md", "text": "Mara crosses the ward after the green light scans her palm."}]},
            {"chapter": 2, "function": "evidence access", "evidence_quotes": [{"file": "ch0002.md", "text": "Soren gives her the records-room key."}]},
        ],
        "findings": [{
            "id": "fe-001", "root_cause": "The records access lacks an investigative consequence.",
            "impact": "The evidence-led story promise stalls after the key handoff.", "chapters": [2],
            "evidence_quotes": [{"file": "ch0002.md", "text": "Soren gives her the records-room key."}],
            "repair_scope": "scene", "requires_human_decision": False,
        }],
    }, CHAPTERS, contract())


def proposal(dossier_value):
    return {
        "story_id": "trial",
        "dossier_contract_sha256": dossier_value["contract_sha256"],
        "items": [{
            "id": "fp-001", "finding_ids": ["fe-001"], "chapter_targets": [2],
            "allowed_word_deltas": {"2": 0.05},
            "editorial_goal": "Show the key producing an immediate evidence-led next action.",
            "expected_function_after": "The chapter ends with a concrete records investigation route.",
            "editor_instructions": "Retain the key handoff and add its direct investigative consequence.",
            "editorial_decision": "Keep the records key as the evidence route and clarify its immediate use.",
            "decision_basis": "The chapter's anchored key handoff already establishes the correct investigative direction.",
            "decision_confidence": "high",
            "validation_checks": ["fact_contract", "word_budget", "change_audit", "independent_review"],
        }],
        "deferred": [],
    }


class FinalEditorPlanTests(unittest.TestCase):
    def test_verifies_bounded_candidate_only_plan(self):
        dossier_value = dossier()
        plan = verify_plan(proposal(dossier_value), dossier_value, contract())
        self.assertTrue(plan_is_usable(plan))
        self.assertEqual(plan["items"][0]["status"], "editorial_decision_approved_for_candidate_only")
        self.assertEqual(plan["aggregate_allowed_word_deltas"], {2: 0.05})

    def test_rejects_unplanned_findings_and_expanded_targets(self):
        dossier_value = dossier()
        unplanned = proposal(dossier_value)
        unplanned["items"] = []
        with self.assertRaisesRegex(FinalEditorPlanError, "every verified finding"):
            verify_plan(unplanned, dossier_value, contract())

        expanded = proposal(dossier_value)
        expanded["items"][0]["chapter_targets"] = [1, 2]
        expanded["items"][0]["allowed_word_deltas"] = {"1": 0.01, "2": 0.05}
        with self.assertRaisesRegex(FinalEditorPlanError, "chapter_targets"):
            verify_plan(expanded, dossier_value, contract())

    def test_rejects_budget_overflow_and_requires_editorial_decision(self):
        dossier_value = dossier()
        too_large = proposal(dossier_value)
        too_large["items"][0]["allowed_word_deltas"] = {"2": 0.26}
        with self.assertRaisesRegex(FinalEditorPlanError, "allowed_word_deltas"):
            verify_plan(too_large, dossier_value, contract())

        human_dossier = copy.deepcopy(dossier_value)
        human_dossier["findings"][0]["requires_human_decision"] = True
        plan = verify_plan(proposal(human_dossier), human_dossier, contract())
        self.assertEqual(plan["items"][0]["decision_confidence"], "high")

        missing_decision = proposal(dossier_value)
        del missing_decision["items"][0]["editorial_decision"]
        with self.assertRaisesRegex(FinalEditorPlanError, "editorial_decision"):
            verify_plan(missing_decision, dossier_value, contract())

        deferred = proposal(dossier_value)
        deferred["items"] = []
        deferred["deferred"] = [{"finding_ids": ["fe-001"], "reason": "Needs editorial judgment."}]
        with self.assertRaisesRegex(FinalEditorPlanError, "deferred must be empty"):
            verify_plan(deferred, dossier_value, contract())

    def test_rejects_stale_dossier_link_and_duplicate_disposition(self):
        dossier_value = dossier()
        stale = proposal(dossier_value)
        stale["dossier_contract_sha256"] = "0" * 64
        with self.assertRaisesRegex(FinalEditorPlanError, "dossier_contract"):
            verify_plan(stale, dossier_value, contract())

        duplicate = proposal(dossier_value)
        duplicate["deferred"] = [{"finding_ids": ["fe-001"], "reason": "Needs a separate editorial choice."}]
        with self.assertRaisesRegex(FinalEditorPlanError, "deferred must be empty"):
            verify_plan(duplicate, dossier_value, contract())

    def test_rejects_overlapping_chapter_items(self):
        dossier_value = dossier()
        related = copy.deepcopy(dossier_value["findings"][0])
        related["id"] = "fe-002"
        dossier_value["findings"].append(related)
        overlapping = proposal(dossier_value)
        second = copy.deepcopy(overlapping["items"][0])
        second["id"] = "fp-002"
        second["finding_ids"] = ["fe-002"]
        overlapping["items"].append(second)
        with self.assertRaisesRegex(FinalEditorPlanError, "belongs to both"):
            verify_plan(overlapping, dossier_value, contract())


if __name__ == "__main__":
    unittest.main()
