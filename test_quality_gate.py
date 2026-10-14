import hashlib
import json
import unittest
from pathlib import Path

import quality_gate


class QualityGateTests(unittest.TestCase):
    def test_chapter_acceptance_blocks_missing_outline_outcome(self):
        review = {
            "source_sha256": hashlib.sha256(b"Mara waits.").hexdigest(),
            "outcomes": [{"id": "c1", "status": "missing"}],
            "findings": [],
        }
        decision = quality_gate.chapter_acceptance_decision(review, "Mara waits.")
        self.assertEqual(decision["decision"], "repair_required")
        self.assertIn("outline outcome c1 is missing", decision["blockers"])

    def test_chapter_acceptance_blocks_every_verified_editorial_finding(self):
        review = {
            "source_sha256": hashlib.sha256(b"Mara waits.").hexdigest(),
            "outcomes": [{"id": "c1", "status": "met"}],
            "findings": [{"category": "duplicate_scene", "reason": "same reveal twice"}],
        }
        decision = quality_gate.chapter_acceptance_decision(review, "Mara waits.")
        self.assertEqual(decision["decision"], "repair_required")
        self.assertIn("duplicate_scene: same reveal twice", decision["blockers"])

    def test_chapter_acceptance_fails_closed_on_source_hash_mismatch(self):
        review = {"source_sha256": "0" * 64, "outcomes": [], "findings": []}
        decision = quality_gate.chapter_acceptance_decision(review, "Mara waits.")
        self.assertEqual(decision["decision"], "verification_failed")

    def test_chapter_acceptance_fails_closed_without_validated_shape(self):
        decision = quality_gate.chapter_acceptance_decision({"source_sha256": "x"}, "Mara waits.")
        self.assertEqual(decision["decision"], "verification_failed")

    def test_blind_review_is_preserved_as_evidence(self):
        report = quality_gate.build_report(
            "Her breath caught as the door opened.", 3,
            {"score": 3, "threshold": 4, "reason": "avoid stock reaction", "provider": "glm"},
        )
        self.assertEqual(report["blind_review"]["provider"], "glm")
        self.assertEqual(report["decision"], "repair_required")
        self.assertTrue(any(i["source"] == "blind_review" for i in report["issues"]))

    def test_local_finding_has_stable_anchor_and_id(self):
        text = "His pupils swallowed his irises."
        first = quality_gate.build_report(text, 1, {"score": 5, "threshold": 4})
        second = quality_gate.build_report(text, 1, {"score": 5, "threshold": 4})
        self.assertEqual(first["issues"][0]["issue_id"], second["issues"][0]["issue_id"])
        self.assertEqual(first["issues"][0]["line_start"], 1)

    def test_state_diff_is_explicit_when_states_are_supplied(self):
        text = "A."
        state = {
            "chapter": 1,
            "source_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "validation_status": "passed",
            "assertion_statuses": [], "non_regression_statuses": [],
            "scene_statuses": [], "fact_statuses": [],
            "relationship_changes": [], "open_hooks": [],
        }
        report = quality_gate.build_report(text, 1, {}, None, state)
        self.assertEqual(report["fact_diff"]["status"], "verified")
        self.assertEqual(report["fact_diff"]["changed"], [])
        self.assertTrue(report["evidence_complete"])

    def test_state_source_hash_mismatch_is_visible(self):
        report = quality_gate.build_report("A.", 1, {}, None, {
            "chapter": 1, "source_sha256": "0" * 64, "validation_status": "passed",
        })
        self.assertEqual(report["fact_diff"]["status"], "invalid")
        self.assertIn("current_state_source_hash_mismatch", report["fact_diff"]["changed"])

    def test_failed_state_delta_blocks_the_quality_gate(self):
        report = quality_gate.build_report(
            "A.", 1, {}, current_state={"validation_status": "failed"},
            state_failure="controlled state delta did not pass",
        )
        self.assertEqual(report["decision"], "repair_required")
        self.assertTrue(any(issue["source"] == "control_state" for issue in report["issues"]))

    def test_nonblocking_findings_do_not_claim_clean(self):
        report = quality_gate.build_report("His pupils swallowed his irises.", 1, {"score": 5, "threshold": 4})
        self.assertEqual(report["decision"], "review_required")

    def test_supernatural_reaction_review_blocks_with_exact_anchor(self):
        text = "His nails lengthened. Elara felt only a pulse of interest."
        blind = {
            "reason": "supernatural_reaction: review\nsupernatural_reaction_evidence: Elara felt only a pulse of interest.",
        }
        report = quality_gate.build_report(text, 1, blind)
        issue = next(item for item in report["issues"] if item["rule_id"] == "marketing-supernatural_reaction")
        self.assertEqual(report["decision"], "repair_required")
        self.assertEqual(issue["line_start"], 1)

    def test_relationship_basis_review_requires_editorial_review(self):
        text = "Mine, the bond seemed to say."
        blind = {
            "reason": "relationship_basis: review\nrelationship_basis_evidence: Mine, the bond seemed to say.",
        }
        report = quality_gate.build_report(text, 1, blind)
        self.assertEqual(report["decision"], "review_required")

    def test_marketing_review_strips_model_quote_marks_before_locating(self):
        text = "Elara felt only a pulse of interest."
        blind = {
            "reason": "supernatural_reaction: review\nsupernatural_reaction_evidence: \"Elara felt only a pulse of interest.\"",
        }
        report = quality_gate.build_report(text, 1, blind)
        issue = next(item for item in report["issues"] if item["rule_id"] == "marketing-supernatural_reaction")
        self.assertEqual(issue["excerpt"], text)
        self.assertNotIn("locator_status", issue)

    def test_report_round_trips_to_json(self):
        report = quality_gate.build_report("A.", 1, {})
        path = Path("tmp_quality_gate_test.json")
        try:
            quality_gate.write_report(path, report)
            loaded = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(loaded["schema"], "quality_gate_report_v1")
        finally:
            path.unlink(missing_ok=True)

    def test_controlled_revision_requires_new_state_validation(self):
        before = "The door opened."
        after = "The door swung open."
        old_state = {"source_sha256": hashlib.sha256(before.encode("utf-8")).hexdigest(), "validation_status": "passed"}
        manifest = quality_gate.build_revision_manifest(1, before, after, before_state=old_state)
        self.assertEqual(manifest["decision"], "rollback_required")
        self.assertTrue(manifest["state_revalidation"]["required"])

    def test_controlled_revision_passes_after_matching_state_validation(self):
        before, after = "The door opened.", "The door swung open."
        old_state = {"source_sha256": hashlib.sha256(before.encode("utf-8")).hexdigest(), "validation_status": "passed"}
        new_state = {"source_sha256": hashlib.sha256(after.encode("utf-8")).hexdigest(), "validation_status": "passed"}
        manifest = quality_gate.build_revision_manifest(1, before, after, before_state=old_state, after_state=new_state)
        self.assertEqual(manifest["decision"], "accepted")


if __name__ == "__main__":
    unittest.main()
