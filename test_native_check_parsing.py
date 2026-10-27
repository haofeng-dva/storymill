import json
import unittest
import hashlib
from unittest.mock import patch

from native_check import (
    DeepSeekReviewer,
    GPTNativeReviewer,
    _acceptance_user_prompt,
    review_chapter_contract_evidence,
    acceptance_review_with_fallback,
    build_acceptance_reviewers,
    acceptance_repair_feedback,
    blocking_review_findings,
    editorial_review_findings,
    extract_chapter_requirements,
    freeze_acceptance_issue_ledger,
    merge_retry_results,
    normalize_review_coverage,
    parse_acceptance_review,
    parse_scores,
    request_json_bounded,
    request_sse_json_bounded,
    review_failure_status,
    review_chapter_acceptance,
    review_texts_with_retries,
    retryable_report_files,
)


class NativeCheckParsingTests(unittest.TestCase):
    def test_gpt_reconnects_once_for_tls_but_not_for_content_errors(self):
        reviewer = GPTNativeReviewer({"OPENAI_RELAY_BASE": "https://relay.example", "OPENAI_RELAY_KEY": "key"}, model="gpt-5.5", primary_attempts=1, fallback_model="", fallback_attempts=1)
        with patch.object(reviewer, "_call_model", side_effect=[
            {"error": "URLError: SSL UNEXPECTED_EOF_WHILE_READING", "stream": True},
            {"content": "{}", "stream": True},
        ]) as call_model, patch("native_check.time.sleep"):
            result = reviewer._call([], max_tokens=1, stream=True)
        self.assertEqual(result["content"], "{}")
        self.assertEqual(call_model.call_count, 2)

        with patch.object(reviewer, "_call_model", return_value={"error": "invalid reviewer response"}) as call_model:
            result = reviewer._call([], max_tokens=1)
        self.assertIn("error", result)
        self.assertEqual(call_model.call_count, 1)
    def test_quality_gate_and_editorial_findings_have_separate_exit_policies(self):
        rows = [{"file": "ch0001.md", "editorial_findings": ["A concrete issue"]}]
        passing_gate = {"release_ready": True}
        self.assertEqual(
            review_failure_status(rows, passing_gate, require_quality_gate=True),
            (False, False, ["ch0001.md"]),
        )
        self.assertEqual(
            review_failure_status(
                rows, passing_gate, require_quality_gate=True, fail_on_editorial_findings=True,
            ),
            (True, False, ["ch0001.md"]),
        )

    def test_acceptance_uses_next_reviewer_when_first_output_fails_schema_validation(self):
        class InvalidQuoteReviewer:
            model = "first-model"
            acceptance_attempts = 1
            acceptance_max_tokens = 700
            acceptance_timeout_seconds = 45

            def _call(self, *_args, **_kwargs):
                return {"content": json.dumps({
                    "outcomes": [{
                        "id": "ch0001-contract-01", "status": "met",
                        "evidence_quotes": ['"Mara found the ledger."'],
                        "insertion_anchor": None, "reason": "shown",
                    }],
                    "findings": [],
                })}

        class ValidReviewer(InvalidQuoteReviewer):
            model = "second-model"

            def _call(self, *_args, **_kwargs):
                return {"content": json.dumps({
                    "outcomes": [{
                        "id": "ch0001-contract-01", "status": "met",
                        "evidence_quotes": ["Mara found the ledger."],
                        "insertion_anchor": None, "reason": "shown",
                    }],
                    "findings": [],
                })}

        result = review_chapter_acceptance(
            "ch0001.md", "Mara found the ledger.",
            [{"id": "ch0001-contract-01", "requirement": "Mara finds the ledger."}],
            [("first", InvalidQuoteReviewer()), ("second", ValidReviewer())],
        )

        self.assertNotIn("error", result)
        self.assertEqual(result["provider"], "second-model (second)")

    def test_acceptance_can_route_gpt_review_to_a_separate_model(self):
        cfg = {"llm": {
            "base_url": "https://ark.example",
            "api_key_env": "ARK_API_KEY",
            "models": {"reviewer": "glm-test"},
            "relay": {"headers": {}, "writer_fallback": {"model": "gpt-5.6-sol"}},
        }}
        env = {"OPENAI_RELAY_BASE": "https://relay.example", "OPENAI_RELAY_KEY": "key"}
        with patch("native_check.load_env", return_value=env), \
             patch("native_check.load_json", return_value=cfg):
            reviewers = build_acceptance_reviewers({
                "gpt_model": "gpt-5.6-terra",
                "gpt_primary_attempts": 1,
                "gpt_fallback_model": "gpt-5.6-sol",
                "gpt_fallback_attempts": 1,
                "gpt_max_output_tokens": 1400,
                "gpt_request_timeout_seconds": 150,
                "glm_max_output_tokens": 700,
                "glm_attempts": 4,
                "glm_request_timeout_seconds": 90,
            })

        gpt = dict(reviewers)["gpt"]
        glm = dict(reviewers)["glm"]
        self.assertEqual(reviewers[0][0], "gpt")
        self.assertEqual(gpt.model, "gpt-5.6-terra")
        self.assertEqual(gpt.primary_attempts, 1)
        self.assertEqual(gpt.fallback_model, "gpt-5.6-sol")
        self.assertEqual(gpt.fallback_attempts, 1)
        self.assertEqual(gpt.acceptance_max_tokens, 1400)
        self.assertEqual(gpt.acceptance_timeout_seconds, 150)
        self.assertEqual(glm.acceptance_max_tokens, 700)
        self.assertEqual(glm.acceptance_attempts, 4)
        self.assertEqual(glm.acceptance_timeout_seconds, 90)

    def test_acceptance_can_limit_the_provider_chain_to_gpt(self):
        cfg = {"llm": {
            "base_url": "https://ark.example",
            "api_key_env": "ARK_API_KEY",
            "models": {"reviewer": "glm-test"},
            "relay": {"headers": {}, "writer_fallback": {"model": "gpt-5.5"}},
        }}
        env = {"OPENAI_RELAY_BASE": "https://relay.example", "OPENAI_RELAY_KEY": "key"}
        with patch("native_check.load_env", return_value=env), \
             patch("native_check.load_json", return_value=cfg):
            reviewers = build_acceptance_reviewers({
                "gpt_model": "gpt-5.6-terra",
                "gpt_primary_attempts": 1,
                "gpt_fallback_model": "",
                "reviewer_order": ["gpt"],
            })

        self.assertEqual([name for name, _reviewer in reviewers], ["gpt"])

    def test_acceptance_stream_uses_a_distinct_idle_budget(self):
        cfg = {"llm": {
            "base_url": "https://ark.example",
            "api_key_env": "ARK_API_KEY",
            "models": {"reviewer": "glm-test"},
            "relay": {"headers": {}, "writer_fallback": {"model": "gpt-5.5"}},
        }}
        env = {"OPENAI_RELAY_BASE": "https://relay.example", "OPENAI_RELAY_KEY": "key"}
        with patch("native_check.load_env", return_value=env), \
             patch("native_check.load_json", return_value=cfg):
            reviewers = build_acceptance_reviewers({
                "gpt_model": "gpt-5.5", "reviewer_order": ["gpt"],
                "gpt_request_timeout_seconds": 300,
                "gpt_stream": True, "gpt_stream_idle_timeout_seconds": 90,
            })

        reviewer = dict(reviewers)["gpt"]
        self.assertEqual(reviewer.acceptance_timeout_seconds, 300)
        self.assertTrue(reviewer.acceptance_stream)
        self.assertEqual(reviewer.acceptance_idle_timeout_seconds, 90)

    def test_acceptance_retries_only_a_failed_glm_attempt_before_fallback(self):
        class FlakyReviewer:
            model = "glm-test"
            acceptance_attempts = 2
            acceptance_max_tokens = 700
            acceptance_timeout_seconds = 90

            def __init__(self):
                self.calls = 0

            def _call(self, *_args, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    return {"error": "transient timeout"}
                return {"content": "{\"outcomes\": [], \"findings\": []}"}

        reviewer = FlakyReviewer()
        content, provider = acceptance_review_with_fallback("Chapter.", [], [("glm", reviewer)])

        self.assertEqual(reviewer.calls, 2)
        self.assertEqual(content, "{\"outcomes\": [], \"findings\": []}")
        self.assertEqual(provider, "glm-test (glm)")

    def test_acceptance_passes_stream_options_only_to_the_gpt_reviewer(self):
        class GlmReviewer:
            model = "glm-test"
            acceptance_attempts = 1
            acceptance_max_tokens = 700
            acceptance_timeout_seconds = 180

            def _call(self, _messages, max_tokens, request_timeout_seconds):
                self.args = (max_tokens, request_timeout_seconds)
                return {"content": "{\"outcomes\": [], \"findings\": []}"}

        reviewer = GlmReviewer()
        content, provider = acceptance_review_with_fallback(
            "chapter", [], [("glm", reviewer)], include_failures=False,
        )

        self.assertEqual(content, "{\"outcomes\": [], \"findings\": []}")
        self.assertEqual(provider, "glm-test (glm)")
        self.assertEqual(reviewer.args, (700, 180))

    def test_deepseek_acceptance_uses_configured_stream_options(self):
        cfg = {
            "llm": {
                "base_url": "https://example.invalid/v1",
                "api_key_env": "TEST_KEY",
                "models": {"reviewer": "glm-test"},
            },
        }
        reviewer = DeepSeekReviewer(cfg, {"TEST_KEY": "key"})
        reviewer.acceptance_attempts = 1
        reviewer.acceptance_max_tokens = 700
        reviewer.acceptance_timeout_seconds = 300
        reviewer.acceptance_stream = True
        reviewer.acceptance_idle_timeout_seconds = 90

        with patch("native_check.request_sse_json_bounded", return_value=(
            {"choices": [{"message": {"content": "{}"}}]}, None,
        )) as stream_request:
            content, provider = acceptance_review_with_fallback(
                "chapter", [], [("glm", reviewer)], include_failures=False,
            )

        self.assertEqual(content, "{}")
        self.assertEqual(provider, "glm-test (glm)")
        self.assertEqual(stream_request.call_args.kwargs, {
            "idle_timeout": 90, "total_timeout": 300,
        })

    def test_contract_only_acceptance_omits_open_ended_editorial_instruction(self):
        prompt = _acceptance_user_prompt(
            "Chapter text.", [{"id": "required", "requirement": "A result occurs."}],
            editorial_findings=False,
        )

        self.assertIn("findings must be []", prompt)
        self.assertNotIn("Report every concrete problem", prompt)

    def test_contract_evidence_checks_each_requirement_and_validates_literal_quotes(self):
        class Reviewer:
            model = "glm-test"
            acceptance_timeout_seconds = 90
            acceptance_contract_max_tokens = 240

            def __init__(self):
                self.calls = 0

            def _call(self, _messages, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    return {"content": json.dumps({
                        "id": "first", "status": "met", "evidence_index": 0, "reason": "literal proof",
                    })}
                return {"content": json.dumps({
                    "id": "second", "status": "missing", "evidence_index": None, "reason": "not stated",
                })}

        text = "The gate opened.\n\nThe rain stopped."
        review = review_chapter_contract_evidence("ch0001.md", text, [
            {"id": "first", "requirement": "The gate opens."},
            {"id": "second", "requirement": "A bell rings."},
        ], [("glm", Reviewer())])

        self.assertNotIn("error", review)
        self.assertEqual([item["status"] for item in review["outcomes"]], ["met", "missing"])
        self.assertEqual(review["findings"], [])

    def test_contract_evidence_candidates_keep_adjacent_actions_together(self):
        from native_check import _contract_evidence_candidates

        text = (
            "The wolf woke.\n\nClaws scraped linoleum.\n\nOnce.\n\nTwice.\n\n"
            "A third time.\n\nThree parallel strokes marked the floor.\n\nThe wolf went limp."
        )
        candidates = _contract_evidence_candidates(
            text, "A wolf scratches three lines before collapsing.", limit=4,
        )

        self.assertTrue(any(
            "Claws scraped linoleum." in candidate
            and "A third time." in candidate
            and "The wolf went limp." in candidate
            for candidate in candidates
        ))
        self.assertTrue(all(candidate in text for candidate in candidates))

    def test_contract_evidence_candidates_handle_collapsing_inflection(self):
        from native_check import _contract_evidence_candidates

        text = "The wolf scratched three grooves. Then the wolf collapsed."
        candidates = _contract_evidence_candidates(
            text, "A wolf scratches three lines before collapsing.", limit=4,
        )

        self.assertTrue(any("scratched three grooves" in candidate and "collapsed" in candidate for candidate in candidates))

    def test_contract_evidence_candidates_handle_agree_inflection(self):
        from native_check import _contract_evidence_candidates

        text = "Elias named his boundaries. Ronan held his gaze. I agree, Ronan said."
        candidates = _contract_evidence_candidates(
            text, "Ronan agreed to Elias's boundaries.", limit=4,
        )

        self.assertTrue(any("I agree" in candidate for candidate in candidates))

    def test_acceptance_failure_keeps_each_provider_and_model(self):
        class FailingReviewer:
            acceptance_attempts = 1
            acceptance_max_tokens = 700
            acceptance_timeout_seconds = 45

            def __init__(self, model, result):
                self.model = model
                self.result = result

            def _call(self, *_args, **_kwargs):
                return self.result

        result = review_chapter_acceptance(
            "ch0001.md", "Mara found the ledger.",
            [{"id": "ch0001-contract-01", "requirement": "Mara finds the ledger."}],
            [
                ("glm", FailingReviewer("glm-test", {"error": "TimeoutError: The read operation timed out"})),
                ("gpt", FailingReviewer("gpt-5.6-terra", {
                    "error": "gpt fallback exhausted",
                    "attempt_failures": [
                        {"model": "gpt-5.6-terra", "attempt": 1, "error": "HTTP 504: Gateway Time-out"},
                        {"model": "gpt-5.5", "attempt": 1, "error": "HTTP 504: Gateway Time-out"},
                    ],
                })),
                ("gemini", FailingReviewer("gemini-test", {
                    "error": "URLError: SSL UNEXPECTED_EOF_WHILE_READING",
                })),
            ],
        )

        self.assertIn("error", result)
        self.assertEqual(result["provider_failures"], [
            {"provider": "glm", "model": "glm-test", "attempt": 1,
             "error": "TimeoutError: The read operation timed out", "failure_type": "upstream_timeout"},
            {"provider": "gpt", "model": "gpt-5.6-terra", "attempt": 1,
             "error": "HTTP 504: Gateway Time-out", "failure_type": "gateway_timeout"},
            {"provider": "gpt", "model": "gpt-5.5", "attempt": 1,
             "error": "HTTP 504: Gateway Time-out", "failure_type": "gateway_timeout"},
            {"provider": "gemini", "model": "gemini-test", "attempt": 1,
             "error": "URLError: SSL UNEXPECTED_EOF_WHILE_READING", "failure_type": "proxy_tls_error"},
        ])

    def test_acceptance_success_keeps_prior_provider_failures(self):
        class FailingReviewer:
            model = "glm-test"
            acceptance_attempts = 1
            acceptance_max_tokens = 700
            acceptance_timeout_seconds = 45

            def _call(self, *_args, **_kwargs):
                return {"error": "TimeoutError: The read operation timed out"}

        class ValidReviewer:
            model = "gpt-test"
            acceptance_attempts = 1
            acceptance_max_tokens = 700
            acceptance_timeout_seconds = 45

            def _call(self, *_args, **_kwargs):
                return {"content": json.dumps({
                    "outcomes": [{
                        "id": "ch0001-contract-01", "status": "met",
                        "evidence_quotes": ["Mara found the ledger."],
                        "insertion_anchor": None, "reason": "shown",
                    }],
                    "findings": [],
                })}

        result = review_chapter_acceptance(
            "ch0001.md", "Mara found the ledger.",
            [{"id": "ch0001-contract-01", "requirement": "Mara finds the ledger."}],
            [("glm", FailingReviewer()), ("gpt", ValidReviewer())],
        )

        self.assertEqual(result["provider"], "gpt-test (gpt)")
        self.assertEqual(result["provider_failures"], [{
            "provider": "glm", "model": "glm-test", "attempt": 1,
            "error": "TimeoutError: The read operation timed out", "failure_type": "upstream_timeout",
        }])

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
            {"id": "ch0001-requirement-01", "status": "met", "evidence_quotes": ["Mara found the ledger."], "insertion_anchor": null, "reason": "shown"},
            {"id": "ch0001-requirement-02", "status": "met", "evidence_quotes": ["June refused to leave."], "insertion_anchor": null, "reason": "shown"}
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
          "outcomes": [{"id": "ch0001-requirement-01", "status": "met", "evidence_quotes": ["Mara found the ledger."], "insertion_anchor": null, "reason": "shown"}],
          "findings": [{"category": "duplicate_scene", "evidence_quotes": ["Mara found the ledger."], "reason": "repeated", "repair_scope": "scene"}]
        }"""
        self.assertIn("error", parse_acceptance_review(review, text, requirements))

    def test_acceptance_review_accepts_missing_outcome_with_unique_paragraph_anchor(self):
        text = "Mara enters the office.\n\nShe opens the tax box."
        requirements = [{"id": "ch0001-requirement-01", "requirement": "Mara catalogues the bills."}]
        review = """{
          "outcomes": [{"id": "ch0001-requirement-01", "status": "missing", "evidence_quotes": [], "insertion_anchor": "Mara enters the office.", "reason": "The bills are never catalogued."}],
          "findings": []
        }"""
        parsed = parse_acceptance_review(review, text, requirements)
        self.assertNotIn("error", parsed)
        self.assertEqual(parsed["outcomes"][0]["insertion_anchor"], "Mara enters the office.")

    def test_acceptance_review_rejects_missing_outcome_without_unique_paragraph_anchor(self):
        text = "Mara enters the office. Mara enters the office."
        requirements = [{"id": "ch0001-requirement-01", "requirement": "Mara catalogues the bills."}]
        review = """{
          "outcomes": [{"id": "ch0001-requirement-01", "status": "missing", "evidence_quotes": [], "insertion_anchor": "Mara enters the office.", "reason": "The bills are never catalogued."}],
          "findings": []
        }"""
        self.assertIn("error", parse_acceptance_review(review, text, requirements))

    def test_acceptance_review_accepts_unclear_outcome_with_evidence_and_insertion_anchor(self):
        text = "Mara sorts the files.\n\nDinner waits downstairs."
        requirements = [{"id": "ch0001-requirement-01", "requirement": "Mara catalogues the bills before Sunday dinner."}]
        review = """{
          "outcomes": [{"id": "ch0001-requirement-01", "status": "unclear", "evidence_quotes": ["Mara sorts the files."], "insertion_anchor": "Mara sorts the files.", "reason": "The files are sorted, but bills and Sunday dinner are not explicit."}],
          "findings": []
        }"""
        parsed = parse_acceptance_review(review, text, requirements)
        self.assertNotIn("error", parsed)
        self.assertEqual(parsed["outcomes"][0]["insertion_anchor"], "Mara sorts the files.")

    def test_acceptance_review_rejects_unclear_outcome_without_evidence_or_anchor(self):
        text = "Mara sorts the files.\n\nDinner waits downstairs."
        requirements = [{"id": "ch0001-requirement-01", "requirement": "Mara catalogues the bills before Sunday dinner."}]
        review = """{
          "outcomes": [{"id": "ch0001-requirement-01", "status": "unclear", "evidence_quotes": [], "insertion_anchor": null, "reason": "The outcome is uncertain."}],
          "findings": []
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

    def test_frozen_issue_ledger_requires_every_initial_item_on_recheck(self):
        initial_text = "Mara found the ledger twice."
        ledger = freeze_acceptance_issue_ledger(1, {
            "source_sha256": hashlib.sha256(initial_text.encode("utf-8")).hexdigest(),
            "findings": [{
                "category": "clarity_idiomaticity", "evidence_quotes": [initial_text],
                "reason": "The sentence repeats.", "repair_scope": "sentence",
            }],
        })
        self.assertEqual(ledger["items"][0]["id"], "ch0001-ledger-01")
        text = "Mara found the ledger."
        requirements = [{"id": "ch0001-requirement-01", "requirement": "Mara finds the ledger."}]
        incomplete = """{
          "outcomes": [{"id": "ch0001-requirement-01", "status": "met", "evidence_quotes": ["Mara found the ledger."], "insertion_anchor": null, "reason": "shown"}],
          "findings": [],
          "ledger_assessment": []
        }"""
        self.assertIn("error", parse_acceptance_review(incomplete, text, requirements, ledger))

    def test_recheck_keeps_new_editorial_finding_separate_from_frozen_ledger(self):
        initial_text = "Mara found the ledger twice."
        ledger = freeze_acceptance_issue_ledger(1, {
            "source_sha256": hashlib.sha256(initial_text.encode("utf-8")).hexdigest(),
            "findings": [{
                "category": "clarity_idiomaticity", "evidence_quotes": [initial_text],
                "reason": "The sentence repeats.", "repair_scope": "sentence",
            }],
        })
        text = "Mara found the ledger. Her pulse turned blunt."
        requirements = [{"id": "ch0001-requirement-01", "requirement": "Mara finds the ledger."}]
        recheck = """{
          "outcomes": [{"id": "ch0001-requirement-01", "status": "met", "evidence_quotes": ["Mara found the ledger."], "insertion_anchor": null, "reason": "shown"}],
          "findings": [{"category": "clarity_idiomaticity", "evidence_quotes": ["Her pulse turned blunt."], "reason": "New wording concern.", "repair_scope": "sentence"}],
          "ledger_assessment": [{"id": "ch0001-ledger-01", "status": "resolved", "evidence_quotes": [], "reason": "The duplicate wording is gone."}]
        }"""
        parsed = parse_acceptance_review(recheck, text, requirements, ledger)
        self.assertNotIn("error", parsed)
        self.assertEqual(parsed["ledger_assessment"][0]["status"], "resolved")
        self.assertEqual(parsed["findings"][0]["reason"], "New wording concern.")

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

    def test_sse_request_uses_content_only_and_keeps_terminal_usage(self):
        class Response:
            def __iter__(self):
                return iter([
                    b'data: {"choices":[{"delta":{"reasoning_content":"hidden"}}]}\n',
                    b'data: {"choices":[{"delta":{"content":"{\\\"outcomes\\\":[]"}}]}\n',
                    b'data: {"choices":[{"delta":{"content":",\\\"findings\\\":[]}"}}]}\n',
                    b'data: {"choices":[],"usage":{"total_tokens":17}}\n',
                    b'data: [DONE]\n',
                ])

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

        with patch("native_check.urllib.request.urlopen", return_value=Response()):
            data, error = request_sse_json_bounded(object(), idle_timeout=1, total_timeout=2)

        self.assertIsNone(error)
        self.assertEqual(data["choices"][0]["message"]["content"], '{"outcomes":[],"findings":[]}')
        self.assertEqual(data["usage"], {"total_tokens": 17})

    def test_sse_request_rejects_a_truncated_json_stream(self):
        class Response:
            def __iter__(self):
                return iter([b'data: {"choices":[{"delta":{"content":"{\\\"outcomes\\\":"}}]}\n'])

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

        with patch("native_check.urllib.request.urlopen", return_value=Response()):
            data, error = request_sse_json_bounded(object(), idle_timeout=1, total_timeout=2)

        self.assertIsNone(data)
        self.assertEqual(error, "StreamInterruptedError: stream ended without a terminal event")

    def test_sse_request_keeps_complete_json_when_relay_omits_terminal_event(self):
        class Response:
            def __iter__(self):
                return iter([b'data: {"choices":[{"delta":{"content":"{}"}}]}\n'])

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

        with patch("native_check.urllib.request.urlopen", return_value=Response()):
            data, error = request_sse_json_bounded(object(), idle_timeout=1, total_timeout=2)

        self.assertIsNone(error)
        self.assertEqual(data["choices"][0]["message"]["content"], "{}")

    def test_sse_request_accepts_a_terminal_choice_when_relay_omits_done(self):
        class Response:
            def __iter__(self):
                return iter([
                    b'data: {"choices":[{"delta":{"content":"{}"},"finish_reason":"stop"}]}\n',
                ])

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

        with patch("native_check.urllib.request.urlopen", return_value=Response()):
            data, error = request_sse_json_bounded(object(), idle_timeout=1, total_timeout=2)

        self.assertIsNone(error)
        self.assertEqual(data["choices"][0]["message"]["content"], "{}")

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
