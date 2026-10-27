import hashlib
import unittest
from unittest.mock import mock_open, patch

from packager import (
    build_manifest, build_plain_text, package_exit_code, summarize_native_review,
    validate_delivery_artifacts,
)


class PackagerDeliveryTests(unittest.TestCase):

    def test_manifest_records_blocked_release_explicitly(self):
        manifest = build_manifest(
            {}, "story", 1, 700, "", "", release_blockers=["delivery_missing:promotion_cn.md"],
            plain_text_path="out/story.txt",
        )
        self.assertEqual(manifest["releaseStatus"], "blocked")
        self.assertEqual(manifest["releaseBlockers"], ["delivery_missing:promotion_cn.md"])
        self.assertEqual(manifest["plainTextPath"], "out/story.txt")

    def test_requires_chinese_summary_and_plain_text_but_not_promotion_copy(self):
        self.assertEqual(
            set(validate_delivery_artifacts("story", "out", "out/story.txt")),
            {"summary_cn.md", "plain_text"},
        )
        with patch("packager.os.path.isfile", return_value=True), patch(
            "builtins.open", mock_open(read_data="内容")
        ):
            self.assertEqual(validate_delivery_artifacts("story", "out", "out/story.txt"), [])

    def test_exit_code_follows_release_blockers(self):
        self.assertEqual(package_exit_code([]), 0)
        self.assertEqual(package_exit_code(["native_review_not_release_ready"]), 2)
        self.assertEqual(package_exit_code(["delivery_missing:summary_cn.md"]), 2)

    def test_plain_text_has_contents_and_numbered_chapters(self):
        opened = mock_open()
        with patch("packager.open", opened):
            build_plain_text(["First chapter.", "Second chapter."], {"title": "Test Story"}, "story.txt")
        text = opened().write.call_args[0][0]
        self.assertIn("TABLE OF CONTENTS", text)
        self.assertIn("Chapter 1\nChapter 2", text)
        self.assertIn("CHAPTER 1\n\nFirst chapter.", text)
        self.assertIn("CHAPTER 2\n\nSecond chapter.", text)

    def test_exhausted_native_review_retries_block_release(self):
        chapter = "A reviewed chapter."
        digest = hashlib.sha256(chapter.encode("utf-8")).hexdigest()
        rows = [{
            "file": "ch0001.md", "source_sha256": digest,
            "review_coverage": "failed_after_retries", "failed": ["审稿调用失败"],
        }]
        with patch("packager.os.path.isfile", return_value=True), patch(
            "packager.open", mock_open(read_data=chapter)
        ), patch("packager.json.load", return_value=rows):
            summary = summarize_native_review("out", "story", ["ch0001.md"])
        self.assertEqual(summary["status"], "blocked")
        self.assertEqual(summary["failed_files"], ["ch0001.md"])


if __name__ == "__main__":
    unittest.main()
