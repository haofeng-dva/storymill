import json
import os
import tempfile
import unittest

from distill.experience_runtime import ExperienceRuntime


class ExperienceRuntimeTests(unittest.TestCase):
    def _runtime(self, payload, enabled=True, max_items=2):
        fd, path = tempfile.mkstemp(suffix="_experience_pack.json")
        os.close(fd)
        root = os.path.dirname(path)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        return ExperienceRuntime(root, {"enabled": enabled, "pack": path, "max_items": max_items})

    def test_limits_and_respects_named_chapter(self):
        rt = self._runtime({"experiences": [
            {"id": "a", "lesson": "Chapter 1: put conflict on page one", "phase": ["chapter_1"], "scope": ["general"], "status": "active"},
            {"id": "b", "lesson": "Chapter 2: escalate the cost", "phase": ["chapter_1", "chapter_2"], "scope": ["general"], "status": "active"},
            {"id": "c", "lesson": "Use a concrete object as a reveal", "phase": ["chapter_1"], "scope": ["general"], "status": "active"},
        ], "selection_policy": {}})
        selected = rt.select("general", "chapter_1")
        self.assertEqual({x["id"] for x in selected}, {"a", "c"})

    def test_disabled_or_invalid_pack_falls_back(self):
        rt = ExperienceRuntime(".", {"enabled": False, "pack": "missing.json"})
        self.assertEqual(rt.render("romance", "chapter_1"), ("", []))

    def test_calibration_and_duplicate_baseline_are_ignored(self):
        rt = self._runtime({"experiences": [
            {"id": "n", "lesson": "Calibration target roughly 2300 words", "phase": ["chapter_1"], "scope": ["general"], "status": "active"},
            {"id": "d", "lesson": "Start in-scene and end with a hook", "phase": ["chapter_1"], "scope": ["general"], "status": "active"},
        ]})
        self.assertEqual(rt.select("general", "chapter_1", "start in-scene end with a hook"), [])


if __name__ == "__main__":
    unittest.main()
