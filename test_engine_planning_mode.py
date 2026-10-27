# -*- coding: utf-8 -*-
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from engine import build_argument_parser, should_reuse_planning_artifact


class PlanningModeArgumentTests(unittest.TestCase):
    def test_outline_only_remains_distinct_from_planning_only(self):
        args = build_argument_parser().parse_args(["--outline-only"])
        self.assertTrue(args.outline_only)
        self.assertFalse(args.planning_only)

    def test_planning_only_is_a_no_chapter_mode(self):
        args = build_argument_parser().parse_args(["--planning-only"])
        self.assertTrue(args.planning_only)
        self.assertFalse(args.outline_only)
        self.assertFalse(args.controlled_generation)

    def test_story_bible_is_an_explicit_opt_in(self):
        args = build_argument_parser().parse_args(["--story-bible"])
        self.assertTrue(args.story_bible)

    def test_planning_only_reuses_saved_planning_artifacts(self):
        args = SimpleNamespace(
            resume=False,
            append_chapters=0,
            controlled_generation=False,
            planning_only=True,
        )
        with patch("engine.os.path.exists", return_value=True):
            self.assertTrue(should_reuse_planning_artifact(args, "outline.md"))


if __name__ == "__main__":
    unittest.main()
