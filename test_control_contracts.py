import copy
from pathlib import Path
import unittest

from control_contracts import (
    CONTROL_FILENAMES,
    ControlContractError,
    MAX_CONTEXT_BYTES,
    acceptance_requirements_for_chapter,
    load_control_bundle,
    validate_chapter_contracts,
    validate_character_voice_cards,
    validate_context_budget,
    validate_opening_contract,
    validate_prose_budget,
    validate_scene_ledger,
    validate_cross_artifact_references,
)


def metadata():
    return {
        "schema_version": "controlled_generation_v2",
        "story_id": "trial",
        "source_sha256": "a" * 64,
        "created_at": "2026-09-11T00:00:00Z",
    }


def valid_documents():
    contracts = metadata()
    contracts["chapters"] = {
        "0001": {
            "chapter": 1,
            "pre_state": {},
            "must_change": ["Mara accepts the contract."],
            "end_assertions": [{"id": "mara-signs", "claim": "Mara accepts the contract.", "required": True}],
            "must_not_regress": [{"id": "lab-closed", "claim": "The lab has already been closed.", "evidence_requirement": "Mara finds the sealed laboratory inaccessible."}],
            "facts": [{"id": "ch1-lab-status", "claim": "Laboratory status", "value": "sealed"}],
        }
    }
    ledger = metadata()
    ledger["chapters"] = {
        "0001": [{
            "scene_id": "ch1-contract", "conflict": "Mara needs work.",
            "choice": "She signs.", "result": "She leaves campus.",
            "irreversible_change": "She is bound to the contract.",
            "completion_assertion": {"id": "ch1-contract-complete", "claim": "Mara signs and leaves campus."},
            "fact_slots": [{"id": "ch1-lab-status", "claim": "Laboratory status", "value": "sealed"}],
            "allowed_reuse_key": "", "participants": ["Mara"],
            "required_information": ["The contract is unusual."],
        }]
    }
    opening = metadata()
    opening.update({
        "visible_loss": "Her sealed laboratory.", "forcing_event": "A contract arrives.",
        "first_core_evidence": "A wolf seal.", "background_serves_choice": "Whether to sign.",
        "max_setup_paragraphs_without_change": 2,
    })
    voices = metadata()
    voices["characters"] = [{
        "name": "Mara", "default_speech": "Precise and guarded.",
        "pressure_response_order": ["notices danger", "hesitates", "asks a precise question"],
        "failure_triggers": ["threat to her work"],
        "forbidden_generic_moves": ["instant witty comeback"],
        "exclusive_action_or_information": "Can interpret laboratory data.",
    }]
    budget = metadata()
    budget.update({
        "rules": [{"id": "room-narrows", "evidence_ids": ["ops-04"], "pattern": "room\\s+narrow"}],
        "per_chapter_limits": {"room-narrows": 0},
        "book_limits": {"room-narrows": 1},
    })
    return {
        "chapter_contracts": contracts,
        "scene_ledger": ledger,
        "opening_contract": opening,
        "character_voice_cards": voices,
        "prose_budget": budget,
    }


class ControlContractSchemaTests(unittest.TestCase):
    def test_valid_documents_pass_all_schema_validators(self):
        docs = valid_documents()
        validators = {
            "chapter_contracts": validate_chapter_contracts,
            "scene_ledger": validate_scene_ledger,
            "opening_contract": validate_opening_contract,
            "character_voice_cards": validate_character_voice_cards,
            "prose_budget": validate_prose_budget,
        }
        self.assertEqual(set(validators), set(CONTROL_FILENAMES))
        for name, validator in validators.items():
            self.assertEqual(validator(docs[name], "trial", "a" * 64), docs[name])

    def test_control_end_assertions_are_the_chapter_acceptance_requirements(self):
        bundle = {"artifacts": valid_documents()}
        self.assertEqual(acceptance_requirements_for_chapter(bundle, 1), [{
            "id": "mara-signs", "requirement": "Mara accepts the contract.",
        }])

        bundle["artifacts"]["chapter_contracts"]["chapters"]["0001"]["end_assertions"][0]["required"] = False
        with self.assertRaisesRegex(ControlContractError, "required and unique"):
            acceptance_requirements_for_chapter(bundle, 1)

    def test_contract_rejects_missing_metadata_and_duplicate_fact_ids(self):
        doc = valid_documents()["chapter_contracts"]
        del doc["created_at"]
        with self.assertRaisesRegex(ControlContractError, "created_at"):
            validate_chapter_contracts(doc)

        doc = valid_documents()["chapter_contracts"]
        doc["chapters"]["0002"] = copy.deepcopy(doc["chapters"]["0001"])
        doc["chapters"]["0002"]["chapter"] = 2
        doc["chapters"]["0002"]["end_assertions"][0]["id"] = "mara-signs-ch2"
        doc["chapters"]["0002"]["must_not_regress"][0]["id"] = "lab-closed-ch2"
        with self.assertRaisesRegex(ControlContractError, "duplicate fact id"):
            validate_chapter_contracts(doc)

    def test_contract_requires_unique_atomic_end_assertions(self):
        doc = valid_documents()["chapter_contracts"]
        del doc["chapters"]["0001"]["end_assertions"]
        with self.assertRaisesRegex(ControlContractError, "end_assertions"):
            validate_chapter_contracts(doc)

        doc = valid_documents()["chapter_contracts"]
        doc["chapters"]["0001"]["must_not_regress"][0]["id"] = "mara-signs"
        with self.assertRaisesRegex(ControlContractError, "duplicate assertion id"):
            validate_chapter_contracts(doc)

        doc = valid_documents()["chapter_contracts"]
        del doc["chapters"]["0001"]["must_not_regress"][0]["evidence_requirement"]
        with self.assertRaisesRegex(ControlContractError, "evidence_requirement"):
            validate_chapter_contracts(doc)

    def test_scene_ledger_requires_irreversible_change_and_new_reuse_outcome(self):
        doc = valid_documents()["scene_ledger"]
        doc["chapters"]["0001"][0]["irreversible_change"] = ""
        with self.assertRaisesRegex(ControlContractError, "irreversible_change"):
            validate_scene_ledger(doc)

        doc = valid_documents()["scene_ledger"]
        duplicate = copy.deepcopy(doc["chapters"]["0001"][0])
        duplicate["scene_id"] = "ch1-contract-again"
        duplicate["completion_assertion"]["id"] = "ch1-contract-again-complete"
        duplicate["fact_slots"][0]["id"] = "ch1-contract-again-fact"
        duplicate["allowed_reuse_key"] = "contract"
        doc["chapters"]["0001"][0]["allowed_reuse_key"] = "contract"
        doc["chapters"]["0002"] = [duplicate]
        with self.assertRaisesRegex(ControlContractError, "repeats the same result"):
            validate_scene_ledger(doc)

    def test_scene_ledger_requires_unique_completion_and_fact_slots(self):
        doc = valid_documents()["scene_ledger"]
        del doc["chapters"]["0001"][0]["completion_assertion"]
        with self.assertRaisesRegex(ControlContractError, "completion_assertion"):
            validate_scene_ledger(doc)

        doc = valid_documents()["scene_ledger"]
        duplicate = copy.deepcopy(doc["chapters"]["0001"][0])
        duplicate["scene_id"] = "ch1-contract-two"
        doc["chapters"]["0002"] = [duplicate]
        with self.assertRaisesRegex(ControlContractError, "duplicate scene completion"):
            validate_scene_ledger(doc)

        doc = valid_documents()["scene_ledger"]
        doc["chapters"]["0001"][0]["fact_slots"][0]["value"] = "Several former alphas"
        with self.assertRaisesRegex(ControlContractError, "concrete literal value"):
            validate_scene_ledger(doc)

    def test_opening_voice_and_budget_reject_unverifiable_rules(self):
        opening = valid_documents()["opening_contract"]
        opening["max_setup_paragraphs_without_change"] = 0
        with self.assertRaisesRegex(ControlContractError, "positive integer"):
            validate_opening_contract(opening)

        voices = valid_documents()["character_voice_cards"]
        voices["characters"][0]["failure_triggers"] = []
        with self.assertRaisesRegex(ControlContractError, "failure_triggers"):
            validate_character_voice_cards(voices)

        budget = valid_documents()["prose_budget"]
        budget["rules"][0]["evidence_ids"] = []
        with self.assertRaisesRegex(ControlContractError, "evidence_ids"):
            validate_prose_budget(budget)

    def test_context_budget_counts_utf8_bytes(self):
        self.assertEqual(validate_context_budget("a" * MAX_CONTEXT_BYTES), MAX_CONTEXT_BYTES)
        with self.assertRaisesRegex(ControlContractError, "limit"):
            validate_context_budget("中" * (MAX_CONTEXT_BYTES // 3 + 1))

    def test_missing_legacy_control_artifacts_are_reported_not_fabricated(self):
        missing_dir = Path(__file__).parent / "__no_control_artifacts_for_test__"
        result = load_control_bundle(missing_dir, "trial")
        self.assertFalse(result["available"])
        self.assertEqual(result["artifacts"], {})
        self.assertEqual(set(result["missing"]), set(CONTROL_FILENAMES))
        with self.assertRaisesRegex(ControlContractError, "missing control artifacts"):
            load_control_bundle(missing_dir, "trial", allow_missing=False)

    def test_cross_artifact_validation_rejects_alias_not_present_in_voice_cards(self):
        docs = valid_documents()
        docs["scene_ledger"]["chapters"]["0001"][0]["participants"] = ["Dr. Mara"]
        with self.assertRaisesRegex(ControlContractError, "exactly match"):
            validate_cross_artifact_references(docs)

    def test_contract_fact_requires_a_claim_and_literal_value(self):
        doc = valid_documents()["chapter_contracts"]
        del doc["chapters"]["0001"]["facts"][0]["value"]
        with self.assertRaisesRegex(ControlContractError, "facts\[0\]\.value"):
            validate_chapter_contracts(doc)

    def test_cross_artifacts_require_every_declared_fact_to_have_a_matching_slot(self):
        docs = valid_documents()
        docs["scene_ledger"]["chapters"]["0001"][0]["fact_slots"] = []
        with self.assertRaisesRegex(ControlContractError, "not covered"):
            validate_cross_artifact_references(docs)

    def test_cross_artifacts_require_every_scene_slot_to_be_declared_by_contract(self):
        docs = valid_documents()
        docs["chapter_contracts"]["chapters"]["0001"]["facts"] = []
        with self.assertRaisesRegex(ControlContractError, "scene fact slot.*not declared"):
            validate_cross_artifact_references(docs)


if __name__ == "__main__":
    unittest.main()
