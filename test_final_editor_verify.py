import copy
import unittest

from final_editor_candidate import build_candidate
from final_editor_contract import build_contract
from final_editor_dossier import verify_dossier
from final_editor_plan import verify_plan
from final_editor import normalize_chapter_review_ids, preserve_unchanged_fact_assessments
from final_editor_verify import FinalEditorVerificationError, verify_candidate


def words(count, token):
    return " ".join([token] * count)


SOURCE = {"ch0001.md": words(100, "first"), "ch0002.md": words(100, "second")}


def setup():
    contract = build_contract("trial", "run1", SOURCE, created_at="now")
    dossier = verify_dossier({
        "story_id": "trial", "book_promise": "A forensic outsider investigates a dangerous inherited bond.",
        "voice_profile": "Close third-person prose uses concrete observation before emotional inference.",
        "chapter_roles": [
            {"chapter": 1, "function": "ward admission", "evidence_quotes": [{"file": "ch0001.md", "text": words(100, "first")}]},
            {"chapter": 2, "function": "evidence access", "evidence_quotes": [{"file": "ch0002.md", "text": words(100, "second")}]},
        ],
        "protected_facts": [{"id": "fact-001", "claim": "Mara's ward entry was authorized by the green palm scan.", "chapters": [1], "evidence_quotes": [{"file": "ch0001.md", "text": words(100, "first")}]}],
        "findings": [{"id": "fe-001", "root_cause": "The evidence handoff lacks a consequence.", "impact": "The investigation promise stalls after the source handoff.", "chapters": [2], "evidence_quotes": [{"file": "ch0002.md", "text": words(100, "second")}], "repair_scope": "scene", "requires_human_decision": False}],
    }, SOURCE, contract)
    plan = verify_plan({
        "story_id": "trial", "dossier_contract_sha256": dossier["contract_sha256"],
        "items": [{"id": "fp-001", "finding_ids": ["fe-001"], "chapter_targets": [2], "allowed_word_deltas": {"2": 0.05}, "editorial_goal": "Give the evidence handoff a concrete investigative consequence.", "expected_function_after": "The chapter ends with an active evidence route.", "editor_instructions": "Keep the source handoff while adding its direct consequence.", "editorial_decision": "Keep the handoff and turn it into the chapter's next concrete action.", "decision_basis": "The anchored handoff is the established evidence route and needs only a local consequence.", "decision_confidence": "high", "validation_checks": ["fact_contract", "word_budget", "change_audit", "independent_review"]}],
        "deferred": [],
    }, dossier, contract)
    from final_editor_contract import sha256_text
    candidate, audit = build_candidate(SOURCE, contract, dossier, plan, {"fp-001": {"plan_id": "fp-001", "chapters": {"ch0002.md": {"source_sha256": sha256_text(SOURCE["ch0002.md"]), "revised_text": words(104, "revised")}}}})
    return contract, dossier, plan, candidate, audit


def review(candidate, status="resolved", fact_status="preserved"):
    from final_editor_contract import sha256_text
    return {"reviewer_id": "independent-reviewer", "candidate_chapter_sha256": {name: sha256_text(text) for name, text in candidate.items()}, "blocking_findings": [], "finding_assessments": [{"finding_id": "fe-001", "status": status}], "protected_fact_assessments": [{"fact_id": "fact-001", "status": fact_status}]}


class FinalEditorVerificationTests(unittest.TestCase):
    def test_accepts_verified_candidate(self):
        contract, dossier, plan, candidate, audit = setup()
        result = verify_candidate(SOURCE, candidate, contract, dossier, plan, audit, {"findings": []}, review(candidate))
        self.assertEqual(result["decision"], "accepted")
        self.assertTrue(result["change_inventory"][1]["changed"])

    def test_rejects_unresolved_or_fact_breaking_candidate(self):
        contract, dossier, plan, candidate, audit = setup()
        unresolved = verify_candidate(SOURCE, candidate, contract, dossier, plan, audit, {"findings": []}, review(candidate, "unresolved"))
        self.assertEqual(unresolved["decision"], "rejected")
        self.assertIn("planned_findings_unresolved", unresolved["blockers"])

        broken = verify_candidate(SOURCE, candidate, contract, dossier, plan, audit, {"findings": [{"severity": "blocking"}]}, review(candidate))
        self.assertEqual(broken["decision"], "rejected")
        self.assertIn("fact_contract_failed", broken["blockers"])

        changed_fact = verify_candidate(SOURCE, candidate, contract, dossier, plan, audit, {"findings": []}, review(candidate, fact_status="changed"))
        self.assertEqual(changed_fact["decision"], "rejected")
        self.assertIn("protected_fact_changed", changed_fact["blockers"])

    def test_rejects_candidate_when_the_fact_contract_was_not_configured(self):
        contract, dossier, plan, candidate, audit = setup()
        result = verify_candidate(
            SOURCE, candidate, contract, dossier, plan, audit,
            {"findings": [], "status": "missing_fact_contract"}, review(candidate),
        )
        self.assertEqual(result["decision"], "rejected")
        self.assertIn("fact_contract_not_configured", result["blockers"])

    def test_rejects_stale_audit_or_incomplete_review(self):
        contract, dossier, plan, candidate, audit = setup()
        stale = copy.deepcopy(audit)
        stale["candidate_chapter_sha256"]["ch0002.md"] = "0" * 64
        with self.assertRaisesRegex(FinalEditorVerificationError, "hashes"):
            verify_candidate(SOURCE, candidate, contract, dossier, plan, stale, {"findings": []}, review(candidate))

        incomplete = review(candidate)
        incomplete["finding_assessments"] = []
        with self.assertRaisesRegex(FinalEditorVerificationError, "assess every"):
            verify_candidate(SOURCE, candidate, contract, dossier, plan, audit, {"findings": []}, incomplete)

    def test_normalizes_compact_reviewer_ids_only_when_expected(self):
        compact = {
            "finding_assessments": [{"id": "fe-001", "status": "resolved"}, {"id": "wrong", "status": "resolved"}],
            "protected_fact_assessments": [{"protected_fact_id": "fact-001", "status": "preserved"}, {"id": "wrong", "status": "preserved"}],
        }
        normalized = normalize_chapter_review_ids(compact, ["fe-001"], ["fact-001"])
        self.assertEqual(normalized["finding_assessments"][0]["finding_id"], "fe-001")
        self.assertNotIn("finding_id", normalized["finding_assessments"][1])
        self.assertEqual(normalized["protected_fact_assessments"][0]["fact_id"], "fact-001")
        self.assertNotIn("fact_id", normalized["protected_fact_assessments"][1])

    def test_rejects_structured_blocking_finding(self):
        contract, dossier, plan, candidate, audit = setup()
        independent = review(candidate)
        independent["blocking_findings"] = [{"location": "ch0002.md", "defect": "new contradiction"}]
        result = verify_candidate(SOURCE, candidate, contract, dossier, plan, audit, {"findings": []}, independent)
        self.assertEqual(result["decision"], "rejected")
        self.assertIn("independent_review_blocking_findings", result["blockers"])

    def test_marks_unchanged_protected_fact_as_preserved_without_model_review(self):
        contract, dossier, _plan, candidate, _audit = setup()
        merged_review = {
            "protected_fact_assessments": [],
            "candidate_chapter_sha256": {},
            "finding_assessments": [],
            "blocking_findings": [],
        }
        normalized = preserve_unchanged_fact_assessments(merged_review, contract, dossier, candidate)
        self.assertEqual(normalized["protected_fact_assessments"], [{
            "fact_id": "fact-001", "status": "preserved",
            "evidence": "deterministic: every chapter containing this protected fact is byte-for-byte unchanged from the source contract",
        }])

        changed = dict(candidate)
        changed["ch0001.md"] += " changed"
        self.assertEqual(
            preserve_unchanged_fact_assessments(merged_review, contract, dossier, changed)["protected_fact_assessments"],
            [],
        )

    def test_rejects_invalid_remediation_audit_wave(self):
        contract, dossier, plan, candidate, audit = setup()
        audit["remediation_waves"] = [{"status": "rejected"}]
        with self.assertRaisesRegex(FinalEditorVerificationError, "remediation wave"):
            verify_candidate(SOURCE, candidate, contract, dossier, plan, audit, {"findings": []}, review(candidate))


if __name__ == "__main__":
    unittest.main()
