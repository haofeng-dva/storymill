import shutil
import uuid
import unittest
from pathlib import Path

from writing_status import is_deliverable, load_status, write_status


class WritingStatusTests(unittest.TestCase):
    def test_partial_status_is_not_deliverable(self):
        root = Path(".status-test-" + uuid.uuid4().hex)
        try:
            root.mkdir()
            path, payload = write_status(
                root, run_id="r", story_id="s", expected_chapters=3,
                completed_chapters=[1, 2], missing_chapters=[3],
            )
            self.assertEqual(Path(path), root / "writing_status.json")
            self.assertEqual(load_status(root), payload)
            self.assertFalse(is_deliverable(payload))
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_complete_status_requires_every_expected_chapter(self):
        payload = {
            "status": "completed", "expected_chapters": 3,
            "completed_chapters": [1, 2], "missing_chapters": [],
        }
        self.assertFalse(is_deliverable(payload))

    def test_complete_status_is_deliverable(self):
        payload = {
            "status": "completed", "expected_chapters": 3,
            "completed_chapters": [1, 2, 3], "missing_chapters": [],
        }
        self.assertTrue(is_deliverable(payload))


if __name__ == "__main__":
    unittest.main()
