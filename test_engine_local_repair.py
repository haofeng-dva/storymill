import json
import unittest
from unittest.mock import Mock

from engine import WritingEngine
from repair_validator import apply_scoped_patches


class StrictLocalRepairTests(unittest.TestCase):
    def test_repairs_only_the_verified_sentence_and_clears_rule(self):
        engine = object.__new__(WritingEngine)
        engine.llm = Mock()
        source = "The corridor was quiet. Her breath caught as the door opened. She kept walking."
        engine.llm.chat.return_value = {"content": json.dumps({
            "find": "Her breath caught as the door opened.",
            "replace": "She paused as the door opened.",
        })}

        result = engine.repair_strict_prose(source)

        self.assertTrue(result["repaired"])
        self.assertEqual(
            result["text"],
            "The corridor was quiet. She paused as the door opened. She kept walking.",
        )
        self.assertEqual(result["rules"], ["breath-caught shorthand"])

    def test_rejects_draft_that_does_not_repeat_the_exact_anchor(self):
        engine = object.__new__(WritingEngine)
        engine.llm = Mock()
        engine.llm.chat.return_value = {"content": json.dumps({
            "find": "different sentence.", "replace": "A replacement."
        })}

        result = engine.repair_strict_prose("Her breath caught as the door opened.")

        self.assertFalse(result["repaired"])
        self.assertIn("unverified", result["reason"])

    def test_acceptance_patches_preserve_text_outside_verified_anchor(self):
        before = "Opening stays. The cited sentence is wrong. Ending stays."
        result = apply_scoped_patches(
            before,
            [{"find": "The cited sentence is wrong.", "replace": "The cited sentence is precise."}],
            ["The cited sentence is wrong."],
        )
        self.assertTrue(result["accepted"])
        self.assertEqual(result["text"], "Opening stays. The cited sentence is precise. Ending stays.")

    def test_acceptance_patches_reject_unverified_edit_range(self):
        before = "Opening stays. The cited sentence is wrong. Ending stays."
        result = apply_scoped_patches(
            before,
            [{"find": "Ending stays.", "replace": "Ending changed."}],
            ["The cited sentence is wrong."],
        )
        self.assertFalse(result["accepted"])
        self.assertEqual(result["text"], before)
        self.assertIn("outside verified repair anchors", result["errors"][0])

    def test_acceptance_patches_reject_ambiguous_anchor(self):
        before = "Repeated detail. Repeated detail."
        result = apply_scoped_patches(
            before,
            [{"find": "Repeated detail.", "replace": "Corrected detail."}],
            ["Repeated detail."],
        )
        self.assertFalse(result["accepted"])
        self.assertEqual(result["text"], before)
        self.assertIn("not uniquely locatable", result["errors"][0])

    def test_acceptance_patches_reject_chapter_sized_replacement(self):
        before = "Opening stays. The cited sentence is wrong. Ending stays."
        result = apply_scoped_patches(
            before,
            [{"find": "The cited sentence is wrong.", "replace": "word " * 121}],
            ["The cited sentence is wrong."],
        )
        self.assertFalse(result["accepted"])
        self.assertEqual(result["text"], before)
        self.assertIn("exceeds its verified repair scope", result["errors"][0])

    def test_missing_acceptance_outcome_fails_without_model_rewrite(self):
        engine = object.__new__(WritingEngine)
        engine.llm = Mock()

        result = engine.repair_chapter_with_verified_patches(
            "Existing chapter.",
            {"outcomes": [{"id": "ch0001-requirement-01", "status": "missing"}], "findings": []},
        )

        self.assertIn("no verified edit anchor", result["error"])
        engine.llm.chat.assert_not_called()


if __name__ == "__main__":
    unittest.main()
