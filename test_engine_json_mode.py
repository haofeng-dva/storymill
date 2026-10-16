import io
import json
import os
import unittest
import urllib.error
from unittest.mock import patch

from engine import LLM, WritingEngine, assemble_full_book, chapter_generation_blocks, serial_recovery_start


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return b'{"choices":[{"message":{"content":"{}"}}],"usage":{}}'


class _ResponsesResponse(_Response):
    def read(self):
        return b'{"output":[{"type":"message","content":[{"type":"output_text","text":"{}"}]}]}'


class _HttpErrorResponse:
    def read(self):
        return b'unsupported endpoint'

    def close(self):
        pass


class _Transient404Response:
    def read(self):
        return b'404 page not found'

    def close(self):
        pass


class JsonModeTests(unittest.TestCase):
    def test_assemble_full_book_requires_all_chapters_in_order(self):
        story_dir = "story"
        sources = {
            os.path.join(story_dir, "ch0001.md"): "first",
            os.path.join(story_dir, "ch0002.md"): "second",
        }
        written = {}

        class Capture(io.StringIO):
            def __init__(self, path):
                super().__init__()
                self.path = path

            def close(self):
                if not self.closed:
                    written[self.path] = self.getvalue()
                super().close()

        def fake_open(path, mode="r", **_kwargs):
            if "r" in mode:
                return io.StringIO(sources[path])
            return Capture(path)

        with patch("engine.os.path.isfile", side_effect=lambda path: path in sources), patch("builtins.open", side_effect=fake_open):
            full_path = assemble_full_book(story_dir, 2)
        self.assertEqual(full_path, os.path.join(story_dir, "full.md"))
        self.assertEqual(written[full_path], "first\n\nsecond")
        with patch("engine.os.path.isfile", return_value=False):
            with self.assertRaisesRegex(ValueError, "ch0003.md"):
                assemble_full_book(story_dir, 3)

    def test_serial_chapters_reject_parallel_blocks(self):
        self.assertEqual(chapter_generation_blocks(1), 1)
        self.assertEqual(chapter_generation_blocks(2), 1)

    def test_serial_recovery_starts_only_at_first_recoverable_gap(self):
        rows = {
            2: {"failure_type": "network_error"},
            3: {"failure_type": "dependency_blocked"},
        }
        self.assertEqual(serial_recovery_start([2, 3], rows, lambda chapter: chapter == 2), 2)
        self.assertIsNone(serial_recovery_start([2, 3], rows, lambda chapter: False))
        rows[2]["failure_type"] = "quality_failure"
        self.assertIsNone(serial_recovery_start([2, 3], rows, lambda chapter: True))

    def test_ark_json_mode_uses_server_side_json_constraint(self):
        llm = LLM({"llm": {
            "base_url": "https://example.invalid/v1", "api_key_env": "TEST_KEY",
            "models": {"outline": "test-model"},
        }})
        with patch("urllib.request.urlopen", return_value=_Response()) as urlopen:
            result = llm._chat_ark("test-model", [{"role": "user", "content": "x"}], 10, 0.1, json_mode=True)
        body = json.loads(urlopen.call_args.args[0].data.decode("utf-8"))
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 90)
        self.assertEqual(result["content"], "{}")

    def test_gpt55_uses_configured_responses_gateway_contract(self):
        llm = LLM({"llm": {
            "base_url": "https://example.invalid/v1", "api_key_env": "TEST_KEY",
            "models": {"outline": "gpt-5.5"},
            "relay": {"wire_api": "responses", "headers": {"x-openai-actor-authorization": "local-image-extension"}},
        }})
        with patch.dict(os.environ, {"OPENAI_RELAY_BASE": "https://relay.example", "OPENAI_RELAY_KEY": "key"}):
            llm.relay_base = os.environ["OPENAI_RELAY_BASE"]
            llm.relay_key = os.environ["OPENAI_RELAY_KEY"]
            with patch("urllib.request.urlopen", return_value=_ResponsesResponse()) as urlopen:
                result = llm.chat("outline", [{"role": "user", "content": "x"}], 10, 0.1)
        self.assertEqual(result["content"], "{}")
        request = urlopen.call_args.args[0]
        self.assertTrue(request.full_url.endswith("/v1/responses"))
        self.assertEqual(request.get_header("X-openai-actor-authorization"), "local-image-extension")

    def test_responses_gateway_does_not_silently_fallback_to_chat(self):
        llm = LLM({"llm": {
            "base_url": "https://example.invalid/v1", "api_key_env": "TEST_KEY",
            "models": {"outline": "gpt-5.5"},
            "relay": {"wire_api": "responses"},
        }})
        llm.relay_base = "https://relay.example"
        llm.relay_key = "key"
        error = urllib.error.HTTPError("https://relay.example/v1/responses", 426, "Upgrade Required", {}, _HttpErrorResponse())
        with patch("urllib.request.urlopen", side_effect=error) as urlopen:
            result = llm.chat("outline", [{"role": "user", "content": "x"}], 10, 0.1)
        self.assertIn("HTTP 426", result["error"])
        self.assertEqual(urlopen.call_count, 1)

    def test_final_editor_can_share_writer_chat_route(self):
        llm = LLM({"llm": {
            "base_url": "https://example.invalid/v1", "api_key_env": "TEST_KEY",
            "models": {"writer": "gpt-5.5", "final_editor": "gpt-5.5"},
            "relay": {
                "wire_api": "responses",
                "role_wire_apis": {"writer": "chat_completions", "final_editor": "chat_completions"},
                "headers": {"x-openai-actor-authorization": "local-image-extension"},
            },
        }})
        llm.relay_base = "https://relay.example"
        llm.relay_key = "key"
        with patch("urllib.request.urlopen", return_value=_Response()) as urlopen:
            result = llm.chat("final_editor", [{"role": "user", "content": "x"}], 10, 0.1, json_mode=True)
        self.assertEqual(result["content"], "{}")
        request = urlopen.call_args.args[0]
        self.assertTrue(request.full_url.endswith("/v1/chat/completions"))
        self.assertEqual(request.get_header("X-openai-actor-authorization"), "local-image-extension")
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["response_format"], {"type": "json_object"})

    def test_outline_can_share_writer_chat_route(self):
        llm = LLM({"llm": {
            "base_url": "https://example.invalid/v1", "api_key_env": "TEST_KEY",
            "models": {"writer": "gpt-5.6-sol", "outline": "gpt-5.6-sol"},
            "relay": {"role_wire_apis": {"writer": "chat_completions", "outline": "chat_completions"}},
        }})
        llm.relay_base = "https://relay.example"
        llm.relay_key = "key"
        with patch("urllib.request.urlopen", return_value=_Response()) as urlopen:
            result = llm.chat("outline", [{"role": "user", "content": "x"}], 10, 0.1)
        self.assertEqual(result["content"], "{}")
        self.assertTrue(urlopen.call_args.args[0].full_url.endswith("/v1/chat/completions"))

    def test_chat_completions_retries_transient_gateway_error(self):
        llm = LLM({"llm": {
            "base_url": "https://example.invalid/v1", "api_key_env": "TEST_KEY",
            "models": {"writer": "gpt-5.6-sol"},
            "relay": {"role_wire_apis": {"writer": "chat_completions"}},
        }})
        llm.relay_base = "https://relay.example"
        llm.relay_key = "key"
        error = urllib.error.HTTPError(
            "https://relay.example/v1/chat/completions", 504,
            "Gateway Time-out", {}, _HttpErrorResponse(),
        )
        with patch("urllib.request.urlopen", side_effect=[error, _Response()]) as urlopen:
            with patch("engine.time.sleep") as sleep:
                result = llm.chat("writer", [{"role": "user", "content": "x"}], 10, 0.1)
        self.assertEqual(result["content"], "{}")
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(5)

    def test_chat_completions_retries_only_the_known_transient_404_page(self):
        llm = LLM({"llm": {
            "base_url": "https://example.invalid/v1", "api_key_env": "TEST_KEY",
            "models": {"writer": "gpt-5.6-sol"},
            "relay": {"role_wire_apis": {"writer": "chat_completions"}},
        }})
        llm.relay_base = "https://relay.example"
        llm.relay_key = "key"
        error = urllib.error.HTTPError(
            "https://relay.example/v1/chat/completions", 404,
            "Not Found", {}, _Transient404Response(),
        )
        with patch("urllib.request.urlopen", side_effect=[error, _Response()]) as urlopen:
            with patch("engine.time.sleep") as sleep:
                result = llm.chat("writer", [{"role": "user", "content": "x"}], 10, 0.1)
        self.assertEqual(result["content"], "{}")
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(5)

    def test_writer_falls_back_to_gpt55_after_one_gpt56_retry(self):
        llm = LLM({"llm": {
            "base_url": "https://example.invalid/v1", "api_key_env": "TEST_KEY",
            "models": {"writer": "gpt-5.6-sol"},
            "relay": {
                "role_wire_apis": {"writer": "chat_completions"},
                "writer_fallback": {
                    "model": "gpt-5.5", "primary_max_attempts": 2,
                    "primary_request_timeout_seconds": 70,
                },
            },
        }})
        llm.relay_base = "https://relay.example"
        llm.relay_key = "key"
        error = urllib.error.HTTPError(
            "https://relay.example/v1/chat/completions", 504,
            "Gateway Time-out", {}, _HttpErrorResponse(),
        )
        with patch("urllib.request.urlopen", side_effect=[error, error, _Response(), _Response()]) as urlopen:
            with patch("engine.time.sleep") as sleep:
                result = llm.chat("writer", [{"role": "user", "content": "x"}], 10, 0.1)
                next_result = llm.chat("writer", [{"role": "user", "content": "y"}], 10, 0.1)
        self.assertEqual(result["content"], "{}")
        self.assertEqual(next_result["content"], "{}")
        self.assertEqual(result["fallback_from"], "gpt-5.6-sol")
        self.assertEqual(urlopen.call_count, 4)
        self.assertEqual(
            [json.loads(call.args[0].data.decode("utf-8"))["model"] for call in urlopen.call_args_list],
            ["gpt-5.6-sol", "gpt-5.6-sol", "gpt-5.5", "gpt-5.5"],
        )
        self.assertEqual([call.kwargs["timeout"] for call in urlopen.call_args_list], [70, 70, 300, 300])
        sleep.assert_called_once_with(5)

    def test_relay_outline_uses_four_bounded_requests(self):
        class OutlineLLM:
            def __init__(self):
                self.models = {"outline": "gpt-5.6-sol"}
                self.calls = []

            def chat(self, role, messages, max_tokens, temperature):
                self.calls.append((role, messages, max_tokens, temperature))
                return {"content": f"part {len(self.calls)}", "elapsed": 1}

        engine = WritingEngine()
        engine.llm = OutlineLLM()
        outline = engine.write_outline_segmented("LGBTQ+ romance")
        self.assertEqual(len(engine.llm.calls), 4)
        self.assertEqual([call[2] for call in engine.llm.calls], [600, 600, 600, 600])
        self.assertIn("chapters 1-4", engine.llm.calls[1][1][-1]["content"])
        self.assertIn("chapters 9-12", engine.llm.calls[3][1][-1]["content"])
        self.assertEqual(outline["elapsed"], 4)

    def test_relay_world_bible_uses_two_bounded_requests(self):
        class OutlineLLM:
            def __init__(self):
                self.calls = []

            def chat(self, role, messages, max_tokens, temperature):
                self.calls.append((role, messages, max_tokens, temperature))
                return {"content": f"part {len(self.calls)}", "elapsed": 1}

        engine = WritingEngine()
        engine.llm = OutlineLLM()
        bible = engine.write_world_bible("LGBTQ+ romance", "outline text")
        self.assertEqual(len(engine.llm.calls), 2)
        self.assertEqual([call[2] for call in engine.llm.calls], [600, 600])
        self.assertIn("World-bible part 1", engine.llm.calls[1][1][-1]["content"])
        self.assertEqual(bible["elapsed"], 2)

    def test_relay_chapter_uses_configured_150_word_segments(self):
        class WriterLLM:
            def chat(self, role, messages, max_tokens, temperature):
                return {"content": "word " * 150, "elapsed": 1}

        engine = WritingEngine()
        engine.llm = WriterLLM()
        engine.w["segment_words"] = 150
        chapter = engine._write_chapter_segmented("direction", "outline", "bible", 1, 1, 300)
        self.assertEqual(len(chapter["content"].split()), 300)

    def test_relay_chapter_retries_only_the_failed_segment(self):
        class WriterLLM:
            def __init__(self):
                self.calls = 0

            def chat(self, role, messages, max_tokens, temperature):
                self.calls += 1
                if self.calls == 2:
                    return {"error": "HTTP 504: Gateway Time-out"}
                return {"content": "word " * 150, "elapsed": 1}

        engine = WritingEngine()
        engine.llm = WriterLLM()
        engine.w["segment_words"] = 150
        engine.w["segment_recovery_attempts"] = 1
        engine.w["segment_recovery_wait_seconds"] = 0
        chapter = engine._write_chapter_segmented("direction", "outline", "bible", 1, 1, 300)
        self.assertEqual(engine.llm.calls, 3)
        self.assertEqual(len(chapter["content"].split()), 300)

    def test_dark_romance_is_not_routed_to_suspense_anchors(self):
        engine = WritingEngine()
        self.assertEqual(engine.detect_genre("A dark adult romance with crime."), "dark_romance")
        self.assertEqual(engine._load_anchors("dark_romance"), [])

    def test_lgbt_romance_does_not_inherit_suspense_anchors(self):
        engine = WritingEngine()
        direction = "An LGBTQ+ second-chance romance with missing family footage."
        self.assertEqual(engine.detect_genre(direction), "lgbt_romance")
        self.assertEqual(engine._load_anchors("lgbt_romance"), [])


if __name__ == "__main__":
    unittest.main()
