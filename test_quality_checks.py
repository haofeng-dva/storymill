import unittest

import quality_checks


class RedundancyCheckTests(unittest.TestCase):
    def test_flags_repeated_fragment_construction(self):
        text = "Not hope. Hope was too dangerous. Later, not hope. Hope was too naked."
        warnings = quality_checks.redundancy_check(text)
        self.assertTrue(any("Not hope" in warning for warning in warnings))

    def test_allows_one_fragment_construction(self):
        text = "Not hope. Hope was too dangerous a word in this room."
        self.assertEqual(quality_checks.redundancy_check(text), [])

    def test_flags_repeated_stock_beat_but_not_one_use(self):
        repeated = "Thunder rolled over the trees. Later, thunder rolled over the road."
        self.assertIn("repeated thunder rolled x2", quality_checks.redundancy_check(repeated))
        self.assertEqual(quality_checks.redundancy_check("Thunder rolled over the trees."), [])

    def test_style_lint_flags_strict_template_rules(self):
        warnings = quality_checks.style_lint("His pupils swallowed his irises. Her breath caught.")
        self.assertTrue(any("pupil-swallowing" in warning for warning in warnings))
        self.assertTrue(any("breath-caught" in warning for warning in warnings))

    def test_style_lint_flags_environment_closing_in_template(self):
        warnings = quality_checks.style_lint("The room seemed to close in around him.")
        self.assertTrue(any("environment-closing-in" in warning for warning in warnings))

    def test_strict_prose_violations_are_zero_tolerance(self):
        self.assertTrue(quality_checks.strict_prose_violations("Not fear. Not love. A mechanism."))

    def test_strict_repair_anchor_is_one_unique_sentence(self):
        text = "The corridor was quiet. Her breath caught as the door opened. She kept walking."
        anchors = quality_checks.strict_repair_anchors(text)
        self.assertEqual(anchors, [{
            "rule_id": "breath-caught shorthand",
            "rule_ids": ["breath-caught shorthand"],
            "excerpt": "Her breath caught as the door opened.",
        }])

    def test_strict_repair_anchor_groups_rules_on_one_sentence_and_keeps_a_triad_intact(self):
        text = "Her breath caught while the wolf stirred beneath her skin. Not fear. Not love. A mechanism."
        anchors = quality_checks.strict_repair_anchors(text)
        self.assertEqual(anchors[0]["rule_ids"], [
            "wolf-beneath-skin shorthand", "breath-caught shorthand",
        ])
        self.assertEqual(anchors[1], {
            "rule_id": "trailer fragment triad",
            "rule_ids": ["trailer fragment triad"],
            "excerpt": "Not fear. Not love. A mechanism.",
        })

    def test_integrity_lint_flags_joined_dialogue_unmatched_quote_and_future_spoiler(self):
        text = '"Yes."Ronan left.\n"Broken dialogue\nShe did not yet know that Ronan would betray her.'
        rule_ids = {item["rule_id"] for item in quality_checks.structured_quality_findings(text)}
        self.assertTrue({"joined-dialogue-and-narration", "unmatched-dialogue-quote", "future-spoiler-narration"} <= rule_ids)

    def test_integrity_breaks_are_strict_acceptance_blockers(self):
        text = '"For them.The answer was hard.\n“Broken dialogue'
        violations = quality_checks.strict_prose_violations(text)
        self.assertTrue(any("missing-space-after-terminal-punctuation" in item for item in violations))
        self.assertTrue(any("unmatched-dialogue-quote" in item for item in violations))

    def test_deterministic_integrity_repair_inserts_only_the_missing_space(self):
        repaired, count = quality_checks.repair_deterministic_integrity("For them.The answer was hard.")
        self.assertEqual(count, 1)
        self.assertEqual(repaired, "For them. The answer was hard.")

    def test_entity_lint_only_flags_a_new_surname_for_a_known_full_name(self):
        findings = quality_checks.unregistered_entity_findings("Mara Bell crossed paths with Ronan Vale.", ["Mara Voss", "Ronan Vale"])
        self.assertEqual([item["rule_id"] for item in findings], ["unregistered-entity"])
        self.assertIn("Mara Bell", findings[0]["excerpt"])

    def test_entity_lint_uses_the_production_style_roster_without_article_false_positive(self):
        roster = ["Elias Ward", "Ronan Vale", "Daniel Ward", "Mara", "Councilor Voss", "The Former Alpha"]
        findings = quality_checks.unregistered_entity_findings(
            "Mara Bell read The March incident records. Councilor Vey interrupted.", roster
        )
        self.assertEqual([item["excerpt"] for item in findings], ["Mara Bell", "Councilor Vey"])

    def test_cross_paragraph_continuing_dialogue_is_not_an_unmatched_quote(self):
        text = '"I need you to listen.\n\n"Because this is the only way."'
        rule_ids = {item["rule_id"] for item in quality_checks.structured_quality_findings(text)}
        self.assertNotIn("unmatched-dialogue-quote", rule_ids)

    def test_adjacent_complete_curly_dialogue_paragraphs_are_not_unmatched(self):
        text = '“I need you to listen.”\n\n“Then tell me what you heard.”'
        rule_ids = {item["rule_id"] for item in quality_checks.structured_quality_findings(text)}
        self.assertNotIn("unmatched-dialogue-quote", rule_ids)


if __name__ == "__main__":
    unittest.main()
