import unittest

from quality_checks import structured_quality_findings
from story_fact_contract import check_text


class StoryFactContractTests(unittest.TestCase):
    def test_prior_evidence_finds_missing_ward_action(self):
        facts = {"facts": [{
            "id": "ward", "canonical_fact": "A ward scans Mara's palm first.", "chapters": [1],
            "checks": [{"kind": "prior_evidence", "trigger": "scanned my palm", "evidence": "green light", "message": "missing action"}],
        }]}
        findings = check_text(facts, 1, "The stone glowed blue.\nIt scanned my palm and let me pass.")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["line_start"], 2)
        self.assertEqual(findings[0]["fact_id"], "ward")

    def test_prior_evidence_accepts_shown_action(self):
        facts = {"facts": [{
            "id": "ward", "canonical_fact": "A ward scans Mara's palm first.", "chapters": [1],
            "checks": [{"kind": "prior_evidence", "trigger": "scanned my palm", "evidence": "green light", "message": "missing action"}],
        }]}
        text = "Green light crossed Mara's palm.\nIt scanned my palm and let me pass."
        self.assertEqual(check_text(facts, 1, text), [])

    def test_forbid_before_respects_boundary(self):
        facts = {"facts": [{
            "id": "archives", "canonical_fact": "Two locations have distinct names.", "chapters": [9],
            "checks": [{"kind": "forbid_before", "pattern": "The archive smelled", "before": "archive is beneath", "message": "wrong name"}],
        }]}
        findings = check_text(facts, 9, "The archive smelled of dust. The archive is beneath the old hall.")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["excerpt"], "The archive smelled")

    def test_local_lint_has_coordinates_and_category(self):
        findings = structured_quality_findings("Mara removed a sterile light.\nThe speaker clicked off.")
        self.assertEqual([item["rule_id"] for item in findings], ["sterile-light-device", "ambiguous-speaker-clicked-off"])
        self.assertEqual(findings[0]["line_start"], 1)
        self.assertEqual(findings[1]["category"], "language_clarity")


if __name__ == "__main__":
    unittest.main()
