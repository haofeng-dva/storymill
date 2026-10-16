import copy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

from control_context import build_control_context, load_state_delta
from control_contracts import ControlContractError
from engine import should_reuse_planning_artifact
from test_control_contracts import valid_documents


def bundle():
    return {"artifacts": valid_documents()}


class ControlContextTests(unittest.TestCase):
    def test_controlled_generation_reuses_existing_planning_inputs(self):
        args = SimpleNamespace(resume=False, append_chapters=0, controlled_generation=True)
        self.assertTrue(should_reuse_planning_artifact(args, __file__))
        self.assertFalse(should_reuse_planning_artifact(args, "__missing_planning_input__.md"))

    def test_chapter_context_contains_only_current_scenes_and_participants(self):
        payload = bundle()
        second_contract = copy.deepcopy(payload["artifacts"]["chapter_contracts"]["chapters"]["0001"])
        second_contract["chapter"] = 2
        second_contract["facts"] = [{"id": "ch2-fact"}]
        payload["artifacts"]["chapter_contracts"]["chapters"]["0002"] = second_contract
        second_scene = copy.deepcopy(payload["artifacts"]["scene_ledger"]["chapters"]["0001"][0])
        second_scene.update({"scene_id": "ch2-rafe", "participants": ["Rafe"]})
        second_scene["completion_assertion"]["id"] = "ch2-rafe-complete"
        second_scene["fact_slots"][0]["id"] = "ch2-rafe-fact"
        payload["artifacts"]["scene_ledger"]["chapters"]["0002"] = [second_scene]
        payload["artifacts"]["character_voice_cards"]["characters"].append({
            "name": "Rafe", "default_speech": "Brief.",
            "pressure_response_order": ["assesses danger"],
            "failure_triggers": ["loss of control"],
            "forbidden_generic_moves": ["witty comeback"],
            "exclusive_action_or_information": "Knows pack routes.",
        })

        result = build_control_context(payload, 2, {"new_information": ["Rafe is cursed."]})

        self.assertEqual(result["diagnostics"]["selected_voice_cards"], ["Rafe"])
        self.assertEqual(result["diagnostics"]["state_delta_status"], "available")
        self.assertIn("ch2-rafe", result["text"])
        self.assertNotIn("ch1-contract", result["text"])
        self.assertNotIn("Mara\",\"default_speech", result["text"])
        self.assertNotIn("pre_state", result["text"])
        self.assertIn('"e"', result["text"])

    def test_first_chapter_uses_opening_contract_without_prior_state(self):
        result = build_control_context(bundle(), 1)
        self.assertEqual(result["diagnostics"]["state_delta_status"], "not_required")
        self.assertIn("\"o\"", result["text"])

    def test_prior_state_handoff_is_bounded_without_losing_assertion_statuses(self):
        prior = {
            "assertion_statuses": [{"assertion_id": "ch1-outcome", "status": "met"}],
            "non_regression_statuses": [{"assertion_id": "ch1-retained", "status": "preserved"}],
            "new_information": ["first fact", "second fact"],
            "relationship_changes": ["first relationship", "second relationship"],
            "open_hooks": ["first hook", "second hook"],
            "fact_statuses": [
                {"fact_id": "sample-count", "status": "consistent", "value": "twelve"},
                {"fact_id": "permission", "status": "consistent", "value": "level-3"},
                {"fact_id": "time", "status": "consistent", "value": "midnight"},
            ],
        }
        result = build_control_context(bundle(), 1)
        self.assertIn('"e"', result["text"])
        # Use chapter one's compact contract while directly checking its prior-state transform.
        from control_context import _previous_execution_view
        view = _previous_execution_view(prior)
        self.assertEqual(view["outcomes"], [{"id": "ch1-outcome", "status": "met"}])
        self.assertEqual(view["new"], ["first fact", "second fact"])
        self.assertEqual(view["facts"], [
            {"id": "sample-count", "value": "twelve"},
            {"id": "permission", "value": "level-3"},
        ])
        self.assertNotIn("relationship_changes", view)
        self.assertNotIn("open_hooks", view)

    def test_state_reader_revalidates_schema_before_handoff(self):
        root = Path("out") / "control_context_state_test"
        state_path = root / "state" / "state_delta_ch0001.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps({
            "chapter": 1,
            "source_sha256": "bad",
            "assertion_statuses": [], "non_regression_statuses": [],
            "scene_statuses": [], "fact_statuses": [],
            "new_information": [], "relationship_changes": [], "open_hooks": [],
            "conflicts_with_contract": [], "validation_status": "passed",
        }), encoding="utf-8")
        with self.assertRaisesRegex(ControlContractError, "prior state delta is invalid"):
            load_state_delta(root, 1)

    def test_missing_voice_card_and_over_budget_context_fail_closed(self):
        payload = bundle()
        payload["artifacts"]["scene_ledger"]["chapters"]["0001"][0]["participants"] = ["Unknown"]
        with self.assertRaisesRegex(ControlContractError, "lack voice cards"):
            build_control_context(payload, 1)

        payload = bundle()
        payload["artifacts"]["chapter_contracts"]["chapters"]["0001"]["end_assertions"][0]["claim"] = "x" * 2200
        with self.assertRaisesRegex(ControlContractError, "limit"):
            build_control_context(payload, 1)

    def test_writer_voice_view_keeps_short_pressure_and_avoid_cues(self):
        result = build_control_context(bundle(), 1)
        self.assertIn("notices danger", result["text"])
        self.assertNotIn("asks a precise question", result["text"])

    def test_writer_scene_view_compacts_completion_and_fact_slot_claims(self):
        result = build_control_context(bundle(), 1)
        self.assertIn('"ch1-contract-complete"', result["text"])
        self.assertIn('["ch1-lab-status","sealed"]', result["text"])
        self.assertNotIn("Mara signs and leaves campus.", result["text"])
        self.assertNotIn("Laboratory status", result["text"])


if __name__ == "__main__":
    unittest.main()
