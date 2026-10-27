import copy
import json
import unittest

from final_editor_candidate import FinalEditorCandidateError, build_candidate, candidate_diff_summary
from final_editor_contract import build_contract, sha256_text
from final_editor_dossier import verify_dossier
from final_editor_plan import verify_plan


def words(count, token):
    return " ".join([token] * count)


CHAPTERS = {
    "ch0001.md": words(100, "first"),
    "ch0002.md": words(100, "second"),
}


def setup():
    contract = build_contract("trial", "run1", CHAPTERS, created_at="now")
    dossier = verify_dossier({
        "story_id": "trial", "book_promise": "A forensic outsider investigates a dangerous inherited bond.",
        "voice_profile": "Close third-person prose uses concrete observation before emotional inference.",
        "chapter_roles": [
            {"chapter": 1, "function": "ward admission", "evidence_quotes": [{"file": "ch0001.md", "text": words(100, "first")}]},
            {"chapter": 2, "function": "evidence access", "evidence_quotes": [{"file": "ch0002.md", "text": words(100, "second")}]},
        ],
        "findings": [{
            "id": "fe-001", "root_cause": "The evidence handoff lacks a consequence.",
            "impact": "The investigation promise stalls after the source handoff.", "chapters": [2],
            "evidence_quotes": [{"file": "ch0002.md", "text": words(100, "second")}],
            "repair_scope": "scene", "requires_human_decision": False,
        }],
    }, CHAPTERS, contract)
    plan = verify_plan({
        "story_id": "trial", "dossier_contract_sha256": dossier["contract_sha256"],
        "items": [{
            "id": "fp-001", "finding_ids": ["fe-001"], "chapter_targets": [2],
            "allowed_word_deltas": {"2": 0.05},
            "editorial_goal": "Give the evidence handoff a concrete investigative consequence.",
            "expected_function_after": "The chapter ends with an active evidence route.",
            "editor_instructions": "Keep the source handoff while adding its direct consequence.",
            "editorial_decision": "Keep the handoff and turn it into the chapter's next concrete action.",
            "decision_basis": "The anchored handoff is the established evidence route and needs only a local consequence.",
            "decision_confidence": "high",
            "validation_checks": ["fact_contract", "word_budget", "change_audit", "independent_review"],
        }], "deferred": [],
    }, dossier, contract)
    return contract, dossier, plan


def revisions(source=CHAPTERS):
    return {"fp-001": {
        "plan_id": "fp-001",
        "chapters": {"ch0002.md": {
            "source_sha256": sha256_text(source["ch0002.md"]),
            "revised_text": words(104, "revised"),
        }},
    }}


class FinalEditorCandidateTests(unittest.TestCase):
    def test_builds_candidate_without_mutating_source(self):
        contract, dossier, plan = setup()
        original = dict(CHAPTERS)
        candidate, audit = build_candidate(CHAPTERS, contract, dossier, plan, revisions())
        self.assertEqual(CHAPTERS, original)
        self.assertNotEqual(candidate["ch0002.md"], CHAPTERS["ch0002.md"])
        self.assertTrue(audit["word_budget"]["accepted"])
        self.assertEqual(audit["status"], "ready_for_independent_verification")

    def test_builds_candidate_from_json_round_tripped_plan(self):
        contract, dossier, plan = setup()
        persisted_plan = json.loads(json.dumps(plan))
        candidate, audit = build_candidate(CHAPTERS, contract, dossier, persisted_plan, revisions())
        self.assertNotEqual(candidate["ch0002.md"], CHAPTERS["ch0002.md"])
        self.assertEqual(audit["waves"][0]["status"], "applied")

    def test_rejects_unplanned_files_stale_hash_and_word_budget_breach(self):
        contract, dossier, plan = setup()
        extra = revisions()
        extra["fp-001"]["chapters"]["ch0001.md"] = {"source_sha256": sha256_text(CHAPTERS["ch0001.md"]), "revised_text": words(100, "other")}
        with self.assertRaisesRegex(FinalEditorCandidateError, "chapter targets"):
            build_candidate(CHAPTERS, contract, dossier, plan, extra)

        stale = revisions()
        stale["fp-001"]["chapters"]["ch0002.md"]["source_sha256"] = "0" * 64
        with self.assertRaisesRegex(FinalEditorCandidateError, "source hash"):
            build_candidate(CHAPTERS, contract, dossier, plan, stale)

        oversized = revisions()
        oversized["fp-001"]["chapters"]["ch0002.md"]["revised_text"] = words(106, "large")
        with self.assertRaisesRegex(FinalEditorCandidateError, "word delta"):
            build_candidate(CHAPTERS, contract, dossier, plan, oversized)

    def test_rejects_changed_source_and_missing_plan_revision(self):
        contract, dossier, plan = setup()
        changed_source = dict(CHAPTERS)
        changed_source["ch0001.md"] = words(100, "changed")
        with self.assertRaisesRegex(FinalEditorCandidateError, "source chapter changed"):
            build_candidate(changed_source, contract, dossier, plan, revisions(changed_source))

        with self.assertRaisesRegex(FinalEditorCandidateError, "exactly one revision"):
            build_candidate(CHAPTERS, contract, dossier, plan, {})

    def test_rejects_dossier_from_a_different_contract(self):
        contract, dossier, plan = setup()
        other_contract = build_contract("trial", "run2", CHAPTERS, created_at="later")
        with self.assertRaisesRegex(FinalEditorCandidateError, "dossier is not linked"):
            build_candidate(CHAPTERS, other_contract, dossier, plan, revisions())

    def test_diff_summary_is_file_bound(self):
        candidate = dict(CHAPTERS)
        candidate["ch0002.md"] = words(104, "revised")
        rows = candidate_diff_summary(CHAPTERS, candidate)
        self.assertFalse(rows[0]["changed"])
        self.assertTrue(rows[1]["changed"])
        with self.assertRaisesRegex(FinalEditorCandidateError, "chapter sets"):
            candidate_diff_summary(CHAPTERS, {"ch0001.md": "only"})


if __name__ == "__main__":
    unittest.main()
