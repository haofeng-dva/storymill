import unittest
import hashlib
from unittest.mock import patch

from native_check import (
    acceptance_repair_feedback,
    blocking_review_findings,
    editorial_review_findings,
    extract_chapter_requirements,
    merge_retry_results,
    normalize_review_coverage,
    parse_acceptance_review,
    parse_scores,
    request_json_bounded,
    review_texts_with_retries,
    retryable_report_files,
)


class NativeCheckParsingTests(unittest.TestCase):
    def test_extracts_numbered_chapter_requirements_without_inventing_them(self):
        outline = """Chapter 1\n1. Mara finds the ledger.\n2. June refuses to leave.\n\nChapter 2\nMara loses the key."""
        requirements = extract_chapter_requirements(outline, 1)
        self.assertEqual(
            [item["requirement"] for item in requirements],
            ["Mara finds the ledger.", "June refuses to leave."],
        )

    def test_extracts_markdown_and_titled_chapter_headers(self):
        outline = """## Chapter 1 - The Ledger
- Mara finds the ledger.

### Chapter 2: The Escape
- June refuses to leave."""
        requirements = extract_chapter_requirements(outline, 2)
        self.assertEqual([item["requirement"] for item in requirements], ["June refuses to leave."])

    def test_extracts_list_style_and_word_number_chapter_headers(self):
        outline = """1. Chapter One: The Ledger
- Mara finds the ledger.

2. Ch. Two - The Escape
- June refuses to leave."""
        requirements = extract_chapter_requirements(outline, 2)
        self.assertEqual([item["requirement"] for item in requirements], ["June refuses to leave."])

    def test_extracts_inline_sentence_plan_after_english_chapter_header(self):
        outline = """Chapter 1: Mara inventories her father's study after the funeral. Behind a false back in his desk drawer, she finds a ledger.
Chapter 2: The Confrontation"""
        requirements = extract_chapter_requirements(outline, 1)
        self.assertEqual(
            [item["requirement"] for item in requirements],
            [
                "Mara inventories her father's study after the funeral. "
                "Behind a false back in his desk drawer, she finds a ledger."
            ],
        )
        self.assertEqual(extract_chapter_requirements(outline, 2), [])

    def test_extracts_markdown_numeric_and_chinese_chapter_headers(self):
        markdown_outline = """## 1. Mara finds the ledger

## 2. June refuses to leave"""
        chinese_outline = """第1章：Mara finds the ledger

第2章 - June refuses to leave"""
        self.assertEqual(
            [item["requirement"] for item in extract_chapter_requirements(markdown_outline, 2)],
            ["June refuses to leave"],
        )
        self.assertEqual(
            [item["requirement"] for item in extract_chapter_requirements(chinese_outline, 2)],
            ["June refuses to leave"],
        )

    def test_does_not_treat_an_ordinary_numbered_beat_as_a_chapter_header(self):
        outline = """1. Mara finds the ledger.
2. June refuses to leave."""
        self.assertEqual(extract_chapter_requirements(outline, 1), [])

    def test_acceptance_review_requires_exact_evidence_for_each_met_outcome(self):
        text = "Mara found the ledger. June refused to leave."
        requirements = [
            {"id": "ch0001-requirement-01", "requirement": "Mara finds the ledger."},
            {"id": "ch0001-requirement-02", "requirement": "June refuses to leave."},
        ]
        review = """{
          "outcomes": [
            {"id": "ch0001-requirement-01", "status": "met", "evidence_quotes": ["Mara found the ledger."], "reason": "shown"},
            {"id": "ch0001-requirement-02", "status": "met", "evidence_quotes": ["June refused to leave."], "reason": "shown"}
          ],
          "findings": []
        }"""
        parsed = parse_acceptance_review(review, text, requirements)
        self.assertNotIn("error", parsed)
        self.assertEqual(parsed["outcomes"][0]["status"], "met")

    def test_acceptance_review_rejects_duplicate_scene_without_two_anchors(self):
        text = "Mara found the ledger. Mara found the ledger again."
        requirements = [{"id": "ch0001-requirement-01", "requirement": "Mara finds the ledger."}]
        review = """{
          "outcomes": [{"id": "ch0001-requirement-01", "status": "met", "evidence_quotes": ["Mara found the ledger."], "reason": "shown"}],
          "findings": [{"category": "duplicate_scene", "evidence_quotes": ["Mara found the ledger."], "reason": "repeated", "repair_scope": "scene"}]
        }"""
        self.assertIn("error", parse_acceptance_review(review, text, requirements))

    def test_acceptance_feedback_only_uses_verified_findings(self):
        feedback = acceptance_repair_feedback({
            "outcomes": [{"status": "missing", "requirement": "Mara leaves town.", "reason": "not in chapter"}],
            "findings": [{
                "category": "clarity_idiomaticity", "repair_scope": "sentence",
                "reason": "non-idiomatic", "evidence_quotes": ["Her pulse turned blunt."],
            }],
        })
        self.assertIn("Mara leaves town.", feedback)
        self.assertIn("Her pulse turned blunt.", feedback)

    def test_bounded_request_preserves_a_successful_json_response(self):
        class Response:
            def read(self):
                return b'{"ok": true}'

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

        with patch("native_check.urllib.request.urlopen", return_value=Response()):
            data, error = request_json_bounded(object(), timeout=1)

        self.assertEqual(data, {"ok": True})
        self.assertIsNone(error)

    def test_parses_markdown_wrapped_scores(self):
        scores = parse_scores("`native_score`: 4/5\n`engagement_score`: 4/5")
        self.assertEqual(scores, {"native": 4.0, "engagement": 4.0})

    def test_continuity_label_blocks_even_with_passing_scores(self):
        review = (
            "1. **Continuity error:** A fact conflicts with the opening.\n"
            "`native_score`: 4/5\n`engagement_score`: 4/5"
        )
        self.assertEqual(len(blocking_review_findings(review)), 1)

    def test_style_note_is_not_promoted_to_a_fact_blocker(self):
        self.assertEqual(
            blocking_review_findings("1. **Mannered phrasing:** This metaphor is vague."),
            [],
        )

    def test_concrete_editorial_issue_survives_passing_scores(self):
        review = "Issues:\n- Repeated stock reaction.\nnative_score: 5\nengagement_score: 4"
        self.assertEqual(editorial_review_findings(review), ["Repeated stock reaction."])

    def test_retries_failed_chapters_only_after_first_pass(self):
        texts = {"ch0001.md": "first", "ch0002.md": "second"}
        response = "native_score: 4\nengagement_score: 4"
        with patch(
            "native_check.review_with_fallback",
            side_effect=[(None, "all providers timed out"), (response, "glm"), (response, "gpt")],
        ) as review:
            rows = review_texts_with_retries(texts, [], max_retries=3)

        self.assertEqual([call.args[0] for call in review.call_args_list], ["first", "second", "first"])
        self.assertEqual([row["file"] for row in rows], ["ch0001.md", "ch0002.md"])
        self.assertEqual(rows[0]["review_coverage"], "reviewed")
        self.assertEqual(rows[0]["review_attempt_count"], 2)
        self.assertEqual(rows[1]["review_attempt_count"], 1)
        self.assertEqual(rows[0]["review_attempts"][0]["status"], "failed")
        self.assertEqual(rows[0]["review_attempts"][1]["status"], "reviewed")

    def test_marks_chapter_unreviewed_only_after_three_retries(self):
        texts = {"ch0001.md": "first"}
        with patch("native_check.review_with_fallback", return_value=(None, "all providers timed out")) as review:
            rows = review_texts_with_retries(texts, [], max_retries=3)

        self.assertEqual(review.call_count, 4)
        self.assertEqual(rows[0]["review_coverage"], "failed_after_retries")
        self.assertEqual(rows[0]["review_attempt_count"], 4)
        self.assertEqual([item["attempt"] for item in rows[0]["review_attempts"]], [1, 2, 3, 4])

    def test_retry_failed_selects_and_replaces_only_transport_failures(self):
        texts = {"ch0001.md": "first", "ch0002.md": "second", "ch0003.md": "third"}
        previous = [
            {"file": "ch0001.md", "review_coverage": "reviewed", "content": "keep one"},
            {"file": "ch0002.md", "error": "all providers timed out", "failed": ["审稿调用失败"]},
            {"file": "ch0003.md", "review_coverage": "reviewed", "content": "keep three"},
        ]
        self.assertEqual(retryable_report_files(previous, texts), ["ch0002.md"])
        retried = [{"file": "ch0002.md", "review_coverage": "reviewed", "content": "replacement"}]
        merged = merge_retry_results(previous, retried, list(texts))
        self.assertEqual([row["file"] for row in merged], list(texts))
        self.assertEqual(merged[0]["content"], "keep one")
        self.assertEqual(merged[1]["content"], "replacement")
        self.assertEqual(merged[2]["content"], "keep three")

    def test_retry_failed_also_recovers_missing_checkpoint_chapters(self):
        texts = {"ch0001.md": "first", "ch0002.md": "second"}
        checkpoint = [{"file": "ch0001.md", "review_coverage": "reviewed", "content": "keep"}]
        self.assertEqual(retryable_report_files(checkpoint, texts), ["ch0002.md"])

    def test_retry_failed_rechecks_only_a_source_stale_chapter(self):
        texts = {"ch0001.md": "changed", "ch0002.md": "unchanged"}
        checkpoint = [
            {"file": "ch0001.md", "review_coverage": "reviewed", "source_sha256": "old hash"},
            {
                "file": "ch0002.md",
                "review_coverage": "reviewed",
                "source_sha256": hashlib.sha256(b"unchanged").hexdigest(),
            },
        ]
        self.assertEqual(retryable_report_files(checkpoint, texts), ["ch0001.md"])

    def test_legacy_success_rows_are_backfilled_without_re_review(self):
        rows = normalize_review_coverage([
            {"file": "ch0001.md", "content": "already reviewed"},
            {"file": "ch0002.md", "error": "timeout"},
        ])
        self.assertEqual([row["review_coverage"] for row in rows], ["reviewed", "failed_after_retries"])
        self.assertEqual(rows[0]["review_coverage_source"], "legacy_report_backfill")


if __name__ == "__main__":
    unittest.main()
