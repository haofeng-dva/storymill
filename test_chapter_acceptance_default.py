# -*- coding: utf-8 -*-
import unittest
from pathlib import Path

from engine import build_argument_parser, option_compatibility_error


class ChapterAcceptanceDefaultTests(unittest.TestCase):
    def setUp(self):
        self.parser = build_argument_parser()

    def test_standard_production_defaults_to_quality_gate(self):
        args = self.parser.parse_args(["--direction", "test direction"])
        self.assertTrue(args.quality_gate)

    def test_append_production_defaults_to_quality_gate(self):
        args = self.parser.parse_args(["--story-id", "existing", "--append-chapters", "12"])
        self.assertTrue(args.quality_gate)

    def test_legacy_quality_gate_flag_remains_compatible(self):
        args = self.parser.parse_args(["--direction", "test direction", "--quality-gate"])
        self.assertTrue(args.quality_gate)

    def test_lightweight_and_controlled_contract_modes_cannot_compete(self):
        args = self.parser.parse_args([
            "--direction", "test direction", "--chapter-contracts", "--controlled-generation",
        ])
        self.assertIn("cannot be combined", option_compatibility_error(args))

    def test_archived_resume_script_cannot_run_a_parallel_production_path(self):
        source = Path("_resume_chapters.py").read_text(encoding="utf-8")
        self.assertIn("cannot produce chapters", source)


if __name__ == "__main__":
    unittest.main()
