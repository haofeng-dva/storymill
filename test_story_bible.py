# -*- coding: utf-8 -*-
import unittest
import json

from engine import WritingEngine
from story_bible import (
    PLAN_SCHEMA_VERSION, StoryBibleError, chapter_outline_section, story_bible_foundation, commit_chapter_plan, make_story_bible,
    normalize_plan_canon_references, normalize_new_entry_source_quotes, render_chapter_packet, rollback_chapter_plan, validate_chapter_plan,
)


class StoryBibleTests(unittest.TestCase):
    def setUp(self):
        self.direction = "A mystery serial."
        self.outline = "Chapter One\nDaniel Ward vanished six months ago. Case WV-4417."
        self.world = "Elias Ward is Daniel Ward's brother."
        self.bible = make_story_bible("book", self.direction, self.outline, self.world, {
            "entities": [
                {"id": "daniel-ward", "name": "Daniel Ward", "aliases": ["Daniel"], "source_quote": "Daniel Ward vanished six months ago."},
                {"id": "elias-ward", "name": "Elias Ward", "aliases": ["Elias"], "source_quote": "Elias Ward is Daniel Ward's brother."},
            ],
            "facts": [
                {"id": "daniel-duration", "subject_id": "daniel-ward", "field": "disappearance-duration", "value": "six months", "source_quote": "Daniel Ward vanished six months ago."},
                {"id": "daniel-case", "subject_id": "daniel-ward", "field": "case-number", "value": "WV-4417", "source_quote": "Case WV-4417."},
            ],
        })
        self.source = "Daniel Ward vanished six months ago. Case WV-4417."

    def plan(self, **changes):
        payload = {
            "schema_version": PLAN_SCHEMA_VERSION,
            "story_id": "book",
            "source_sha256": self.bible["source_sha256"],
            "chapter": 1,
            "known_entity_ids": ["elias-ward", "daniel-ward"],
            "known_fact_ids": ["daniel-duration", "daniel-case"],
            "new_entities": [],
            "new_facts": [],
            "beats": [{
                "participant_ids": ["elias-ward", "daniel-ward"],
                "fact_ids": ["daniel-duration", "daniel-case"],
                "conflict": "{{entity:elias-ward}} finds {{fact:daniel-case}}.",
                "choice": "He pursues the six-month trail.",
                "result": "{{entity:daniel-ward}} becomes the active case.",
            }],
        }
        payload.update(changes)
        return payload

    def test_committed_plan_renders_canonical_values(self):
        updated = commit_chapter_plan(self.bible, self.plan(), self.source)
        packet = render_chapter_packet(updated, 1)
        self.assertIn("Daniel Ward", packet)
        self.assertIn("six months", packet)
        self.assertIn("WV-4417", packet)

    def test_conflicting_new_fact_is_rejected_before_commit(self):
        plan = self.plan(new_facts=[{
            "id": "wrong-duration", "subject_id": "daniel-ward", "field": "disappearance-duration",
            "value": "nine years", "source_quote": "Daniel Ward vanished nine years ago.",
        }])
        with self.assertRaisesRegex(StoryBibleError, "conflicts with canon"):
            validate_chapter_plan(plan, self.bible, 1, "Daniel Ward vanished nine years ago.")

    def test_new_entity_and_fact_are_committed_once(self):
        source = "Mara Bell saw the token. Mara Bell is a witness."
        plan = self.plan(
            new_entities=[{"id": "mara-bell", "name": "Mara Bell", "role": "witness", "source_quote": "Mara Bell saw the token."}],
            new_facts=[{"id": "mara-role", "subject_id": "mara-bell", "field": "role", "value": "witness", "source_quote": "Mara Bell is a witness."}],
        )
        updated = commit_chapter_plan(self.bible, plan, source)
        self.assertEqual(updated["entities"][-1]["name"], "Mara Bell")
        self.assertEqual(updated["facts"][-1]["value"], "witness")

    def test_plan_cannot_commit_transient_beat_as_canon(self):
        plan = self.plan(new_facts=[{
            "id": "elias-emotional-turn", "subject_id": "elias-ward", "field": "emotional-turn",
            "value": "anger becomes resolve", "source_quote": "Elias feels anger becomes resolve.",
        }])
        with self.assertRaisesRegex(StoryBibleError, "chapter beat"):
            validate_chapter_plan(plan, self.bible, 1, "Elias feels anger becomes resolve.")

    def test_failed_chapter_plan_rolls_back_its_new_canon(self):
        source = "Mara Bell is a witness."
        plan = self.plan(new_entities=[{
            "id": "mara-bell", "name": "Mara Bell", "role": "witness", "source_quote": source,
        }], new_facts=[{
            "id": "mara-role", "subject_id": "mara-bell", "field": "role", "value": "witness", "source_quote": source,
        }])
        committed = commit_chapter_plan(self.bible, plan, source)
        rolled_back = rollback_chapter_plan(committed, 1)
        self.assertNotIn("0001", rolled_back["chapter_plans"])
        self.assertNotIn("mara-bell", [item["id"] for item in rolled_back["entities"]])
        self.assertNotIn("mara-role", [item["id"] for item in rolled_back["facts"]])

    def test_plan_cannot_reference_unknown_ids(self):
        plan = self.plan(known_fact_ids=["missing-fact"])
        with self.assertRaisesRegex(StoryBibleError, "unknown or duplicate fact"):
            validate_chapter_plan(plan, self.bible, 1, self.source)

    def test_plan_cannot_omit_canon_used_by_its_macro_outline(self):
        plan = self.plan(known_fact_ids=["daniel-case"])
        with self.assertRaisesRegex(StoryBibleError, "omitted canonical fact ids: daniel-duration"):
            validate_chapter_plan(plan, self.bible, 1, self.source)

    def test_existing_literals_must_use_tokens(self):
        plan = self.plan()
        plan["beats"][0]["choice"] = "Elias Ward waits six months."
        with self.assertRaisesRegex(StoryBibleError, "must reference entity"):
            validate_chapter_plan(plan, self.bible, 1, self.source)

    def test_normalizer_converts_existing_literals_into_verified_tokens(self):
        plan = self.plan()
        plan["known_fact_ids"] = []
        plan["beats"][0]["conflict"] = "Elias Ward finds WV-4417 after six months."
        normalized = normalize_plan_canon_references(plan, self.bible)
        self.assertIn("{{entity:elias-ward}}", normalized["beats"][0]["conflict"])
        self.assertIn("{{fact:daniel-case}}", normalized["beats"][0]["conflict"])
        self.assertIn("{{fact:daniel-duration}}", normalized["beats"][0]["conflict"])
        validate_chapter_plan(normalized, self.bible, 1, self.source)

    def test_normalizer_does_not_rewrite_ids_inside_existing_tokens(self):
        plan = self.plan()
        plan["beats"][0]["conflict"] = "{{entity:daniel-ward}} checks {{fact:daniel-case}}."
        normalized = normalize_plan_canon_references(plan, self.bible)
        self.assertEqual(
            normalized["beats"][0]["conflict"],
            "{{entity:daniel-ward}} checks {{fact:daniel-case}}.",
        )

    def test_normalizer_declares_canon_used_by_the_macro_outline(self):
        plan = self.plan(known_entity_ids=[], known_fact_ids=[])
        plan["beats"][0]["conflict"] = "The investigation needs a documented lead."
        normalized = normalize_plan_canon_references(plan, self.bible, self.source)
        self.assertIn("daniel-ward", normalized["known_entity_ids"])
        self.assertIn("daniel-duration", normalized["known_fact_ids"])
        self.assertIn("daniel-case", normalized["known_fact_ids"])
        validate_chapter_plan(normalized, self.bible, 1, self.source)

    def test_planner_does_not_treat_mutable_role_as_fixed_canon(self):
        self.bible["facts"].append({
            "id": "elias-role", "subject_id": "elias-ward", "field": "role",
            "value": "detective", "source_quote": "Elias Ward is Daniel Ward's brother.",
        })
        plan = self.plan(known_fact_ids=["daniel-duration", "daniel-case"])
        plan["beats"][0]["choice"] = "Elias becomes detective."
        normalized = normalize_plan_canon_references(plan, self.bible, "Elias becomes detective.")
        self.assertNotIn("elias-role", normalized["known_fact_ids"])
        validate_chapter_plan(normalized, self.bible, 1, "Elias becomes detective.")

    def test_normalizer_repairs_new_fact_quote_only_when_value_is_local(self):
        source = "Daniel Ward vanished six months ago.\nCase WV-4417.\nMara Bell is a witness."
        plan = self.plan(new_entities=[{
            "id": "mara-bell", "name": "Mara Bell", "role": "witness", "source_quote": "bad quote",
        }], new_facts=[{
            "id": "mara-role", "subject_id": "mara-bell", "field": "role", "value": "witness", "source_quote": "bad quote",
        }])
        normalized = normalize_new_entry_source_quotes(plan, source)
        self.assertEqual(normalized["new_entities"][0]["source_quote"], "Mara Bell is a witness.")
        self.assertEqual(normalized["new_facts"][0]["source_quote"], "Mara Bell is a witness.")
        validate_chapter_plan(normalized, self.bible, 1, source)

    def test_chapter_section_does_not_include_neighboring_chapter(self):
        outline = "Chapter One\nFirst.\n\nChapter Two\nSecond."
        self.assertEqual(chapter_outline_section(outline, 1), "Chapter One\nFirst.")
        self.assertEqual(chapter_outline_section(outline, 2), "Chapter Two\nSecond.")

    def test_chapter_section_ignores_serial_arc_summaries(self):
        outline = (
            "Story canon: Daniel Ward disappeared six months ago.\n\n"
            "Serial arc: Chapter Two: A future-summary must not become chapter input.\n\n"
            "Chapter One\nFirst detail.\n\nChapter Two\nSecond detail."
        )
        self.assertEqual(story_bible_foundation(outline), "Story canon: Daniel Ward disappeared six months ago.")
        self.assertEqual(chapter_outline_section(outline, 1), "Chapter One\nFirst detail.")
        self.assertEqual(chapter_outline_section(outline, 2), "Chapter Two\nSecond detail.")

    def test_chapter_section_accepts_compact_colon_heading(self):
        outline = (
            "Foundation.\n\n"
            "Serial arc:\n1. A summary, not a chapter section.\n\n"
            "Chapter 1: First detail.\n\n"
            "Chapter 2: Second detail."
        )
        self.assertEqual(chapter_outline_section(outline, 1), "Chapter 1: First detail.")
        self.assertEqual(chapter_outline_section(outline, 2), "Chapter 2: Second detail.")

    def test_engine_compiler_and_planner_feed_the_local_canon_gate(self):
        class FakeLLM:
            def __init__(self):
                self.responses = [
                    {"entities": [
                        {"id": "daniel-ward", "name": "Daniel Ward", "aliases": ["Daniel"], "source_quote": "Daniel Ward vanished six months ago."},
                    ], "facts": [
                        {"id": "daniel-duration", "subject_id": "daniel-ward", "field": "disappearance-duration", "value": "six months", "source_quote": "Daniel Ward vanished six months ago."},
                    ]},
                    {
                        "schema_version": PLAN_SCHEMA_VERSION, "story_id": "book", "source_sha256": None, "chapter": 1,
                        "known_entity_ids": ["daniel-ward"], "known_fact_ids": ["daniel-duration"],
                        "new_entities": [], "new_facts": [],
                        "beats": [{
                            "participant_ids": ["daniel-ward"], "fact_ids": ["daniel-duration"],
                            "conflict": "{{entity:daniel-ward}} is missing.",
                            "choice": "Search the trail for {{fact:daniel-duration}}.",
                            "result": "The case remains open.",
                        }],
                    },
                ]

            def chat(self, *_args, **_kwargs):
                payload = self.responses.pop(0)
                return {"content": json.dumps(payload)}

        engine = WritingEngine.__new__(WritingEngine)
        engine.llm = FakeLLM()
        direction = "A mystery."
        outline = "Story canon: Daniel Ward vanished six months ago.\n\nChapter One\nDaniel Ward vanished six months ago."
        world = "Daniel Ward is missing."
        compiled = engine.compile_story_bible(direction, outline, world, "book")
        self.assertNotIn("error", compiled)
        bible = compiled["content"]
        engine.llm.responses[0]["source_sha256"] = bible["source_sha256"]
        planned = engine.plan_story_bible_chapter(direction, chapter_outline_section(outline, 1), bible, 1)
        self.assertNotIn("error", planned)
        committed = commit_chapter_plan(bible, planned["content"], chapter_outline_section(outline, 1))
        self.assertIn("six months", render_chapter_packet(committed, 1))

    def test_compiler_retries_only_after_a_local_canon_rejection(self):
        valid = {
            "entities": [{"id": "daniel-ward", "name": "Daniel Ward", "aliases": [], "source_quote": "Daniel Ward vanished six months ago."}],
            "facts": [],
        }
        invalid = {
            "entities": [{"id": "daniel-ward", "name": "Daniel Ward", "aliases": [], "source_quote": "No matching source."}],
            "facts": [],
        }

        class FakeLLM:
            def __init__(self):
                self.calls = 0

            def chat(self, *_args, **_kwargs):
                self.calls += 1
                return {"content": json.dumps(invalid if self.calls == 1 else valid)}

        engine = WritingEngine.__new__(WritingEngine)
        engine.llm = FakeLLM()
        result = engine.compile_story_bible(
            "A mystery.",
            "Story canon: Daniel Ward vanished six months ago.\n\nChapter One\nDaniel Ward vanished six months ago.",
            "",
            "book",
        )
        self.assertNotIn("error", result)
        self.assertEqual(engine.llm.calls, 2)


if __name__ == "__main__":
    unittest.main()
