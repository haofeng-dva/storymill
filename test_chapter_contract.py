# -*- coding: utf-8 -*-
import json
import unittest

from chapter_contract import (
    ChapterContractError, parse_compiler_response, planning_hash,
    requirements_for_chapter, validate_contract, writer_contract_block,
)


OUTLINE = (
    "Claire sorts records in her father's study on Friday night. "
    "Your father kept everything for a reason. "
    "$84,000 transfer dated five days before his death."
)


def contract_document():
    return {
        "schema_version": "chapter_acceptance_contract_v1",
        "story_id": "trial",
        "source_sha256": planning_hash("direction", "outline", "bible"),
        "chapters": {
            "0001": {
                "chapter": 1,
                "requirements": [
                    {"id": "ch0001-contract-01", "type": "context", "requirement": "Claire sorts records in her father's study on Friday night.", "source_quote": "Claire sorts records in her father's study on Friday night."},
                    {"id": "ch0001-contract-02", "type": "dialogue", "requirement": "Her mother says, 'Your father kept everything for a reason.'", "source_quote": "Your father kept everything for a reason."},
                    {"id": "ch0001-contract-03", "type": "fact", "requirement": "Claire finds an $84,000 transfer dated five days before his death.", "source_quote": "$84,000 transfer dated five days before his death."},
                ],
            },
        },
    }


class ChapterContractTests(unittest.TestCase):
    def test_valid_contract_produces_shared_writer_and_acceptance_requirements(self):
        document = contract_document()
        validated = validate_contract(document, "trial", [1], document["source_sha256"], OUTLINE)
        requirements = requirements_for_chapter(validated, 1)
        self.assertEqual([item["id"] for item in requirements], [
            "ch0001-contract-01", "ch0001-contract-02", "ch0001-contract-03",
        ])
        rendered = writer_contract_block(validated, 1)
        self.assertIn("ch0001-contract-03", rendered)
        self.assertIn(requirements[2]["requirement"], rendered)

    def test_rejects_unstable_or_unsupported_contract_items(self):
        document = contract_document()
        document["chapters"]["0001"]["requirements"][1]["id"] = "wrong-id"
        with self.assertRaisesRegex(ChapterContractError, "stable and unique"):
            validate_contract(document, "trial", [1], document["source_sha256"], OUTLINE)

        document = contract_document()
        document["chapters"]["0001"]["requirements"][0]["type"] = "style"
        with self.assertRaisesRegex(ChapterContractError, "invalid type"):
            validate_contract(document, "trial", [1], document["source_sha256"], OUTLINE)

    def test_rejects_requirement_without_an_outline_anchor(self):
        document = contract_document()
        document["chapters"]["0001"]["requirements"][2]["source_quote"] = "A fabricated event"
        with self.assertRaisesRegex(ChapterContractError, "source_quote is not in the outline"):
            validate_contract(document, "trial", [1], document["source_sha256"], OUTLINE)

    def test_parser_rejects_contract_from_different_planning_inputs(self):
        document = contract_document()
        with self.assertRaisesRegex(ChapterContractError, "source hash mismatch"):
            parse_compiler_response(json.dumps(document), "trial", [1], "a" * 64, OUTLINE)
