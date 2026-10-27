import copy
import unittest

from final_editor_contract import (
    FinalEditorContractError,
    build_contract,
    candidate_story_id,
    chapter_manifest,
    validate_contract,
    word_budget_report,
)


def words(count, token="word"):
    return " ".join([token] * count)


def source_texts():
    return {
        "ch0001.md": words(100, "first"),
        "ch0002.md": words(100, "second"),
    }


class FinalEditorContractTests(unittest.TestCase):
    def test_candidate_id_is_stable_and_safe_for_nested_repair_rounds(self):
        story_id = "lgbt_romance_final_editor_20261008_final_edit_lgbt_editor_decision_20261009d"
        run_id = "lgbt_editor_full_repair_20261009e"

        value = candidate_story_id(story_id, run_id)

        self.assertLessEqual(len(value), 128)
        self.assertRegex(value, r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
        self.assertEqual(value, candidate_story_id(story_id, run_id))
        self.assertNotEqual(value, candidate_story_id(story_id, run_id + "_r2"))

    def test_builds_a_locked_candidate_only_contract(self):
        contract = build_contract(
            "trial", "r1", source_texts(), created_at="2026-09-18T00:00:00Z",
            source_artifacts={"outline": "outline", "world_bible": "bible"},
        )
        self.assertEqual(contract["source"]["chapter_count"], 2)
        self.assertEqual(contract["source"]["book_words"], 200)
        self.assertEqual(contract["candidate"]["story_id"], "trial_final_edit_r1")
        self.assertEqual(contract["candidate"]["source_mutation"], "forbidden")
        self.assertTrue(all(contract["locked_invariants"].values()))

    def test_rejects_missing_reordered_or_changed_source_manifest(self):
        contract = build_contract("trial", "r1", source_texts(), created_at="now")

        missing = copy.deepcopy(contract)
        missing["source"]["chapters"] = missing["source"]["chapters"][:1]
        missing["source"]["chapter_count"] = 1
        missing["source"]["book_words"] = 100
        with self.assertRaisesRegex(FinalEditorContractError, "manifest changed"):
            validate_contract(missing, expected_manifest=contract["source"]["chapters"])

        changed = copy.deepcopy(contract)
        changed["source"]["chapters"][1]["source_sha256"] = "0" * 64
        with self.assertRaisesRegex(FinalEditorContractError, "manifest changed"):
            validate_contract(changed, expected_manifest=contract["source"]["chapters"])

        with self.assertRaisesRegex(FinalEditorContractError, "contiguous"):
            chapter_manifest({"ch0002.md": words(10)})

    def test_rejects_budget_relaxation_and_unlocked_source_mutation(self):
        contract = build_contract("trial", "r1", source_texts(), created_at="now")
        relaxed = copy.deepcopy(contract)
        relaxed["word_budget"]["per_chapter_max_delta"] = 0.26
        with self.assertRaisesRegex(FinalEditorContractError, "per_chapter"):
            validate_contract(relaxed)

        mutable = copy.deepcopy(contract)
        mutable["candidate"]["source_mutation"] = "allowed"
        with self.assertRaisesRegex(FinalEditorContractError, "source_mutation"):
            validate_contract(mutable)

        unlocked = copy.deepcopy(contract)
        unlocked["locked_invariants"]["narrative_voice"] = False
        with self.assertRaisesRegex(FinalEditorContractError, "invariants"):
            validate_contract(unlocked)

    def test_word_budget_rejects_large_chapter_reduction(self):
        contract = build_contract("trial", "r1", source_texts(), created_at="now")
        accepted = word_budget_report(contract, {
            "ch0001.md": words(95, "first"),
            "ch0002.md": words(105, "second"),
        })
        self.assertTrue(accepted["accepted"])

        rejected = word_budget_report(contract, {
            "ch0001.md": words(74, "first"),
            "ch0002.md": words(100, "second"),
        })
        self.assertFalse(rejected["accepted"])
        self.assertFalse(rejected["chapters"][0]["within_budget"])

    def test_candidate_must_have_the_same_chapter_set(self):
        contract = build_contract("trial", "r1", source_texts(), created_at="now")
        with self.assertRaisesRegex(FinalEditorContractError, "chapter set"):
            word_budget_report(contract, {"ch0001.md": words(100)})


if __name__ == "__main__":
    unittest.main()
