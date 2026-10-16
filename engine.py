# -*- coding: utf-8 -*-
"""
engine.py — 英文写作引擎（P0 核心）

自研英文短篇产出模块，替代 InkOS 生产。
核心能力：
  1. 大纲生成（outline）
  2. 分章写作（每章目标词数硬校验，不符重写）
  3. 英文 AI 味抑制（写前注入禁用词表 + 写后密度校验）
  4. 风格样本库锚点（anchor_examples 做 few-shot）
  5. 中文摘要（供看不懂英文的人判断方向）

用法:
    py -3 engine.py --direction "US-market domestic suspense..." --chapters 12 --words 700 --story-id test

模型: 火山方舟 Ark（deepseek-v4-flash 写作 / deepseek-v4-pro 审稿）
"""
import argparse
import json
import os
import re
import sys
import threading
import time
import urllib.request
import urllib.error
import uuid

import progress
from profiles import get_writer_profile
from state_store import StateStore
import quality_checks
import quality_gate
from distill.experience_runtime import ExperienceRuntime
from chapter_state import ChapterStatus, FailureType, classify_failure, is_auto_retryable
from chapter_tasks import ChapterTaskStore
from writing_status import write_status
from book_memory import BookMemory
from repair_validator import apply_scoped_patches, local_validate
from chapter_contract import (
    ARTIFACT_NAME, ChapterContractError, parse_compiler_response,
    planning_hash as chapter_contract_planning_hash, requirements_for_chapter,
    writer_contract_block,
)
from control_context import build_control_context, load_state_delta
from control_contracts import (
    ControlContractError, acceptance_requirements_for_chapter, load_control_bundle,
    sha256_text, source_hash,
)
from control_runtime import ControlledGenerationRuntime

HERE = os.path.dirname(os.path.abspath(__file__))


def chapter_generation_blocks(requested_blocks):
    """Serial chapters have a hard predecessor dependency.

    Keep the old option parseable for existing launch scripts, but never let it
    produce a later chapter without the actual state from its predecessor.
    """
    return 1


def serial_recovery_start(missing, rows, predecessor_available):
    """Return the only safe recovery entry point for a serial chapter chain."""
    if not missing:
        return None
    chapter_no = min(missing)
    failure_type = (rows.get(chapter_no) or {}).get("failure_type")
    if predecessor_available(chapter_no) and is_auto_retryable(failure_type):
        return chapter_no
    return None


def build_argument_parser():
    """Create the CLI parser without starting a production run."""
    ap = argparse.ArgumentParser(description="InkOS 替代：英文写作引擎")
    ap.add_argument("--direction", help="故事方向文本（与 --direction-file 二选一）")
    ap.add_argument("--direction-file", help="方向文件路径（由 direction_manager 生成）")
    ap.add_argument("--chapters", type=int, default=None, help="章节数（默认读 config）")
    ap.add_argument("--words", type=int, default=None, help="每章目标词数（默认读 config）")
    ap.add_argument("--story-id", default="test")
    ap.add_argument("--genre", default=None, help="题材目录名：domestic_suspense / webfiction / literary；不填则从 direction 自动检测")
    ap.add_argument("--outline-only", action="store_true", help="只出大纲，不写正文")
    ap.add_argument("--chapter", type=int, default=0, help="只写指定章（0=全部）")
    ap.add_argument("--extra-rules", default="", help="额外写作规则（从盲评迭代回灌），追加注入写章 prompt")
    ap.add_argument("--no-experiences", action="store_true", help="本次运行不读取蒸馏经验包，保留原写作提示")
    ap.add_argument(
        "--quality-gate", action="store_true", default=True,
        help="兼容旧命令；生产章节始终执行质量门禁、修复与复查",
    )
    ap.add_argument("--quality-min", type=int, default=4, help="质量门槛最低 naturalness（默认 4）")
    ap.add_argument("--parallel-blocks", type=int, default=None, help="兼容旧启动脚本；连续章节固定串行")
    ap.add_argument("--run-id", default="", help="运行 ID，用于章节状态审计")
    ap.add_argument("--writer-model", default="", help="仅覆盖本次运行的正文模型；不修改 config.json")
    ap.add_argument("--outline-model", default="", help="仅覆盖本次运行的大纲/设定模型；不修改 config.json")
    ap.add_argument("--resume", action="store_true", help="恢复指定 --run-id，跳过已完成章节")
    ap.add_argument("--append-chapters", type=int, default=0,
                    help="在已有故事末尾追加章节；12章只是试写批次，不代表全书结束")
    ap.add_argument("--controlled-generation", action="store_true",
                    help="启用章节合同/场景账本控制（默认关闭）")
    ap.add_argument("--chapter-contracts", action="store_true",
                    help="试用：将每章大纲编译为冻结的原子合同，供正文与验收共用")
    ap.add_argument("--control-dir", default="", help="控制产物目录（默认故事目录/control）")
    ap.add_argument("--control-dry-run", action="store_true",
                    help="仅校验受控生成输入和每章上下文，不调用正文模型")
    ap.add_argument("--control-fail-on", default="", help="预留：逗号分隔的控制失败类别")
    ap.add_argument("--control-repair-card", default="", help="预留：仅处理指定控制失败卡")
    ap.add_argument("--assemble-only", action="store_true", help="仅从已完成章节重建 full.md，不调用模型")
    return ap


def option_compatibility_error(args):
    """Reject modes that would define competing chapter completion contracts."""
    if args.controlled_generation and args.chapter_contracts:
        return "--chapter-contracts cannot be combined with --controlled-generation; controlled end_assertions are the acceptance contract"
    return None


def should_reuse_planning_artifact(args, path):
    """Controlled runs are bound to their compiled planning-input hash."""
    return bool(
        os.path.exists(path)
        and (args.resume or args.append_chapters or args.controlled_generation)
    )


def assemble_full_book(story_dir, chapter_count):
    """Build the deterministic full-book artifact from completed chapter files."""
    chapter_paths = [os.path.join(story_dir, f"ch{number:04d}.md") for number in range(1, chapter_count + 1)]
    missing = [path for path in chapter_paths if not os.path.isfile(path)]
    if missing:
        raise ValueError(f"cannot assemble full book; missing chapters: {', '.join(os.path.basename(path) for path in missing)}")
    joined = "\n\n".join(open(path, encoding="utf-8").read() for path in chapter_paths)
    full_path = os.path.join(story_dir, "full.md")
    open(full_path, "w", encoding="utf-8").write(joined)
    return full_path


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def lessons_path(story_id):
    """某本书独立的优化词条库路径（只影响本书，不碰全局规则包）。"""
    return os.path.join(HERE, "shorts", story_id, "lessons.json")


# 块状并行下，多线程会同时读写 lessons.json（读改写竞态），用一把锁串行化存取
_LESSON_LOCK = threading.Lock()


def load_lessons(story_id):
    """读某本书的优化词条，返回规则列表。"""
    path = lessons_path(story_id)
    with _LESSON_LOCK:
        if os.path.exists(path):
            try:
                d = load_json(path)
                return [x["rule"] for x in d.get("lessons", [])]
            except Exception:
                return []
    return []


def add_lesson(story_id, rule, source=""):
    """往某本书的 lessons 追加一条词条（去重），只写入本书库，不影响其他书和全局。"""
    path = lessons_path(story_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with _LESSON_LOCK:
        d = {"lessons": []}
        if os.path.exists(path):
            try:
                d = load_json(path)
            except Exception:
                d = {"lessons": []}
        rules = [x["rule"] for x in d.get("lessons", [])]
        if rule and rule not in rules:
            d.setdefault("lessons", []).append({"rule": rule, "source": source, "ts": time.strftime("%Y-%m-%dT%H:%M:%S")})
            json.dump(d, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


def load_env():
    """从 .env 读键值到 os.environ，再用 keys.local.json 覆盖（使用者自己的 key 优先）。"""
    path = os.path.join(HERE, ".env")
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    # 使用者 key 覆盖（仅本地，不进 git）
    kp = os.path.join(HERE, "keys.local.json")
    if os.path.exists(kp):
        try:
            d = load_json(kp)
            for k, v in d.items():
                if v:
                    os.environ[k] = str(v).strip()
        except Exception:
            pass
    return os.environ


def count_words(text):
    return len(re.findall(r"[A-Za-z']+", text))


class LLM:
    """多后端客户端：火山方舟 Ark（OpenAI 兼容）+ Gemini（官方 generateContent）。"""

    def __init__(self, cfg):
        self.base_url = cfg["llm"]["base_url"].rstrip("/")
        self.api_key = os.environ.get(cfg["llm"]["api_key_env"], "")
        self.models = cfg["llm"]["models"]
        self.gemini_key = os.environ.get("GEMINI_API_KEY", "")
        self.gemini_proxy = os.environ.get("GEMINI_PROXY", "")
        self.relay_base = os.environ.get("OPENAI_RELAY_BASE", "")
        self.relay_key = os.environ.get("OPENAI_RELAY_KEY", "")
        self.relay_config = cfg["llm"].get("relay", {})
        # Ark can leave a socket half-open past urllib's nominal timeout. Keep
        # every Ark-backed step bounded so a serial chapter run can fail closed.
        self.ark_request_timeout_seconds = max(
            1, int(cfg["llm"].get("ark_request_timeout_seconds", 90))
        )
        self.run_id = ""
        self.story_id = ""
        self.chapter_no = None
        self._state = None
        self._local = threading.local()
        self._writer_fallback_model = ""

    def _relay_headers(self):
        headers = {"Content-Type": "application/json"}
        if self.relay_key:
            headers["Authorization"] = f"Bearer {self.relay_key}"
        extra_headers = self.relay_config.get("headers", {})
        if not isinstance(extra_headers, dict):
            raise ValueError("llm.relay.headers must be an object")
        for name, value in extra_headers.items():
            if not isinstance(name, str) or not isinstance(value, str):
                raise ValueError("llm.relay.headers must contain string pairs")
            headers[name] = value
        return headers

    def _relay_wire_api(self, role):
        role_apis = self.relay_config.get("role_wire_apis", {})
        if not isinstance(role_apis, dict):
            raise ValueError("llm.relay.role_wire_apis must be an object")
        wire_api = role_apis.get(role, self.relay_config.get("wire_api", "responses"))
        if wire_api not in ("responses", "chat_completions"):
            raise ValueError(f"unsupported relay wire API for {role}: {wire_api}")
        return wire_api

    def set_run_context(self, run_id, story_id, state_store=None, chapter_no=None):
        self.run_id = run_id or ""
        self.story_id = story_id or ""
        self.chapter_no = chapter_no
        self._local.chapter_no = chapter_no
        self._state = state_store
        self._writer_fallback_model = ""

    def set_chapter_context(self, chapter_no):
        self._local.chapter_no = chapter_no

    def set_call_context(self, attempt=None, purpose=None, retry_index=None):
        self._local.attempt = attempt
        self._local.purpose = purpose
        self._local.retry_index = retry_index

    def _chat_ark(self, model, messages, max_tokens, temperature, json_mode=False):
        body = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        req = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        t0 = time.time()
        result = {}

        def worker():
            try:
                with urllib.request.urlopen(
                    req, timeout=self.ark_request_timeout_seconds
                ) as response:
                    result["data"] = json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                result["error"] = (
                    f"HTTP {exc.code}: "
                    f"{exc.read().decode('utf-8', 'ignore')[:400]}"
                )
            except Exception as exc:
                result["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        thread.join(timeout=self.ark_request_timeout_seconds + 5)
        if thread.is_alive():
            return {"error": (
                f"TimeoutError: Ark request exceeded "
                f"{self.ark_request_timeout_seconds + 5}s"
            )}
        if "error" in result:
            return {"error": result["error"]}
        data = result.get("data")
        if not isinstance(data, dict):
            return {"error": "invalid Ark response"}
        msg = data["choices"][0]["message"]
        content = (msg.get("content") or "").strip()
        reasoning = (msg.get("reasoning_content") or "").strip()
        usage = data.get("usage", {})
        return {
            "content": content,
            "reasoning_len": len(reasoning),
            "elapsed": round(time.time() - t0, 1),
            "tokens": usage.get("total_tokens"),
        }

    def _chat_gemini(self, model, messages, max_tokens, temperature, json_mode=False):
        system = ""
        contents = []
        for m in messages:
            if m["role"] == "system":
                system = m["content"]
            else:
                contents.append({
                    "role": "user" if m["role"] == "user" else "model",
                    "parts": [{"text": m["content"]}],
                })
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={self.gemini_key}"
        body = {
            "contents": contents,
            "generationConfig": {"maxOutputTokens": max_tokens, "temperature": temperature},
        }
        if json_mode:
            body["generationConfig"]["responseMimeType"] = "application/json"
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        proxy = urllib.request.ProxyHandler({"https": self.gemini_proxy, "http": self.gemini_proxy}) if self.gemini_proxy else None
        opener = urllib.request.build_opener(proxy) if proxy else urllib.request.build_opener()
        t0 = time.time()
        data = None
        for attempt in range(3):
            try:
                with opener.open(req, timeout=300) as r:
                    data = json.loads(r.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as e:
                return {"error": f"HTTP {e.code}: {e.read().decode('utf-8', 'ignore')[:400]}"}
            except Exception as e:
                if attempt == 2:
                    return {"error": f"{type(e).__name__}: {str(e)[:200]}"}
                time.sleep(3 * (attempt + 1))  # 断连重试
        parts = data["candidates"][0]["content"]["parts"]
        content = "".join(p.get("text", "") for p in parts).strip()
        um = data.get("usageMetadata", {})
        return {
            "content": content,
            "reasoning_len": 0,
            "elapsed": round(time.time() - t0, 1),
            "tokens": um.get("totalTokenCount"),
        }

    def _chat_relay(self, model, messages, max_tokens, temperature, json_mode=False):
        """GPT 中转（OpenAI responses API），system 角色转 developer。
        看门狗：请求放 daemon 线程，join(hard=100s) 超时判死，治 Windows socket 半开假死（timeout 不覆盖 DNS/建连阶段）。"""
        input_msgs = []
        for m in messages:
            role = "developer" if m["role"] == "system" else m["role"]
            input_msgs.append({"role": role, "content": m["content"]})
        body = {
            "model": model,
            "input": input_msgs,
            "max_output_tokens": max_tokens,
        }
        if json_mode:
            body["text"] = {"format": {"type": "json_object"}}
        req = urllib.request.Request(
            self.relay_base + "/v1/responses",
            data=json.dumps(body).encode("utf-8"),
            headers=self._relay_headers(),
        )
        t0 = time.time()
        data = None
        # 路3：5xx/429/timeout 全部纳入重试，指数退避 5/15/45/90s，避免单次波动丢章
        retry_codes = (400, 429, 500, 502, 503, 504)
        retry_sleeps = (5, 15, 45, 90)
        for attempt in range(5):
            result = {}

            def worker():
                try:
                    with urllib.request.urlopen(req, timeout=90) as r:
                        result["data"] = json.loads(r.read().decode("utf-8"))
                except urllib.error.HTTPError as e:
                    result["exc"] = ("http", e.code, e.read().decode("utf-8", "ignore")[:400])
                except Exception as e:
                    result["exc"] = ("exc", type(e).__name__, str(e)[:200])

            th = threading.Thread(target=worker, daemon=True)
            th.start()
            th.join(timeout=100)  # 90s socket + 10s 余量，超过即判假死
            if th.is_alive():
                if attempt == 4:
                    return {"error": "HTTP HANG: request stuck >100s after 5 attempts"}
                time.sleep(retry_sleeps[attempt])
                continue
            if "exc" in result:
                kind, code, msg = result["exc"]
                if kind == "http":
                    if code in (404, 426) and self.relay_config.get("allow_chat_fallback", False):
                        return self._chat_relay_completions(
                            model, messages, max_tokens, temperature, t0, json_mode=json_mode
                        )
                    if code in retry_codes and attempt < 4:
                        time.sleep(retry_sleeps[attempt])
                        continue
                    return {"error": f"HTTP {code}: {msg}"}
                if attempt == 4:
                    return {"error": f"{code}: {msg}"}
                time.sleep(retry_sleeps[attempt])
                continue
            data = result["data"]
            break
        text_out = ""
        for item in data.get("output", []):
            if item.get("type") == "message":
                for c in item.get("content", []):
                    if c.get("type") == "output_text":
                        text_out += c.get("text", "")
        return {
            "content": text_out.strip(),
            "reasoning_len": 0,
            "elapsed": round(time.time() - t0, 1),
            "tokens": None,
        }

    def _chat_relay_completions(self, model, messages, max_tokens, temperature, started=None, json_mode=False,
                                max_attempts=5, request_timeout=300):
        """Compatibility path for relays that require WebSocket on Responses but retain HTTP chat completions."""
        body = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        req = urllib.request.Request(
            self.relay_base + "/v1/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers=self._relay_headers(),
        )
        retry_codes = (429, 500, 502, 503, 504)
        retry_sleeps = (5, 15, 45, 90)
        max_attempts = max(1, min(int(max_attempts), 5))
        request_timeout = max(1, int(request_timeout))
        for attempt in range(max_attempts):
            try:
                with urllib.request.urlopen(req, timeout=request_timeout) as response:
                    data = json.loads(response.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as exc:
                error_body = exc.read().decode("utf-8", "ignore")[:400]
                # This relay intermittently returns a bare gateway page as a
                # 404. Do not retry real API/model 404s such as model_not_found.
                transient_gateway_404 = exc.code == 404 and error_body.strip().lower() == "404 page not found"
                if (exc.code in retry_codes or transient_gateway_404) and attempt < max_attempts - 1:
                    time.sleep(retry_sleeps[attempt])
                    continue
                return {"error": f"HTTP {exc.code}: {error_body}"}
            except Exception as exc:
                if attempt < max_attempts - 1:
                    time.sleep(retry_sleeps[attempt])
                    continue
                return {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
        message = data["choices"][0]["message"]
        usage = data.get("usage", {})
        return {
            "content": (message.get("content") or "").strip(),
            "reasoning_len": len(message.get("reasoning_content") or ""),
            "elapsed": round(time.time() - (started or time.time()), 1),
            "tokens": usage.get("total_tokens"),
        }

    def _call_model(self, role, model, messages, max_tokens, temperature, json_mode=False,
                    relay_max_attempts=5, relay_request_timeout=300):
        backend = get_writer_profile(model)["backend"]
        if backend == "gemini":
            return backend, self._chat_gemini(model, messages, max_tokens, temperature, json_mode=json_mode)
        if backend == "relay":
            if self._relay_wire_api(role) == "chat_completions":
                return backend, self._chat_relay_completions(
                    model, messages, max_tokens, temperature, json_mode=json_mode,
                    max_attempts=relay_max_attempts, request_timeout=relay_request_timeout,
                )
            return backend, self._chat_relay(model, messages, max_tokens, temperature, json_mode=json_mode)
        return backend, self._chat_ark(model, messages, max_tokens, temperature, json_mode=json_mode)

    def _record_call(self, role, model, backend, result, started):
        if not (self._state and self.run_id):
            return
        self._state.record_llm_call(
            self.run_id, self.story_id, role, model, backend,
            "error" if "error" in result else "completed",
            elapsed_s=round(time.time() - started, 1),
            tokens=result.get("tokens"), error=result.get("error"),
            chapter_no=getattr(self._local, "chapter_no", self.chapter_no),
            attempt=getattr(self._local, "attempt", None),
            purpose=getattr(self._local, "purpose", role) or role,
            retry_index=getattr(self._local, "retry_index", None),
        )

    def chat(self, role, messages, max_tokens=2000, temperature=0.85, json_mode=False):
        configured_model = self.models[role]
        started = time.time()
        fallback = self.relay_config.get("writer_fallback", {}) if role == "writer" else {}
        fallback_model = fallback.get("model") if isinstance(fallback, dict) else ""
        model = self._writer_fallback_model if role == "writer" and self._writer_fallback_model else configured_model
        using_primary_writer = model == configured_model
        try:
            retry_attempts = fallback.get("primary_max_attempts", 5) if fallback and using_primary_writer else 5
            primary_timeout = fallback.get("primary_request_timeout_seconds", 300) if fallback and using_primary_writer else 300
            backend, result = self._call_model(
                role, model, messages, max_tokens, temperature, json_mode=json_mode,
                relay_max_attempts=retry_attempts, relay_request_timeout=primary_timeout,
            )
        except Exception as exc:
            result = {"error": f"{type(exc).__name__}: {exc}"}
            backend = get_writer_profile(model)["backend"]
        if "error" not in result and not (result.get("content") or "").strip():
            result = {**result, "error": "model returned empty output", "failure_type": FailureType.EMPTY_OUTPUT.value}
        if "error" in result:
            result.setdefault("failure_type", classify_failure(result.get("error")))
        if "error" in result and using_primary_writer and fallback_model and fallback_model != model:
            self._record_call(role, model, backend, result, started)
            # A relay failure is a run-level condition, not a single-segment
            # condition. Do not spend two failing primary attempts per segment.
            self._writer_fallback_model = fallback_model
            fallback_started = time.time()
            try:
                fallback_backend, fallback_result = self._call_model(
                    role, fallback_model, messages, max_tokens, temperature, json_mode=json_mode,
                )
            except Exception as exc:
                fallback_backend = get_writer_profile(fallback_model)["backend"]
                fallback_result = {"error": f"{type(exc).__name__}: {exc}"}
            if "error" not in fallback_result and not (fallback_result.get("content") or "").strip():
                fallback_result = {**fallback_result, "error": "model returned empty output"}
            if "error" in fallback_result:
                fallback_result.setdefault("failure_type", classify_failure(fallback_result.get("error")))
            result = {**fallback_result, "fallback_from": model}
            model, backend, started = fallback_model, fallback_backend, fallback_started
        self._record_call(role, model, backend, result, started)
        return result


class WritingEngine:
    def __init__(self, config_path=None):
        config_path = config_path or os.path.join(HERE, "config.json")
        self.cfg = load_json(config_path)
        self.llm = LLM(self.cfg)
        self.tells = load_json(os.path.join(HERE, "ai_tells_en.json"))
        self.w = self.cfg["writing"]
        # 字数区间从 default_words 按 word_band 比例自动推导，保证改 default_words 一处即全局联动
        # 例：default_words=2000, word_band=0.1 → word_min=1800 / word_max=2200
        dw = self.w.get("default_words", 700)
        band = self.w.get("word_band", 0.1)
        self.w["word_min"] = round(dw * (1 - band))
        self.w["word_max"] = round(dw * (1 + band))
        self.anchor_examples = []
        self.genre = None
        self.experience_runtime = ExperienceRuntime(HERE, self.w.get("experience_runtime", {}))

    def experience_block(self, chapter_no):
        """Return a small, optional experience block for the current chapter."""
        phase = f"chapter_{chapter_no}"
        baseline = (
            "start in-scene end with a hook no opening exposition dumps "
            "concrete sensory detail show do not tell dialogue physical action"
        )
        return self.experience_runtime.render(self.genre or "general", phase, baseline)

    def _load_anchors(self, genre=None):
        """加载风格样本库。genre 指定子目录；None 时加载全部子目录。"""
        base = os.path.join(HERE, self.cfg["paths"]["anchor_examples"])
        if genre:
            genre_dir = os.path.join(base, genre)
            # A recognized genre without its own vetted samples must not fall
            # back to every unrelated genre (for example, dark romance must
            # not inherit domestic-suspense prose anchors).
            dirs = [genre_dir] if os.path.isdir(genre_dir) else []
        else:
            dirs = []
            if os.path.isdir(base):
                dirs = [os.path.join(base, sub) for sub in sorted(os.listdir(base))
                        if os.path.isdir(os.path.join(base, sub))]
        samples = []
        for d in dirs:
            for fn in sorted(os.listdir(d)):
                if fn.endswith(".md") or fn.endswith(".txt"):
                    txt = open(os.path.join(d, fn), encoding="utf-8").read()
                    samples.append(txt[:3000])  # 每篇截 3000 字符做锚点
        return samples

    def detect_genre(self, direction):
        """从 direction 文本检测题材，返回题材目录名或 None。"""
        dl = direction.lower()
        for genre, keywords in self.cfg.get("genre_keywords", {}).items():
            for kw in keywords:
                if kw in dl:
                    return genre
        return None

    def set_genre(self, direction, genre=None):
        """设置题材并加载对应样本。genre 显式指定优先，否则从 direction 检测。"""
        g = genre or self.detect_genre(direction)
        self.genre = g
        self.anchor_examples = self._load_anchors(g)
        return g

    def _anchor_block(self):
        if not self.anchor_examples:
            return ""
        parts = ["\n\nHere are examples of the target prose style (imitate the voice, not the content):"]
        for i, s in enumerate(self.anchor_examples, 1):
            parts.append(f"\n<example {i}>\n{s}\n</example>")
        return "\n".join(parts)

    def _forbidden_block(self):
        phrases = self.tells.get("phrases", []) + self.tells.get("expressions", [])
        verbs = self.tells.get("verbs", [])
        sensory = self.tells.get("cliche_sensory", [])
        return (
            "\n\nStyle constraints (hard rules):\n"
            f"- Do NOT use these phrases: {', '.join(phrases[:25])}\n"
            f"- Avoid these tell-y verbs: {', '.join(verbs)}\n"
            f"- Avoid AI-template sensory clichés: {', '.join(sensory)}\n"
            "- Concrete sensory detail over abstraction; show, do not tell\n"
            "- No purple prose, no melodrama, no rhetorical questions to the reader\n"
        )

    def write_outline(self, direction):
        """一次性生成大纲。注意：GPT 中转长输出会 504，长场景请用 write_outline_segmented。"""
        if get_writer_profile(self.llm.models.get("outline", ""))["backend"] == "relay":
            return self.write_outline_segmented(direction)
        serial = self.w.get("serial_mode", False)
        sys_msg = (
            "You are a professional English fiction planner. "
            "Design a tight, publishable serialized story outline."
        )
        if serial:
            structure = (
                "3. Serial-arc structure: this outline covers ONLY the first "
                + str(self.w['default_chapters']) + " chapters of an ongoing serial (Volume One opening). "
                "Establish the long-term central mystery/threat, escalate it across the volume, and end the final "
                "chapter on a strong hook or new escalation. Do NOT resolve the central mystery. "
                "The story must be able to continue well beyond this outline."
            )
        else:
            structure = "3. 3-act structure summary"
        user = (
            f"Story direction: {direction}\n\n"
            f"Target: {self.w['default_chapters']} chapters, {self.w['default_words']} words each.\n\n"
            "Output a concise outline with:\n"
            "1. Logline (one sentence)\n"
            "2. Protagonist (name, flaw, want)\n"
            + structure + "\n"
            "4. Chapter-by-chapter beats. For EACH chapter, specify 2-3 scene beats, "
            "including at least one dialogue exchange AND one concrete detail or revelation. "
            "Each chapter's beats must be substantial enough to sustain ~" + str(self.w['default_words']) + " words of prose without padding or repetition. "
            "Give the middle chapters rising complications, not filler. "
            "Avoid clichéd story skeletons (e.g. late-night phone call -> accident -> calm spouse -> immediate clue-hunting). "
            "Give this story a distinctive, unexpected entry point and emotional texture.\n"
            "Keep it under 1200 words, plain prose, no markdown headers."
        )
        profile = get_writer_profile(self.llm.models.get("outline", ""))
        r = self.llm.chat("outline", [
            {"role": "system", "content": sys_msg},
            {"role": "user", "content": user},
        ], max_tokens=profile["max_tokens"], temperature=0.7)
        return r

    def write_outline_segmented(self, direction):
        """Keep each relay outline request below the gateway's long-output limit."""
        serial = self.w.get("serial_mode", False)
        if serial:
            structure = (
                "3. Serial-arc structure: this outline covers ONLY the first "
                + str(self.w['default_chapters']) + " chapters of an ongoing serial (Volume One opening). "
                "Establish the long-term central mystery/threat, escalate it across the volume, and end the final "
                "chapter on a strong hook or new escalation. Do NOT resolve the central mystery. "
                "The story must be able to continue well beyond this outline."
            )
        else:
            structure = "3. 3-act structure summary"
        foundation = self.llm.chat("outline", [
            {"role": "system", "content": "You are a professional English fiction planner. Output plain prose, no markdown."},
            {"role": "user", "content": (
                f"Story direction: {direction}\n\n"
                "Write the story foundation (under 220 words):\n"
                "1. Logline (one sentence)\n2. Protagonist (name, flaw, want)\n"
                + structure + "\n"
                "Output only this part, no commentary."
            )},
        ], max_tokens=600, temperature=0.7)
        if "error" in foundation:
            return foundation
        beats = []
        total = self.w["default_chapters"]
        for start in range(1, total + 1, 4):
            end = min(total, start + 3)
            chunk = self.llm.chat("outline", [
                {"role": "system", "content": "You are a professional English fiction planner. Output plain prose, no markdown."},
                {"role": "user", "content": (
                    f"Story direction: {direction}\n\n"
                    "Story foundation:\n" + foundation["content"] + "\n\n"
                    f"Write chapter-by-chapter beats only for chapters {start}-{end} (under 220 words). "
                    "For EACH chapter: 2-3 scene beats, at least one dialogue exchange, and one concrete detail or revelation. "
                    f"Each chapter's beats must sustain ~{self.w['default_words']} words of prose. "
                    "Preserve escalation across the stated chapter range. Output only these beats."
                )},
            ], max_tokens=600, temperature=0.7)
            if "error" in chunk:
                return chunk
            beats.append(chunk)
        return {
            "content": foundation["content"].strip() + "\n\n" + "\n\n".join(
                chunk["content"].strip() for chunk in beats
            ),
            "elapsed": round(foundation.get("elapsed", 0) + sum(chunk.get("elapsed", 0) for chunk in beats), 1),
        }

    def write_world_bible(self, direction, outline):
        """Build a compact bible in bounded relay-sized calls."""
        sys_msg = "You are a worldbuilding designer for serialized fiction. Output plain prose, no markdown."
        core = self.llm.chat("outline", [
            {"role": "system", "content": sys_msg},
            {"role": "user", "content": (
                f"Story direction: {direction}\n\nOutline:\n{outline}\n\n"
                "Create world-bible part 1 (under 220 words): the premise-specific world rules and the protagonist plus key cast. "
                "For every character give name, flaw, want, and role. Preserve only facts grounded in the outline."
            )},
        ], max_tokens=600, temperature=0.7)
        if "error" in core:
            return core
        support = self.llm.chat("outline", [
            {"role": "system", "content": sys_msg},
            {"role": "user", "content": (
                f"Story direction: {direction}\n\nOutline:\n{outline}\n\n"
                "World-bible part 1:\n" + core["content"] + "\n\n"
                "Create world-bible part 2 (under 180 words): recurring locations, objects, relationship boundaries, and continuity rules. "
                "Do not invent a new plotline. Output only this part."
            )},
        ], max_tokens=600, temperature=0.7)
        if "error" in support:
            return support
        return {
            "content": core["content"].strip() + "\n\n" + support["content"].strip(),
            "elapsed": round(core.get("elapsed", 0) + support.get("elapsed", 0), 1),
        }

    def compile_chapter_contracts(self, direction, outline, world_bible, story_id, chapter_numbers):
        """Compile only hard chapter obligations; prose taste stays outside the contract."""
        source_sha256 = chapter_contract_planning_hash(direction, outline, world_bible)
        schema = {
            "schema_version": "chapter_acceptance_contract_v1",
            "story_id": story_id,
            "source_sha256": source_sha256,
            "chapters": {
                "0001": {
                    "chapter": 1,
                    "requirements": [{
                        "id": "ch0001-contract-01",
                        "type": "event|fact|dialogue|context|recognition",
                        "requirement": "one atomic, literal chapter obligation",
                        "source_quote": "exact contiguous wording copied from the outline",
                    }],
                },
            },
        }
        response = self.llm.chat("outline", [
            {"role": "system", "content": (
                "You compile fiction planning into a strict production contract. "
                "Return JSON only. Do not invent facts or add prose advice."
            )},
            {"role": "user", "content": (
                "Build one frozen atomic contract for each requested chapter. "
                "Use 2-6 requirements per chapter. Include only plot outcomes that must be explicit in the prose: "
                "events, concrete facts, required dialogue, time/place context, or a character's explicit recognition. "
                "Each requirement must test one thing; split compound beats. Exclude mood, quality, style, pacing, "
                "and optional description. IDs must be sequential exactly as shown in the schema.\n\n"
                "Every source_quote must be an exact contiguous quote copied from the supplied outline that supports the item.\n\n"
                "Requested chapters: " + json.dumps(list(chapter_numbers)) + "\n\n"
                "Story direction:\n" + direction + "\n\n"
                "Outline:\n" + outline + "\n\n"
                "World bible:\n" + world_bible + "\n\n"
                "Return exactly this JSON shape, using the real requested chapter keys and no extra keys:\n"
                + json.dumps(schema, ensure_ascii=False)
            )},
        ], max_tokens=2400, temperature=0.1, json_mode=True)
        if "error" in response:
            return response
        try:
            document = parse_compiler_response(
                response.get("content"), story_id, chapter_numbers, source_sha256,
                outline,
            )
        except ChapterContractError as exc:
            return {"error": str(exc)}
        return {"content": document, "source_sha256": source_sha256}

    def _minimal_user(self, direction, outline, world_bible, chapter_n, total, target_words, word_feedback, extra_rules=""):
        """精简包：thinking 强模型用。只给方向+大纲+字数，加两条极简关键约束（禁 on-the-nose 隐喻 + 禁背景堆砌）。
        交接卡/lessons 等额外规则拼接在末尾，保证衔接信息不丢。"""
        bible = f"\n\nWorld bible (settings that MUST stay consistent):\n{world_bible}" if world_bible else ""
        fb = f"\n\nWord count correction: {word_feedback}" if word_feedback else ""
        extra = f"\n\n{extra_rules}\n" if extra_rules else ""
        return (
            f"Story direction: {direction}\n\n"
            f"Outline:\n{outline}\n"
            + bible
            + f"\n\nWrite Chapter {chapter_n} of {total}, following the outline's beats. "
            f"Write the FULL chapter of {target_words} words, do NOT stop early. "
            f"Third person, past tense, start in-scene, end with a hook.\n"
            "Two hard rules:\n"
            "- No on-the-nose metaphors that map a character's profession directly onto their emotions "
            "(e.g. an accountant 'balancing the ledger of her life'). Keep imagery concrete and unexpected.\n"
            "- No opening exposition dumps. Reveal character and backstory through action and detail, not explanation.\n"
            "- Use 'as if' sparingly (at most twice in this chapter); prefer concrete verbs, images, or physical detail.\n"
            "- Do not repeat a revelation, action, object state, or atmospheric reaction unless the second instance changes it.\n"
            + extra
            + fb
            + "\n\nOutput only the chapter text, no headers, no commentary."
        )

    def _full_user(self, direction, outline, world_bible, chapter_n, total, target_words, word_feedback, extra_rules):
        """完整包：轻量/普通模型用（约束辅助完成）。"""
        anchor = self._anchor_block()
        forbidden = self._forbidden_block()
        fb = f"\n\nWord count correction: {word_feedback}" if word_feedback else ""
        bible = f"\n\nWorld bible (settings that MUST stay consistent):\n{world_bible}" if world_bible else ""
        extra = f"\n\nAdditional writing rules (learned from previous blind reviews, MUST follow):\n{extra_rules}" if extra_rules else ""
        return (
            f"Story direction: {direction}\n\n"
            f"Outline:\n{outline}\n"
            + bible
            + f"\n\nWrite Chapter {chapter_n} of {total}. Follow the outline's beats for this chapter.\n"
            f"Develop EVERY scene beat fully: write out the dialogue exchange, ground the detail/revelation with setup and payoff. "
            f"Do not compress beats or skip them.\n"
            f"Emotional verisimilitude: when a character faces devastating news, betrayal, or shock, "
            f"write the emotional response FIRST (physical reaction, hesitation, denial, internal struggle) "
            f"before any rational or investigative action. Characters are NOT instantly calm and analytical.\n"
            f"Word count: write the FULL chapter of {target_words} words. Do NOT stop early, do NOT write a fragment. "
            f"Finish the complete chapter with its beats, dialogue, and ending.\n"
            f"Third person, past tense. Start in-scene. End with a hook.\n"
            "Avoid repeated 'as if' comparisons; use no more than two in the full chapter.\n"
            "Do not repeat a revelation, action, object state, or atmospheric reaction unless the second instance changes it.\n"
            + anchor
            + forbidden
            + extra
            + fb
            + "\n\nOutput only the chapter text, no headers, no commentary."
        )

    def _write_chapter_segmented(self, direction, outline, world_bible, chapter_n, total, target_words, word_feedback="", extra_rules=""):
        """GPT 中转对长输出易超时，分段写并把字数反馈带入重写请求。"""
        import time as _t
        # Keep requests below the relay timeout while avoiding the large
        # request count and fixed wait cost of 300-word segments.
        seg_words = int(self.w.get("segment_words", 500))
        seg_words = max(150, min(seg_words, 900))
        segment_cooldown = max(0, float(self.w.get("segment_cooldown", 1)))
        segment_recovery_attempts = max(0, min(int(self.w.get("segment_recovery_attempts", 0)), 2))
        segment_recovery_wait = max(0, float(self.w.get("segment_recovery_wait_seconds", 20)))
        n_segs = max(1, (target_words + seg_words - 1) // seg_words)
        bible = f"\n\nWorld bible (settings that MUST stay consistent):\n{world_bible}" if world_bible else ""
        base = (
            f"Story direction: {direction}\n\nOutline:\n{outline}\n" + bible +
            f"\n\nWrite Chapter {chapter_n} of {total}, following the outline's beats. Third person, past tense.\n"
            "Two hard rules: no on-the-nose metaphors mapping a profession onto emotions; no opening exposition dumps.\n"
            "Avoid repeated 'as if' comparisons; use no more than two in the full chapter.\n"
            "Do not repeat a revelation, action, object state, or atmospheric reaction unless the second instance changes it.\n"
            + (f"\n{extra_rules}\n" if extra_rules else "")
            + (f"\nWord count correction: {word_feedback}\n" if word_feedback else "")
        )
        full = ""
        r = {}
        for i in range(n_segs):
            remaining_words = max(1, target_words - count_words(full))
            current_seg_words = min(seg_words, remaining_words)
            if i == 0:
                user = base + f"Write the opening ~{current_seg_words} words of this chapter. Output only prose."
            else:
                user = base + f"Continue the chapter seamlessly, writing the next ~{current_seg_words} words. Output only prose.\n\nWritten so far:\n{full[-1500:]}"
            for segment_attempt in range(segment_recovery_attempts + 1):
                r = self.llm.chat("writer", [
                    {"role": "system", "content": "You are a literary fiction writer. Write vivid, natural English prose."},
                    {"role": "user", "content": user},
                ], max_tokens=2000, temperature=self.w["temperature"])
                if "error" not in r:
                    break
                if (
                    classify_failure(r.get("error")) != FailureType.NETWORK.value
                    or segment_attempt >= segment_recovery_attempts
                ):
                    return r
                if segment_recovery_wait:
                    _t.sleep(segment_recovery_wait)
            seg = r["content"].strip()
            full = (full + "\n\n" + seg).strip()
            if count_words(full) >= target_words:
                break
            if i < n_segs - 1 and segment_cooldown:
                _t.sleep(segment_cooldown)
        return {**r, "content": full}

    def write_chapter(self, direction, outline, world_bible, chapter_n, total, target_words, word_feedback="", extra_rules=""):
        sys_msg = (
            "You are a literary fiction writer. Write vivid, natural, contemporary English prose. "
            "You are writing one chapter of a serialized short story."
        )
        profile = get_writer_profile(self.llm.models.get("writer", ""))
        if profile["backend"] == "relay":
            return self._write_chapter_segmented(direction, outline, world_bible, chapter_n, total, target_words, word_feedback, extra_rules)
        if profile["prompt_pack"] == "minimal":
            user = self._minimal_user(direction, outline, world_bible, chapter_n, total, target_words, word_feedback, extra_rules)
        else:
            user = self._full_user(direction, outline, world_bible, chapter_n, total, target_words, word_feedback, extra_rules)
        r = self.llm.chat("writer", [
            {"role": "system", "content": sys_msg},
            {"role": "user", "content": user},
        ], max_tokens=profile["max_tokens"], temperature=self.w["temperature"])
        return r

    def ai_tell_check(self, text):
        """写后 AI 味密度校验，返回 (issues, density_report)。"""
        words = max(count_words(text), 1)
        k = words / 1000.0
        issues = []
        all_terms = (
            self.tells.get("phrases", [])
            + self.tells.get("verbs", [])
            + self.tells.get("expressions", [])
        )
        for term in all_terms:
            n = len(re.findall(re.escape(term), text, re.I))
            if n > 0 and n / k > 1.5:
                issues.append(f"{term} x{n}")
        em_n = text.count("\u2014") + text.count("--")
        em_rate = em_n / k
        if em_rate > self.tells.get("em_dash_per_1000", 8):
            issues.append(f"em-dash {em_rate:.1f}/k")
        return issues, {"words": words, "em_rate": round(em_rate, 1)}

    def repair_strict_prose(self, text):
        """Repair only unique sentences matched by deterministic strict rules.

        This is a bounded fallback after a candidate exists, not a new review
        loop: every replacement is tied to one exact source sentence and the
        complete candidate must clear the same local rules afterward.
        """
        anchors = quality_checks.strict_repair_anchors(text)
        if not anchors:
            return {"repaired": False, "reason": "no unique strict-rule anchors"}
        candidate = text
        audit = []
        for anchor in anchors:
            source = anchor["excerpt"]
            result = self.llm.chat("writer", [
                {"role": "system", "content": (
                    "You are a precision fiction line editor. Return JSON only. "
                    "Replace exactly one supplied sentence; preserve facts, plot, "
                    "character agency, tense, and all surrounding prose."
                )},
                {"role": "user", "content": json.dumps({
                    "task": "Return exactly {find, replace}.",
                    "strict_rule": anchor["rule_id"],
                    "find": source,
                    "constraints": [
                        "find must exactly equal the supplied sentence",
                        "replace one grammatical English sentence only",
                        "remove the strict-rule construction",
                        "do not add facts, actions, dialogue, or named entities",
                    ],
                }, ensure_ascii=False)},
            ], max_tokens=500, temperature=0.1, json_mode=True)
            raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", result.get("content", "").strip(), flags=re.I)
            try:
                draft = json.loads(raw)
            except (TypeError, ValueError, json.JSONDecodeError):
                return {"repaired": False, "reason": f"invalid local repair JSON for {anchor['rule_id']}"}
            replacement = draft.get("replace", "") if isinstance(draft, dict) else ""
            if not isinstance(draft, dict) or draft.get("find") != source or candidate.count(source) != 1:
                return {"repaired": False, "reason": f"unverified local repair for {anchor['rule_id']}"}
            errors = local_validate(source, replacement)
            if errors:
                return {"repaired": False, "reason": "; ".join(errors)}
            candidate = candidate.replace(source, replacement, 1)
            audit.append(anchor["rule_id"])
        remaining = quality_checks.strict_prose_violations(candidate)
        if remaining:
            return {"repaired": False, "reason": "; ".join(remaining)}
        return {"repaired": True, "text": candidate, "rules": audit}

    def _word_feedback(self, text, target, lo, hi):
        """具体化的字数反馈：告诉模型差多少、怎么补/删。"""
        wc = count_words(text)
        if wc < lo:
            return (f"You wrote {wc} words, about {target - wc} short of the {target}-word target. "
                    f"Expand: add concrete sensory detail, one more exchange of dialogue, or a beat of interior thought. "
                    f"Do not pad with filler or repeat yourself.")
        if wc > hi:
            return (f"You wrote {wc} words, about {wc - target} over the {target}-word target. "
                    f"Trim redundancy, tighten sentences, cut filler words and repeated beats.")
        return ""

    def write_with_retry(self, direction, outline, world_bible, chapter_n, total, target_words, extra_rules=""):
        """写一章，字数硬校验，不符则带具体反馈重写（最多 max_rewrite 次）。"""
        lo, hi = self.w["word_min"], self.w["word_max"]
        text, r = "", {}
        for attempt in range(1 + self.w["max_rewrite"]):
            self.llm.set_call_context(attempt=attempt + 1, purpose="chapter_generation", retry_index=attempt)
            fb = "" if attempt == 0 else self._word_feedback(text, target_words, lo, hi)
            r = self.write_chapter(direction, outline, world_bible, chapter_n, total, target_words, fb, extra_rules)
            if "error" in r:
                r.setdefault("failure_type", classify_failure(r.get("error")))
                return r
            text = r["content"]
            wc = count_words(text)
            if lo <= wc <= hi:
                return {**r, "text": text, "words": wc, "rewrites": attempt}
        # 重试耗尽：返回最后一次
        wc = count_words(text)
        return {**r, "text": text, "words": wc, "rewrites": self.w["max_rewrite"],
                "warn": "word target missed", "failure_type": classify_failure(
                    content=text, word_count=wc, word_min=lo, word_max=hi)}

    def generate_cn_summary(self, text, outline="", direction=""):
        """生成完整中文故事摘要，不能只概括第一章。"""
        # Full prose is too large and front-loading it biases the model toward chapter 1.
        # Use the outline as the plot authority, plus bounded chapter openings/endings.
        chapters = re.split(r"(?=\n?Chapter\s+\d+\b)", outline or "", flags=re.I)
        chapter_digest = []
        for i, part in enumerate(chapters):
            if re.search(r"Chapter\s+\d+", part, re.I):
                chapter_digest.append(part[:700])
        if not chapter_digest:
            paras = [p.strip() for p in text.split("\n\n") if p.strip()]
            chapter_digest = [p[:350] for p in paras[:12]]
        evidence = "\n\n".join(chapter_digest[:12])
        prompt = (
            "请为下面这部英文连载小说写一份给中文读者看的完整剧情摘要。\n"
            "要求：只输出中文正文，不要标题、不要分析、不要逐句翻译。字数 350-550 字。\n"
            "必须覆盖：1）主角和初始处境；2）第一章触发事件；3）感情/关系主线；"
            "4）中段至少两次升级、反转或代价；5）主要反派/阴谋；6）第12章的阶段性结局；"
            "7）结尾留下的后续钩子。不要只写第一章，不要罗列章节，不要使用‘某某等人’糊弄。"
            "专有名词可保留英文或使用前后一致的中文译名；要让不了解英文的读者能判断这部书讲什么。\n\n"
            f"故事方向：{direction[:1200]}\n\n完整大纲（最高优先级）：\n{outline[:12000]}\n\n"
            f"逐章证据摘录：\n{evidence[:9000]}"
        )
        messages = [
            {"role": "system", "content": "你是资深中文小说编辑，擅长把英文连载剧情讲清楚，严格遵守摘要范围和结构要求。"},
            {"role": "user", "content": prompt},
        ]
        r = self.llm.chat("cn_summary", messages, max_tokens=1200, temperature=0.35)
        if "error" not in r and r.get("content"):
            content = r["content"].strip()
            cn_count = len(re.findall(r"[\u4e00-\u9fff]", content))
            weak = cn_count < 220 or not any(x in content for x in ("最终", "结局", "第12章", "血月", "真相", "选择"))
            if weak:
                retry = self.llm.chat("cn_summary", messages + [{"role": "user", "content": "上一版不合格：它没有完整覆盖中后段和结局。请重写，必须写到第12章结局和最后钩子，仍只输出350-550字中文摘要。"}], max_tokens=1200, temperature=0.25)
                if "error" not in retry and retry.get("content"):
                    r = retry
        return r

    def generate_handoff(self, text):
        """生成章节交接卡：本章结束时的人物位置/情绪/钩子/时间线/关键信息。
        供下一章写作时作为衔接锚点注入，解决块状并行下的接缝问题。
        用 cheap 模型（handoff 角色）快速总结，不阻塞主写章。"""
        r = self.llm.chat("handoff", [
            {"role": "system", "content": (
                "You are a continuity editor for serialized fiction. "
                "Read a chapter and produce a compact handoff card so the NEXT chapter can start seamlessly."
            )},
            {"role": "user", "content": (
                "Read this chapter and output a handoff card with these fields, one line each, plain English, no markdown:\n"
                "1. Ending location: where the chapter ends\n"
                "2. Character state: protagonist's physical/emotional state at chapter end\n"
                "3. Key info revealed: what important information was revealed this chapter\n"
                "4. Open hook: the unresolved tension/question carried into the next chapter\n"
                "5. Time: time of day / time passed since story start\n"
                "Keep under 120 words total.\n\n"
                f"Chapter text:\n---\n{text}\n---"
            )},
        ], max_tokens=800, temperature=0.3)
        return r

    def generate_state_delta(self, text, chapter_no, contract, scenes=()):
        """Use the structured reviewer route for an actual-state JSON record."""
        required = {
            "chapter": chapter_no,
            "source_sha256": sha256_text(text),
            "end_assertions": contract.get("end_assertions", []),
            "must_not_regress": contract.get("must_not_regress", []),
            "scenes": [{
                "scene_id": scene["scene_id"],
                "completion_assertion": scene["completion_assertion"],
                "fact_slots": scene["fact_slots"],
            } for scene in scenes],
        }
        # Controlled state is the inter-chapter authority. Use its dedicated
        # structured-extraction route, separate from literary acceptance and
        # long-form outline generation.
        return self.llm.chat("state_delta", [
            {"role": "system", "content": (
                "You are a continuity editor. Report only what the supplied chapter explicitly establishes. "
                "Return strict JSON only; do not repair, infer, or make prose recommendations."
            )},
            {"role": "user", "content": (
                "Return exactly these fields: chapter, source_sha256, assertion_statuses, non_regression_statuses, scene_statuses, fact_statuses, "
                "new_information, relationship_changes, open_hooks, conflicts_with_contract. assertion_statuses is "
                "a list of assertion_id, status (met|violated|unclear), and an exact chapter quotation as evidence. "
                "non_regression_statuses is a list of assertion_id, status (preserved|violated|unclear), and an exact "
                "chapter quotation as evidence. Evidence must be a literal contiguous substring copied from the chapter: do not add "
                "quotation marks, alter punctuation, normalize apostrophes, or paraphrase narrative prose. Include one status for every supplied assertion ID. Copy the chapter and source hash "
                "exactly from the control input. scene_statuses must contain every supplied scene_id using exactly {scene_id,status,occurrence_count,evidence}; status is completed|repeated|missing|unclear. Count every substantive replay of that scene in the chapter: only completed with occurrence_count 1 passes. fact_statuses must contain every supplied fact slot using exactly {fact_id,status,value,evidence}; never use fact_slot_id or any alternate key. Status is consistent|contradicted|unclear. Copy the supplied canonical value exactly when consistent; report contradicted if the chapter gives any competing value. Set new_information, relationship_changes, open_hooks, and conflicts_with_contract to [] exactly. Do not summarize the chapter in those fields. "
                "\n\nControl input:\n"
                + json.dumps(required, ensure_ascii=False)
                + "\n\nChapter text:\n---\n" + text + "\n---"
            )},
        # GLM returns reliable structured text through ordinary chat, whereas
        # its Ark response_format mode can stall or return no content. The
        # parser below still validates the complete JSON schema and evidence.
        ], max_tokens=800, temperature=0.1, json_mode=False)

    def repair_chapter_with_verified_patches(self, text, acceptance):
        """Repair acceptance findings without letting a model rewrite other prose."""
        repairable_outcomes = [
            outcome for outcome in acceptance.get("outcomes", [])
            if outcome.get("status") in {"missing", "unclear"}
        ]
        insertion_anchors = [
            outcome.get("insertion_anchor") for outcome in repairable_outcomes
            if isinstance(outcome.get("insertion_anchor"), str) and outcome["insertion_anchor"]
        ]
        if len(insertion_anchors) != len(repairable_outcomes):
            return {"error": "acceptance repair has no verified insertion anchor for incomplete outcome(s): " + ", ".join(
                outcome.get("id", "unknown") for outcome in repairable_outcomes
                if not outcome.get("insertion_anchor")
            )}
        findings = acceptance.get("findings", [])
        anchors = [
            quote for finding in findings
            for quote in finding.get("evidence_quotes", [])
            if isinstance(quote, str) and quote
        ]
        if not anchors and not insertion_anchors:
            return {"error": "acceptance repair has no verified finding anchors"}
        repair_outcomes = [{
            "id": outcome.get("id"), "requirement": outcome.get("requirement"),
            "status": outcome.get("status"), "reason": outcome.get("reason"),
            "evidence_quotes": outcome.get("evidence_quotes"), "insertion_anchor": outcome.get("insertion_anchor"),
        } for outcome in repairable_outcomes]
        repair_request = [{
            "category": finding.get("category"),
            "repair_scope": finding.get("repair_scope"),
            "reason": finding.get("reason"),
            "evidence_quotes": finding.get("evidence_quotes"),
        } for finding in findings]
        response = self.llm.chat("writer", [
            {"role": "system", "content": (
                "You are a fiction copy editor. Return JSON only with exactly one key: patches. "
                "patches is a non-empty array. Use {find,replace} only when find copies one supplied "
                "finding anchor exactly, character for character. Use {insert_after,insert} only when "
                "insert_after copies one supplied insertion anchor exactly; insert must be one short paragraph "
                "of at most 120 English words. Do not include surrounding text, new commentary, or a full chapter. "
                "Repair only the cited defect."
            )},
            {"role": "user", "content": (
                "Incomplete required outcomes with verified insertion anchors:\n" + json.dumps(repair_outcomes, ensure_ascii=False) +
                "\n\nVerified acceptance findings:\n" + json.dumps(repair_request, ensure_ascii=False) +
                "\n\nAllowed exact anchors:\n" + json.dumps(anchors, ensure_ascii=False) +
                "\n\nAllowed exact insertion anchors:\n" + json.dumps(insertion_anchors, ensure_ascii=False) +
                "\n\nChapter text:\n---\n" + text + "\n---"
            )},
        ], max_tokens=1800, temperature=0.25, json_mode=True)
        if "error" in response:
            return response
        try:
            raw = re.sub(
                r"^```(?:json)?\s*|\s*```$", "", (response.get("content") or "").strip(), flags=re.I,
            )
            payload = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            return {"error": f"acceptance patch response is not valid JSON: {exc}"}
        if not isinstance(payload, dict) or set(payload) != {"patches"}:
            return {"error": "acceptance patch response must contain only patches"}
        applied = apply_scoped_patches(text, payload["patches"], anchors, insertion_anchors)
        if not applied["accepted"]:
            return {"error": "acceptance repair scope rejected: " + "; ".join(applied["errors"]), "audit": applied["audit"]}
        return {"content": applied["text"], "audit": applied["audit"]}


def main():
    args = build_argument_parser().parse_args()

    if args.assemble_only:
        story_dir = os.path.join(HERE, "shorts", args.story_id)
        chapter_count = args.chapters or 12
        try:
            full_path = assemble_full_book(story_dir, chapter_count)
        except (OSError, ValueError) as exc:
            print(f"ASSEMBLE ERROR: {exc}")
            return 2
        print(f"full book -> {full_path}")
        return 0

    option_error = option_compatibility_error(args)
    if option_error:
        print("OPTION ERROR:", option_error)
        return 2

    # 解析方向：--direction-file 优先，否则 --direction
    if args.direction_file:
        direction = open(args.direction_file, encoding="utf-8").read().strip()
        print(f"[direction] loaded from {args.direction_file}")
    elif args.direction:
        direction = args.direction
    elif args.append_chapters:
        direction = ""
    else:
        print("需要 --direction 或 --direction-file")
        sys.exit(1)

    load_env()
    engine = WritingEngine()
    if args.writer_model:
        engine.llm.models["writer"] = args.writer_model
        print(f"[model override] writer={args.writer_model}")
    if args.outline_model:
        engine.llm.models["outline"] = args.outline_model
        print(f"[model override] outline={args.outline_model}")
    if args.no_experiences:
        engine.experience_runtime.enabled = False
    # 章节数/字数默认从 config 读（唯一来源），命令行显式传参时才覆盖
    if args.chapters is None:
        args.chapters = int(engine.w.get("default_chapters", 12))
    if args.words is None:
        args.words = int(engine.w.get("default_words", 700))
    # Explicit CLI word targets must also drive the validation band.
    if args.words != engine.w.get("default_words"):
        band = float(engine.w.get("word_band", 0.1))
        engine.w["word_min"] = round(args.words * (1 - band))
        engine.w["word_max"] = round(args.words * (1 + band))
    genre = engine.set_genre(direction, args.genre)
    print(f"[genre] {genre or 'general'} ({len(engine.anchor_examples)} anchor sample(s))")
    out_dir = os.path.join(HERE, engine.cfg["paths"]["output"], args.story_id)
    os.makedirs(out_dir, exist_ok=True)
    book_memory = BookMemory(out_dir)
    existing_numbers = sorted(
        int(m.group(1)) for name in os.listdir(out_dir)
        if (m := re.fullmatch(r"ch(\d+)\.md", name))
    )
    if args.append_chapters:
        if args.append_chapters < 1:
            print("--append-chapters 必须大于 0")
            sys.exit(1)
        if not existing_numbers:
            print(f"无法追加：故事目录没有已完成章节: {out_dir}")
            sys.exit(1)
        # Append mode deliberately starts after the current highest chapter and
        # reuses the existing outline/bible; it never rewrites the trial batch.
        if not direction:
            outline_source = os.path.join(out_dir, "outline.md")
            if os.path.exists(outline_source):
                direction = open(outline_source, encoding="utf-8").read()[:5000]
            else:
                print("追加模式缺少 direction 和 outline.md")
                sys.exit(1)
        args.chapters = max(existing_numbers) + args.append_chapters
        args.chapter = 0
        print(f"[append] existing through chapter {max(existing_numbers)}; writing through chapter {args.chapters}")
    if args.resume and not args.run_id:
        print("--resume 需要同时提供 --run-id")
        sys.exit(1)
    run_id = args.run_id or f"{args.story_id}_{uuid.uuid4().hex[:8]}"
    task_store = StateStore()
    chapter_store = ChapterTaskStore(task_store, run_id, args.story_id)
    run_started = time.time()
    task_store.start_run(run_id, args.story_id, "writing")
    engine.llm.set_run_context(run_id, args.story_id, task_store)
    completed_before = {
        int(row["chapter_no"]): row for row in task_store.chapter_tasks(run_id)
        if row["status"] == ChapterStatus.COMPLETED.value and row.get("output_path") and os.path.exists(row["output_path"])
    } if args.resume else {}
    control_dir = args.control_dir or os.path.join(out_dir, "control")

    # A control dry run must never fall through to outline/world-bible generation.
    if args.control_dry_run:
        if not args.controlled_generation:
            print("--control-dry-run requires --controlled-generation")
            task_store.finish_run(run_id, args.story_id, "writing", "failed", round(time.time() - run_started, 1), "missing --controlled-generation")
            return 2
        outline_path = os.path.join(out_dir, "outline.md")
        bible_path = os.path.join(out_dir, "world_bible.md")
        if not os.path.exists(outline_path) or not os.path.exists(bible_path):
            print("CONTROL ERROR: --control-dry-run requires existing outline.md and world_bible.md")
            task_store.finish_run(run_id, args.story_id, "writing", "failed", round(time.time() - run_started, 1), "missing planning artifacts")
            return 2
        outline_text = open(outline_path, encoding="utf-8").read()
        bible_text = open(bible_path, encoding="utf-8").read()
        try:
            control_bundle = load_control_bundle(
                control_dir,
                args.story_id,
                source_hash(direction, outline_text, bible_text),
                allow_missing=False,
            )
        except ControlContractError as exc:
            print(f"CONTROL ERROR: {exc}")
            task_store.finish_run(run_id, args.story_id, "writing", "failed", round(time.time() - run_started, 1), str(exc))
            return 2
        chapter_range = [args.chapter] if args.chapter > 0 else list(range(1, args.chapters + 1))
        for n in chapter_range:
            previous = load_state_delta(control_dir, n - 1) if n > 1 else None
            try:
                result = build_control_context(control_bundle, n, previous)
            except ControlContractError as exc:
                print(f"CONTROL ERROR ch{n}: {exc}")
                task_store.finish_run(run_id, args.story_id, "writing", "failed", round(time.time() - run_started, 1), str(exc))
                return 2
            print(json.dumps(result["diagnostics"], ensure_ascii=False))
        task_store.finish_run(run_id, args.story_id, "writing", "completed", round(time.time() - run_started, 1), None)
        print("[control] dry run passed")
        return 0

    # 1. 大纲
    outline_path = os.path.join(out_dir, "outline.md")
    if should_reuse_planning_artifact(args, outline_path):
        outline_text = open(outline_path, encoding="utf-8").read()
        print(f"[1/4] reusing outline ({len(outline_text)} chars)")
    else:
        print(f"[1/4] generating outline for: {direction[:60]}...")
        outline = engine.write_outline(direction)
        if "error" in outline:
            print("OUTLINE ERROR:", outline["error"])
            sys.exit(1)
        outline_text = outline["content"]
        open(outline_path, "w", encoding="utf-8").write(outline_text)
        print(f"    outline done ({outline['elapsed']}s, {len(outline_text)} chars)")

    if args.outline_only:
        print("\n" + outline_text)
        return 0

    # 2. 设定圣经（world bible）
    bible_path = os.path.join(out_dir, "world_bible.md")
    if should_reuse_planning_artifact(args, bible_path):
        bible_text = open(bible_path, encoding="utf-8").read()
        print(f"[2/4] reusing world bible ({len(bible_text)} chars)")
    else:
        print("[2/4] generating world bible ...")
        bible = engine.write_world_bible(direction, outline_text)
        bible_text = bible["content"] if "error" not in bible else ""
        if bible_text:
            open(bible_path, "w", encoding="utf-8").write(bible_text)
            print(f"    world bible done ({bible['elapsed']}s, {len(bible_text)} chars)")
        else:
            print(f"    world bible FAILED: {bible.get('error')}")

    control_bundle = None
    control_runtime = None
    if args.controlled_generation:
        try:
            control_bundle = load_control_bundle(
                control_dir,
                args.story_id,
                source_hash(direction, outline_text, bible_text),
                allow_missing=False,
            )
        except ControlContractError as exc:
            print(f"CONTROL ERROR: {exc}")
            task_store.finish_run(run_id, args.story_id, "writing", "failed", round(time.time() - run_started, 1), str(exc))
            return 2
        control_runtime = ControlledGenerationRuntime(
            # Control findings and repair routing must point at the same
            # story directory where this engine writes chapter files.
            args.story_id, control_dir, out_dir, control_bundle
        )
        if args.control_repair_card:
            try:
                repair_path = control_runtime.write_repair_request(args.control_repair_card)
            except ControlContractError as exc:
                print(f"CONTROL REPAIR ERROR: {exc}")
                task_store.finish_run(run_id, args.story_id, "writing", "failed", round(time.time() - run_started, 1), str(exc))
                return 2
            task_store.finish_run(run_id, args.story_id, "writing", "completed", round(time.time() - run_started, 1), None)
            print(f"[control] repair request written: {repair_path}")
            return 0

    # 3. 写章
    n_chapters = args.chapters
    if args.append_chapters:
        chapter_range = list(range(max(existing_numbers) + 1, n_chapters + 1))
    elif args.chapter > 0:
        chapter_range = [args.chapter]
    else:
        chapter_range = list(range(1, n_chapters + 1))
    if completed_before:
        chapter_range = [n for n in chapter_range if n not in completed_before]
        print(f"[resume] skipping completed chapters: {sorted(completed_before)}; remaining: {chapter_range}")
    if args.resume and not chapter_range:
        task_store.finish_run(run_id, args.story_id, "writing", "completed", 0, None)
        print("[resume] all requested chapters are already completed; nothing to do")
        return 0

    chapter_contract_document = None
    if args.chapter_contracts:
        if args.append_chapters:
            print("CHAPTER CONTRACT ERROR: --chapter-contracts does not yet support --append-chapters")
            task_store.finish_run(run_id, args.story_id, "writing", "failed", round(time.time() - run_started, 1), "append contracts unsupported")
            return 2
        print(f"[contract] compiling frozen requirements for {len(chapter_range)} chapter(s) ...")
        compiled = engine.compile_chapter_contracts(
            direction, outline_text, bible_text, args.story_id, chapter_range,
        )
        if "error" in compiled:
            print("CHAPTER CONTRACT ERROR:", compiled["error"])
            task_store.finish_run(run_id, args.story_id, "writing", "failed", round(time.time() - run_started, 1), compiled["error"])
            return 2
        chapter_contract_document = compiled["content"]
        with open(os.path.join(out_dir, ARTIFACT_NAME), "w", encoding="utf-8") as handle:
            json.dump(chapter_contract_document, handle, ensure_ascii=False, indent=2)
        item_count = sum(len(requirements_for_chapter(chapter_contract_document, n)) for n in chapter_range)
        print(f"    contract done ({item_count} atomic requirements)")

    for chapter_no in chapter_range:
        chapter_store.update(chapter_no, ChapterStatus.PENDING.value)

    # 质量门槛：可选盲评重写（确保每章 naturalness 达标）
    judge = None
    chapter_acceptance_enabled = False
    chapter_acceptance_rounds = 0
    chapter_acceptance_reviewers = []
    chapter_requirements = {}
    if args.quality_gate:
        from native_check import (
            QualityJudge, build_acceptance_reviewers, freeze_acceptance_issue_ledger,
            extract_chapter_requirements, review_chapter_acceptance,
        )
        judge = QualityJudge(os.environ)
        try:
            acceptance_cfg = load_json(os.path.join(HERE, "orchestrator.json")).get("chapter_acceptance", {})
        except (OSError, ValueError, json.JSONDecodeError):
            acceptance_cfg = {}
        chapter_acceptance_enabled = bool(acceptance_cfg.get("enabled", True))
        chapter_acceptance_rounds = max(0, min(4, int(acceptance_cfg.get("max_repair_rounds", 2))))
        if chapter_acceptance_enabled:
            chapter_acceptance_reviewers = build_acceptance_reviewers(acceptance_cfg)
            if control_bundle:
                try:
                    chapter_requirements = {
                        chapter_no: acceptance_requirements_for_chapter(control_bundle, chapter_no)
                        for chapter_no in chapter_range
                    }
                except ControlContractError as exc:
                    print(f"CONTROL ERROR: {exc}")
                    task_store.finish_run(run_id, args.story_id, "writing", "failed", round(time.time() - run_started, 1), str(exc))
                    return 2
            elif chapter_contract_document:
                chapter_requirements = {
                    chapter_no: requirements_for_chapter(chapter_contract_document, chapter_no)
                    for chapter_no in chapter_range
                }
            else:
                chapter_requirements = {
                    chapter_no: extract_chapter_requirements(outline_text, chapter_no)
                    for chapter_no in chapter_range
                }

    def _write_acceptance_artifact(chapter_no, history, issue_ledger=None):
        """Keep failed candidates out of the story directory but retain their audit trail."""
        target = os.path.join(HERE, "out", args.story_id, "chapter_acceptance", f"ch{chapter_no:04d}.json")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        artifact = {"story_id": args.story_id, "chapter": chapter_no, "history": history}
        if issue_ledger is not None:
            artifact["issue_ledger"] = issue_ledger
        with open(target, "w", encoding="utf-8") as handle:
            json.dump(artifact, handle, ensure_ascii=False, indent=2)

    # 块状并行块数：命令行 > config（默认 1 = 串行）
    requested_blocks = args.parallel_blocks
    if requested_blocks is None:
        requested_blocks = int(engine.w.get("parallel_blocks", 1))
    n_blocks = chapter_generation_blocks(requested_blocks)
    if int(requested_blocks) > 1:
        print("[serial] ignoring parallel-blocks: each chapter requires the verified state of its predecessor", flush=True)

    results = {}          # n -> text（各线程写不同 key）
    results_lock = threading.Lock()

    def _split_blocks(items, k):
        """把章节列表切成 k 个连续块，尽量均匀，保持章节顺序。"""
        k = max(1, min(k, len(items)))
        if k <= 1:
            return [items]
        size = (len(items) + k - 1) // k
        return [items[i:i + size] for i in range(0, len(items), size)]

    def _handoff_for(text, chapter_no=None):
        """生成本章交接卡，失败返回空串（不阻断块内后续章）。"""
        if control_bundle:
            if chapter_no is None:
                return False
            contract = control_bundle["artifacts"]["chapter_contracts"]["chapters"].get(f"{chapter_no:04d}")
            if not contract:
                return False
            engine.llm.set_call_context(purpose="control_state_delta")
            result = engine.generate_state_delta(text, chapter_no, contract, control_runtime.scenes_for(chapter_no))
            if control_runtime.record_state_delta(chapter_no, text, result):
                print(f"    [control] state delta passed ch{chapter_no}", flush=True)
                return True
            print(f"    [control] state delta failed ch{chapter_no}", flush=True)
            return False
        engine.llm.set_call_context(purpose="handoff")
        r = engine.generate_handoff(text)
        if "error" in r or not r.get("content", "").strip():
            # Deterministic fallback preserves the immediate physical state so
            # a transient reviewer/model failure cannot silently break the arc.
            tail = re.sub(r"\s+", " ", text or "")[-900:]
            print(f"    [handoff] 生成失败，使用末段状态回退: {r.get('error') if 'error' in r else 'empty'}", flush=True)
            return "Ending-state excerpt (preserve location, injuries, objects, and unresolved action): " + tail
        return r["content"].strip()

    def _write_one(n, handoff=""):
        """写一章（含质量门槛），返回 (n, text)。失败返回 (n, None)，由补跑循环重试。"""
        chapter_started = time.time()
        engine.llm.set_chapter_context(n)
        progress.update("writing", f"ch {n}/{n_chapters}", args.story_id)
        print(f"[3/4] writing chapter {n}/{n_chapters} ...", flush=True)
        handoff_block = ""
        if handoff:
            handoff_block = ("\n\nHandoff from the previous chapter (this chapter MUST start from this exact state):\n" + handoff)
        quality_feedback = ""
        blind_evidence = {}
        r = None
        text = ""
        best = None
        best_score = -1
        experience_block, selected_experiences = engine.experience_block(n)
        if experience_block:
            print(f"    [experience] ch{n}: {len(selected_experiences)} optional cue(s)", flush=True)
        # The word retry happens inside write_with_retry. Quality feedback is
        # intentionally capped at two extra full generations per chapter.
        for qa in range(1 + (2 if judge else 0)):
            chapter_store.update(
                n, ChapterStatus.WRITING.value, attempt=qa + 1,
                output_path=os.path.join(out_dir, f"ch{n:04d}.md"),
            )
            # 每次写前读本书最新优化词条（只影响本书，不碰全局/其他书）
            book_lessons = load_lessons(args.story_id)
            lessons_block = ""
            if book_lessons:
                lessons_block = ("\n\nPrevious quality issues to avoid in THIS book (hard rules for this book only):\n"
                                 + "\n".join(f"- {x}" for x in book_lessons[-20:]))
            memory_block = "" if control_bundle else book_memory.context_block(n)
            if memory_block and not handoff_block:
                memory_block = "\n\n" + memory_block
            extra = handoff_block + memory_block + args.extra_rules + lessons_block + quality_feedback
            if chapter_contract_document:
                extra += "\n\n" + writer_contract_block(chapter_contract_document, n)
            if control_bundle:
                try:
                    controlled = control_runtime.context_for(n)
                except ControlContractError as exc:
                    chapter_store.update(
                        n, ChapterStatus.FAILED.value, elapsed_s=round(time.time() - chapter_started, 1),
                        error=str(exc), failure_type=FailureType.QUALITY.value,
                    )
                    print(f"    CONTROL ERROR ch{n}: {exc}", flush=True)
                    return n, None
                extra += "\n\n" + controlled["text"]
            if experience_block:
                extra += "\n\n" + experience_block
            # Blind-review retries create a new candidate. They never mutate a
            # prior candidate because blind feedback has no verified edit span.
            r = engine.write_with_retry(direction, outline_text, bible_text, n, n_chapters, args.words, extra)
            if "error" in r:
                chapter_store.update(
                    n, ChapterStatus.FAILED.value, attempt=qa + 1,
                    elapsed_s=round(time.time() - chapter_started, 1), error=r.get("error"),
                    failure_type=r.get("failure_type") or classify_failure(r.get("error")),
                )
                break
            text = r["text"]
            strict_violations = quality_checks.strict_prose_violations(text)
            last_quality_attempt = qa == (2 if judge else 0)
            if strict_violations:
                engine.llm.set_call_context(
                    attempt=qa + 1, purpose="strict_local_repair", retry_index=qa
                )
                repaired = engine.repair_strict_prose(text)
                if repaired.get("repaired"):
                    text = repaired["text"]
                    r["text"] = text
                    r["words"] = count_words(text)
                    strict_violations = quality_checks.strict_prose_violations(text)
                    print(f"    ch{n} repaired strict rule(s) locally: {repaired['rules']}", flush=True)
            if strict_violations and not last_quality_attempt:
                quality_feedback = (
                    "\n\nHard prose-rule violation. Rewrite the chapter and remove every instance: "
                    + "; ".join(strict_violations)
                    + ". Do not replace it with another fragment-triad construction."
                )
                chapter_store.update(
                    n, ChapterStatus.RETRYING.value, attempt=qa + 1,
                    failure_type=FailureType.QUALITY.value, warning="; ".join(strict_violations),
                )
                print(f"    ch{n} strict prose rule hit, rewriting...", flush=True)
                continue
            if strict_violations:
                r["error"] = "strict prose rules unresolved: " + "; ".join(strict_violations)
                r["failure_type"] = FailureType.QUALITY.value
                break
            if not judge:
                break
            wc = r.get("words", 0)
            if wc < engine.w["word_min"] or wc > engine.w["word_max"]:
                chapter_store.update(
                    n, ChapterStatus.RETRYING.value, attempt=qa + 1,
                    failure_type=FailureType.WORD_COUNT.value, warning=engine._word_feedback(
                        text, args.words, engine.w["word_min"], engine.w["word_max"]),
                )
                quality_feedback = "\n\n" + engine._word_feedback(
                    text, args.words, engine.w["word_min"], engine.w["word_max"]
                )
                print(
                    f"    ch{n} 字数越界 {wc}，要求 {engine.w['word_min']}-{engine.w['word_max']}，重写...",
                    flush=True,
                )
                continue
            score, reason, provider = judge.blind(text)
            blind_evidence = {
                "score": score,
                "threshold": args.quality_min,
                "reason": reason,
                "provider": provider,
            }
            if score > best_score:
                best_score = score
                best = (r, text, score, reason, provider)
            if score >= args.quality_min:
                r["quality"] = score
                break
            # 只在 verdict=ai 时沉淀"要避免的 AI-tell"到本书独立词条库（隔离：不碰全局/其他书）
            v = re.search(r"verdict\s*:\s*(ai|human)", reason, re.I)
            if v and v.group(1).lower() == "ai":
                m = re.search(r"reason\s*:\s*(.+)\.?", reason, re.I)
                lesson_rule = m.group(1).strip() if m else ""
                if lesson_rule:
                    add_lesson(args.story_id, lesson_rule, f"ch{n} 盲评 {score} 分")
            quality_feedback = f"\n\nQuality issue to fix (rewrite to avoid this): {reason}"
            chapter_store.update(
                n, ChapterStatus.RETRYING.value, attempt=qa + 1,
                failure_type=FailureType.QUALITY.value, warning=reason[:500],
            )
            print(f"    ch{n} 质量 {score}<{args.quality_min}[{provider}], 带反馈重写...", flush=True)
        if judge and best is not None and not (r or {}).get("quality"):
            r, text, score, reason, provider = best
            r["quality"] = score
            r["quality_warning"] = f"best candidate below gate ({score}<{args.quality_min})"
            r["failure_type"] = FailureType.QUALITY.value
            r["error"] = f"quality gate not met: best={score}, required={args.quality_min} ({provider})"
            print(f"    ch{n} 质量门槛失败 {score}[{provider}]<{args.quality_min}", flush=True)
        if not r or "error" in r:
            print(f"    CH{n} ERROR: {r['error']}")
            chapter_store.update(
                n, ChapterStatus.FAILED.value,
                elapsed_s=round(time.time() - chapter_started, 1), error=r.get("error"),
                failure_type=r.get("failure_type") or classify_failure(r.get("error")),
            )
            return n, None
        # Conservative cleanup of adjacent model paste duplicates, then lint.
        text, removed_repeats = quality_checks.remove_exact_repeats(text)
        if removed_repeats:
            r["warning"] = f"removed {removed_repeats} adjacent duplicate paragraph(s)"
        acceptance = None
        if chapter_acceptance_enabled:
            acceptance_history = []
            issue_ledger = None
            for acceptance_round in range(chapter_acceptance_rounds + 1):
                local_blockers = quality_checks.strict_prose_violations(text)
                words = count_words(text)
                if words < engine.w["word_min"] or words > engine.w["word_max"]:
                    local_blockers.append(engine._word_feedback(
                        text, args.words, engine.w["word_min"], engine.w["word_max"]
                    ))
                if issue_ledger is None:
                    acceptance = review_chapter_acceptance(
                        f"ch{n:04d}.md", text, chapter_requirements.get(n, []), chapter_acceptance_reviewers,
                    )
                else:
                    acceptance = review_chapter_acceptance(
                        f"ch{n:04d}.md", text, chapter_requirements.get(n, []), chapter_acceptance_reviewers,
                        issue_ledger=issue_ledger,
                    )
                acceptance_gate = quality_gate.chapter_acceptance_decision(
                    acceptance, text, local_blockers, issue_ledger=issue_ledger,
                )
                ledger_frozen_this_round = False
                if (chapter_contract_document or control_bundle) and issue_ledger is None and acceptance_gate["decision"] != "verification_failed":
                    try:
                        issue_ledger = freeze_acceptance_issue_ledger(n, acceptance)
                        ledger_frozen_this_round = True
                    except ValueError as exc:
                        acceptance_gate = {"decision": "verification_failed", "blockers": [str(exc)]}
                acceptance_history.append({
                    "attempt": acceptance_round + 1,
                    "candidate_sha256": sha256_text(text),
                    "review": acceptance,
                    "gate": acceptance_gate,
                })
                _write_acceptance_artifact(n, acceptance_history, issue_ledger)
                if acceptance_gate["decision"] == "passed":
                    print(f"    ch{n} chapter acceptance passed on attempt {acceptance_round + 1}", flush=True)
                    break
                if acceptance_gate["decision"] == "verification_failed":
                    failure = "; ".join(acceptance_gate["blockers"])
                    chapter_store.update(
                        n, ChapterStatus.FAILED.value, elapsed_s=round(time.time() - chapter_started, 1),
                        error=failure, failure_type=FailureType.QUALITY.value,
                    )
                    print(f"    CH{n} acceptance verification failed: {failure}", flush=True)
                    return n, None
                if acceptance_round >= chapter_acceptance_rounds:
                    failure = "chapter acceptance unresolved after repair budget: " + "; ".join(acceptance_gate["blockers"])
                    chapter_store.update(
                        n, ChapterStatus.FAILED.value, elapsed_s=round(time.time() - chapter_started, 1),
                        error=failure, failure_type=FailureType.QUALITY.value,
                    )
                    print(f"    CH{n} acceptance failed after {acceptance_round + 1} audit(s): {failure}", flush=True)
                    return n, None
                chapter_store.update(
                    n, ChapterStatus.RETRYING.value, attempt=acceptance_round + 1,
                    failure_type=FailureType.QUALITY.value, warning="; ".join(acceptance_gate["blockers"])[:500],
                )
                engine.llm.set_call_context(
                    attempt=acceptance_round + 1, purpose="chapter_acceptance_repair", retry_index=acceptance_round,
                )
                repair_review = acceptance
                if issue_ledger is not None and not ledger_frozen_this_round:
                    repair_review = dict(acceptance)
                    repair_review["findings"] = acceptance_gate["repair_findings"]
                repaired = engine.repair_chapter_with_verified_patches(text, repair_review)
                acceptance_history[-1]["repair"] = repaired.get("audit") or {"status": "rejected"}
                _write_acceptance_artifact(n, acceptance_history, issue_ledger)
                if "error" in repaired or not repaired.get("content", "").strip():
                    failure = repaired.get("error", "chapter acceptance repair returned empty text")
                    chapter_store.update(
                        n, ChapterStatus.FAILED.value, elapsed_s=round(time.time() - chapter_started, 1),
                        error=failure, failure_type=FailureType.QUALITY.value,
                    )
                    print(f"    CH{n} acceptance repair failed: {failure}", flush=True)
                    return n, None
                text = repaired["content"].strip()
                r["text"] = text
                r["words"] = count_words(text)
                print(f"    ch{n} acceptance findings -> bounded revision {acceptance_round + 1}/{chapter_acceptance_rounds}", flush=True)
        issues, dens = engine.ai_tell_check(text)
        local_warnings = quality_checks.local_checks(
            text, target=args.words, lo=engine.w["word_min"], hi=engine.w["word_max"]
        )
        local_warnings.extend(quality_checks.repetition_check(text))
        local_warnings.extend(quality_checks.redundancy_check(text))
        if control_bundle:
            budget_findings = control_runtime.post_write_findings(n, text)
            if budget_findings:
                message = "; ".join(finding["message"] for finding in budget_findings)
                chapter_store.update(
                    n, ChapterStatus.FAILED.value, elapsed_s=round(time.time() - chapter_started, 1),
                    error=message, failure_type=FailureType.QUALITY.value,
                )
                print(f"    CONTROL ERROR ch{n}: {message}", flush=True)
                return n, None
        q = f", quality={r.get('quality','?')}" if judge else ""
        print(f"    ch{n}: {r['words']} words, {r['elapsed']}s, rewrites={r['rewrites']}{q}, ai-tells={issues or 'clean'}, local={local_warnings or 'clean'}")
        state_ok = _handoff_for(text, n) if control_bundle else True
        previous_state = load_state_delta(control_dir, n - 1) if control_bundle and n > 1 else None
        current_state = quality_gate.load_state_evidence(
            os.path.join(control_dir, "state", f"state_delta_ch{n:04d}.json")
        ) if control_bundle else None
        state_failure = "controlled state delta did not pass; next chapter is blocked" if not state_ok else None
        quality_report = quality_gate.build_report(
            text, n, blind_evidence,
            previous_state=previous_state,
            current_state=current_state,
            local_warnings=local_warnings,
            state_failure=state_failure,
            acceptance=acceptance,
        )
        quality_gate.write_report(
            os.path.join(out_dir, f"ch{n:04d}.quality_gate.json"), quality_report
        )
        if not state_ok:
            failure = state_failure
            chapter_store.update(
                n, ChapterStatus.FAILED.value, elapsed_s=round(time.time() - chapter_started, 1),
                error=failure, failure_type=FailureType.QUALITY.value,
            )
            print(f"    CONTROL ERROR ch{n}: {failure}", flush=True)
            return n, None
        chap_file = os.path.join(out_dir, f"ch{n:04d}.md")
        try:
            open(chap_file, "w", encoding="utf-8").write(text)
        except OSError as exc:
            failure = f"{type(exc).__name__}: {exc}"
            chapter_store.update(
                n, ChapterStatus.FAILED.value, attempt=(r.get("rewrites", 0) + 1),
                elapsed_s=round(time.time() - chapter_started, 1), error=failure,
                failure_type=FailureType.FILE.value,
            )
            print(f"    CH{n} FILE ERROR: {failure}", flush=True)
            return n, None
        chapter_store.update(
            n, ChapterStatus.COMPLETED.value, quality=r.get("quality"),
            words=r.get("words"), elapsed_s=round(time.time() - chapter_started, 1),
            output_path=chap_file, error=None,
            warning="; ".join(local_warnings) or r.get("quality_warning"),
            failure_type=None,
        )
        with results_lock:
            results[n] = text
        return n, text

    def run_block(block):
        """块内串行：写一章 → 生成交接卡 → 注入下一章。"""
        handoff = ""
        for index, n in enumerate(block):
            nn, text = _write_one(n, handoff)
            if control_bundle and not text:
                # State, scene, and fact validation are the cross-chapter
                # authority in controlled mode. Continuing would create a
                # later chapter from an explicitly failed predecessor.
                print(f"    [control] stopping serial run after failed ch{n}", flush=True)
                break
            if text:
                handoff = "" if control_bundle else _handoff_for(text, n)
                try:
                    book_memory.save_chapter(n, text, handoff, outline_text)
                except Exception as exc:
                    print(f"    [memory] save failed ch{n}: {exc}", flush=True)
            if not text:
                # A failed predecessor invalidates every later chapter in the
                # same serial block; do not create an ungrounded continuation.
                for later in block[index + 1:]:
                    chapter_store.update(
                        later, ChapterStatus.BLOCKED.value,
                        error=f"blocked by failed chapter {n}",
                        failure_type=FailureType.DEPENDENCY.value,
                    )
                print(f"    [serial] stopping block after failed ch{n}", flush=True)
                break

    # 主循环：块状并行（块内串行 + 块间并行）
    blocks = _split_blocks(chapter_range, n_blocks)
    mode = "串行" if len(blocks) == 1 else f"块状并行 {len(blocks)} 块（每块约 {len(blocks[0])} 章）"
    print(f"[3/4] 写章模式: {mode}, 共 {len(chapter_range)} 章", flush=True)
    t_start = time.time()
    if len(blocks) == 1:
        run_block(blocks[0])
    else:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=len(blocks)) as ex:
            list(ex.map(run_block, blocks))

    # 路4：失败章节自动入队补跑（等通道恢复，最多 3 轮，不丢章）
    missing = [n for n in chapter_range if n not in results]
    initial_rows = {int(row["chapter_no"]): row for row in chapter_store.list()}
    for n in missing:
        row = initial_rows.get(n) or {}
        if row.get("status") == ChapterStatus.PENDING.value:
            # The serial loop did not start this chapter because the earlier
            # dependency chain stopped. Pending would falsely imply it is safe
            # to schedule independently on recovery.
            chapter_store.update(
                n, ChapterStatus.BLOCKED.value,
                error="serial predecessor chain did not complete",
                failure_type=FailureType.DEPENDENCY.value,
            )
    recovery_rounds = max(0, int(engine.w.get("recovery_rounds", 3)))
    recovery_wait = max(0, float(engine.w.get("recovery_wait_seconds", 90)))
    for round_i in range(recovery_rounds):
        if not missing:
            break
        rows = {int(row["chapter_no"]): row for row in chapter_store.list()}
        def predecessor_available(n):
            if n <= 1 or n - 1 in results:
                return True
            # Resume/append runs may have a predecessor from an earlier run.
            return n - 1 not in chapter_range and os.path.exists(
                os.path.join(out_dir, f"ch{n - 1:04d}.md")
            )

        start = serial_recovery_start(missing, rows, predecessor_available)
        if start is None:
            print(f"[retry] 不自动补跑不可恢复章节: {missing}", flush=True)
            break
        suffix = [n for n in chapter_range if n >= start and n in missing]
        print(f"\n[retry] 从 ch{start} 串行恢复 {len(suffix)} 章（第 {round_i+1} 轮，等 {recovery_wait:g}s 让通道恢复）...", flush=True)
        chapter_store.update(
            start, ChapterStatus.RETRY_WAIT.value,
            failure_type=(rows[start].get("failure_type") or FailureType.NETWORK.value),
            warning=f"recovery round {round_i + 1}",
        )
        if recovery_wait:
            time.sleep(recovery_wait)
        prev = results.get(start - 1)
        if prev is None and start > 1 and start - 1 not in chapter_range:
            prev_path = os.path.join(out_dir, f"ch{start - 1:04d}.md")
            if os.path.exists(prev_path):
                prev = open(prev_path, encoding="utf-8").read()
        handoff = _handoff_for(prev, start - 1) if prev else ""
        for index, n in enumerate(suffix):
            nn, text = _write_one(n, handoff)
            if not text:
                for later in suffix[index + 1:]:
                    chapter_store.update(
                        later, ChapterStatus.BLOCKED.value,
                        error=f"blocked by failed chapter {n}",
                        failure_type=FailureType.DEPENDENCY.value,
                    )
                break
            handoff = "" if control_bundle else _handoff_for(text, n)
            try:
                book_memory.save_chapter(n, text, handoff, outline_text)
            except Exception as exc:
                print(f"    [memory] recovery save failed ch{n}: {exc}", flush=True)
        missing = [n for n in chapter_range if n not in results]
    if missing:
        print(f"WARNING: 以下章节 3 轮补跑后仍失败，需人工处理: {missing}", flush=True)

    # A targeted resume (for example, repairing only chapter 12) must retain
    # the book-level completion record instead of reporting a one-chapter book.
    status_expected = len(chapter_range)
    status_completed = results
    status_missing = missing
    if args.resume and not args.append_chapters:
        status_expected = n_chapters
        status_completed = {
            n: os.path.join(out_dir, f"ch{n:04d}.md")
            for n in range(1, n_chapters + 1)
            if os.path.exists(os.path.join(out_dir, f"ch{n:04d}.md"))
        }
        status_missing = [n for n in range(1, n_chapters + 1) if n not in status_completed]
    run_status = "completed" if not missing else "partial"
    write_status(
        out_dir,
        run_id=run_id,
        story_id=args.story_id,
        expected_chapters=status_expected,
        completed_chapters=status_completed,
        missing_chapters=status_missing,
    )
    task_store.finish_run(
        run_id, args.story_id, "writing", run_status,
        round(time.time() - run_started, 1),
        f"missing chapters: {missing}" if missing else None,
    )

    if missing:
        print("[writing] partial run is not deliverable; skipping summary", flush=True)
        return 2
    if not results:
        print("no chapters written")
        return 2
    continuity_report = {}
    ordered = sorted(results)
    for prev_n, curr_n in zip(ordered, ordered[1:]):
        warnings = quality_checks.continuity_check(results[prev_n], results[curr_n])
        if warnings:
            continuity_report[f"ch{prev_n:04d}->ch{curr_n:04d}"] = warnings
    if continuity_report:
        report_path = os.path.join(out_dir, "quality_checks.json")
        json.dump({"continuity": continuity_report}, open(report_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(f"[quality] continuity warnings: {continuity_report}", flush=True)
    print(f"[3/4] 写章完成: {len(results)}/{len(chapter_range)} 章, 总耗时 {time.time() - t_start:.0f}s", flush=True)

    # 4. 中文摘要
    print("[4/4] generating cn summary ...")
    full_path = assemble_full_book(out_dir, n_chapters)
    joined = open(full_path, encoding="utf-8").read()
    summ = engine.generate_cn_summary(joined, outline_text, direction)
    if "error" not in summ:
        open(os.path.join(out_dir, "summary_cn.md"), "w", encoding="utf-8").write(summ["content"])
        print("    summary done")
    print(f"\nDONE: {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
