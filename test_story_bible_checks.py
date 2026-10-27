# -*- coding: utf-8 -*-
import copy
import unittest

from story_bible import commit_chapter_plan
from story_bible_checks import audit_chapter_facts, planned_fact_slots
from test_story_bible import StoryBibleTests


class StoryBiblePostWriteChecksTests(unittest.TestCase):
    def setUp(self):
        fixture = StoryBibleTests()
        fixture.setUp()
        self.bible = commit_chapter_plan(copy.deepcopy(fixture.bible), fixture.plan(), fixture.source)

    def test_planned_fact_is_delegated_to_state_review(self):
        audit = audit_chapter_facts(
            self.bible, 1, "Elias reopened case WV-4417 after six months of silence."
        )
        self.assertEqual(audit["decision"], "deferred_to_state_delta")
        self.assertEqual(audit["findings"], [])
        self.assertEqual(audit["state_review_fact_slots"][0]["story_bible_fact_id"], "daniel-duration")

    def test_planned_fact_slots_keep_canonical_value_and_distinct_runtime_id(self):
        slots = planned_fact_slots(self.bible, 1)
        self.assertEqual(slots[0]["id"], "story-bible--daniel-duration")
        self.assertEqual(slots[0]["value"], "six months")

    def test_available_but_unused_fact_is_not_required_in_the_prose(self):
        plan = copy.deepcopy(self.bible["chapter_plans"]["0001"])
        plan["known_fact_ids"] = []
        plan["beats"][0]["fact_ids"] = ["daniel-case"]
        self.bible["chapter_plans"]["0001"] = plan
        audit = audit_chapter_facts(self.bible, 1, "Elias reopened case WV-4417.")
        self.assertEqual(audit["decision"], "deferred_to_state_delta")

    def test_known_fact_is_context_not_an_unconditional_prose_requirement(self):
        plan = copy.deepcopy(self.bible["chapter_plans"]["0001"])
        plan["beats"][0]["fact_ids"] = []
        self.bible["chapter_plans"]["0001"] = plan
        audit = audit_chapter_facts(self.bible, 1, "Elias reopened case WV-4417 after nine years.")
        self.assertEqual(audit["decision"], "not_applicable")
        self.assertEqual(audit["checked_fact_ids"], [])

    def test_empty_canon_input_is_not_reported_as_a_checked_pass(self):
        plan = copy.deepcopy(self.bible["chapter_plans"]["0001"])
        plan["known_fact_ids"] = []
        plan["beats"][0]["fact_ids"] = []
        self.bible["chapter_plans"]["0001"] = plan
        audit = audit_chapter_facts(self.bible, 1, "Elias waits.")
        self.assertEqual(audit["checked_fact_ids"], [])
        self.assertEqual(audit["decision"], "not_applicable")

    def test_runtime_slots_are_empty_only_when_the_plan_cites_no_facts(self):
        plan = copy.deepcopy(self.bible["chapter_plans"]["0001"])
        plan["beats"][0]["fact_ids"] = []
        self.bible["chapter_plans"]["0001"] = plan
        self.assertEqual(planned_fact_slots(self.bible, 1), [])

    def test_mutable_role_is_not_forced_back_into_state_validation(self):
        self.bible["facts"].append({
            "id": "elias-role", "subject_id": "elias-ward", "field": "role",
            "value": "detective", "source_quote": "Elias Ward is Daniel Ward's brother.",
        })
        plan = copy.deepcopy(self.bible["chapter_plans"]["0001"])
        plan["beats"][0]["fact_ids"].append("elias-role")
        self.bible["chapter_plans"]["0001"] = plan
        slots = planned_fact_slots(self.bible, 1)
        self.assertNotIn("story-bible--elias-role", [slot["id"] for slot in slots])
        self.assertIn("story-bible--daniel-duration", [slot["id"] for slot in slots])


if __name__ == "__main__":
    unittest.main()
