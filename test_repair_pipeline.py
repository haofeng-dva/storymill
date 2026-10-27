import hashlib
import json
import unittest
from unittest.mock import patch
from pathlib import Path

from repair_pipeline import anchors_from_excerpts, locate_issue, resume_cards, split_issues
from apply_repair_drafts import apply_cards_to_text, eligible, revision_evidence
from round2_repair import apply_ops


class RepairPipelineTests(unittest.TestCase):
    def test_split_issues_skips_scores(self):
        content = "1. First issue.\n\n2. Second issue.\n\n- native_score: 4"
        self.assertEqual(split_issues(content), ["First issue.", "Second issue."])

    def test_split_issues_removes_inline_score_tail(self):
        content = "1. First issue.\n\n2. Second issue. `native_score`: 4/5 `engagement_score`: 4/5"
        self.assertEqual(split_issues(content), ["First issue.", "Second issue."])

    def test_split_issues_removes_scores_heading(self):
        content = "1. First issue.\n\n**Scores**\n- `native_score`: 4/5"
        self.assertEqual(split_issues(content), ["First issue."])

    def test_locator_returns_exact_coordinates_for_unique_quote(self):
        text = "One line.\nThe black mark beneath his ribs pulsed once.\nLast line."
        issue = "The phrase \"The black mark beneath his ribs pulsed once\" repeats."
        anchors = locate_issue(text, issue)
        self.assertEqual(len(anchors), 1)
        self.assertEqual(anchors[0]["line_start"], 2)
        self.assertEqual(anchors[0]["excerpt"], "The black mark beneath his ribs pulsed once")

    def test_locator_rejects_ambiguous_quote(self):
        text = "Thunder rolled.\nThunder rolled."
        issue = "The phrase \"Thunder rolled\" repeats."
        self.assertEqual(locate_issue(text, issue), [])

    def test_model_excerpts_must_match_once_exactly(self):
        text = "Alpha detail.\nBeta detail.\nAlpha detail."
        anchors = anchors_from_excerpts(text, ["Beta detail.", "Alpha detail.", "missing"])
        self.assertEqual(len(anchors), 1)
        self.assertEqual(anchors[0]["line_start"], 2)

    def test_resume_preserves_model_output_only_for_same_hash(self):
        payload = {"cards": [{"id": "ch0001-i01", "file_sha256": "current", "anchors": [], "planning_status": "pending"}]}
        existing = {"cards": [{"id": "ch0001-i01", "file_sha256": "current", "anchors": [{"excerpt": "exact"}], "locator_status": "model_located", "planning_status": "planned"}]}
        with patch("repair_pipeline.Path.is_file", return_value=True), patch(
            "repair_pipeline.Path.read_text", return_value=json.dumps(existing)
        ):
            resumed = resume_cards(payload, "cards.json")
        card = resumed["cards"][0]
        self.assertEqual(card["locator_status"], "model_located")
        self.assertEqual(card["planning_status"], "planned")

    def test_apply_replaces_only_unique_verified_draft(self):
        card = {"id": "ch0001-i01", "draft": {"find": "old sentence", "replace": "new sentence"}}
        text, audit = apply_cards_to_text("First. old sentence. Last.", [card])
        self.assertEqual(text, "First. new sentence. Last.")
        self.assertEqual(audit[0]["status"], "applied")

    def test_unplanned_card_is_not_a_valid_auto_apply_candidate(self):
        card = {"draft_status": "drafted", "anchors": [{"excerpt": "old"}], "draft": {"find": "old", "replace": "new"}}
        self.assertFalse(eligible(card))

    def test_noop_draft_is_not_eligible(self):
        card = {
            "draft_status": "drafted", "anchors": [{"excerpt": "same"}],
            "draft": {"find": "same", "replace": "same"},
            "plan": {"change_strategy": "replace", "allowed_scope": "one sentence", "dependencies": [], "requires_human_decision": False},
        }
        self.assertFalse(eligible(card))

    def test_round2_apply_rejects_ambiguous_find(self):
        text, audit = apply_ops("Same. Same.", [{"find": "Same.", "replace": "Different."}])
        self.assertEqual(text, "Same. Same.")
        self.assertEqual(audit[0]["status"], "rejected")

    def test_controlled_repair_requires_state_revalidation(self):
        before = "First. old sentence. Last."
        after = "First. new sentence. Last."
        state = {
            "source_sha256": hashlib.sha256(before.encode("utf-8")).hexdigest(),
            "validation_status": "passed",
        }
        with patch("apply_repair_drafts._load_json", side_effect=[None, state]):
            manifest, _ = revision_evidence(Path("story"), "ch0001.md", before, after)
        self.assertEqual(manifest["decision"], "rollback_required")
        self.assertTrue(manifest["state_revalidation"]["required"])


if __name__ == "__main__":
    unittest.main()
