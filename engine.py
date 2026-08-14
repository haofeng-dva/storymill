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
import time
import urllib.request
import urllib.error

import progress
from profiles import get_writer_profile

HERE = os.path.dirname(os.path.abspath(__file__))


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def lessons_path(story_id):
    """某本书独立的优化词条库路径（只影响本书，不碰全局规则包）。"""
    return os.path.join(HERE, "shorts", story_id, "lessons.json")


def load_lessons(story_id):
    """读某本书的优化词条，返回规则列表。"""
    path = lessons_path(story_id)
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

    def _chat_ark(self, model, messages, max_tokens, temperature):
        body = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        req = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                data = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return {"error": f"HTTP {e.code}: {e.read().decode('utf-8', 'ignore')[:400]}"}
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

    def _chat_gemini(self, model, messages, max_tokens, temperature):
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

    def _chat_relay(self, model, messages, max_tokens, temperature):
        """GPT 中转（OpenAI responses API），system 角色转 developer。"""
        input_msgs = []
        for m in messages:
            role = "developer" if m["role"] == "system" else m["role"]
            input_msgs.append({"role": role, "content": m["content"]})
        body = {
            "model": model,
            "input": input_msgs,
            "max_output_tokens": max_tokens,
        }
        req = urllib.request.Request(
            self.relay_base + "/v1/responses",
            data=json.dumps(body).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.relay_key}", "Content-Type": "application/json"},
        )
        t0 = time.time()
        data = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=300) as r:
                    data = json.loads(r.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as e:
                if e.code in (400, 500, 502, 504) and attempt < 2:
                    time.sleep(3 * (attempt + 1))  # 中转波动/上游偶发失败重试
                    continue
                return {"error": f"HTTP {e.code}: {e.read().decode('utf-8', 'ignore')[:400]}"}
            except Exception as e:
                if attempt == 2:
                    return {"error": f"{type(e).__name__}: {str(e)[:200]}"}
                time.sleep(3 * (attempt + 1))
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

    def chat(self, role, messages, max_tokens=2000, temperature=0.85):
        model = self.models[role]
        backend = get_writer_profile(model)["backend"]
        if backend == "gemini":
            return self._chat_gemini(model, messages, max_tokens, temperature)
        if backend == "relay":
            return self._chat_relay(model, messages, max_tokens, temperature)
        return self._chat_ark(model, messages, max_tokens, temperature)


class WritingEngine:
    def __init__(self, config_path=None):
        config_path = config_path or os.path.join(HERE, "config.json")
        self.cfg = load_json(config_path)
        self.llm = LLM(self.cfg)
        self.tells = load_json(os.path.join(HERE, "ai_tells_en.json"))
        self.w = self.cfg["writing"]
        self.anchor_examples = []
        self.genre = None

    def _load_anchors(self, genre=None):
        """加载风格样本库。genre 指定子目录；None 时加载全部子目录。"""
        base = os.path.join(HERE, self.cfg["paths"]["anchor_examples"])
        if genre and os.path.isdir(os.path.join(base, genre)):
            dirs = [os.path.join(base, genre)]
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
        sys_msg = (
            "You are a professional English short-fiction planner. "
            "Design a tight, publishable serialized short story outline."
        )
        user = (
            f"Story direction: {direction}\n\n"
            f"Target: {self.w['default_chapters']} chapters, {self.w['default_words']} words each.\n\n"
            "Output a concise outline with:\n"
            "1. Logline (one sentence)\n"
            "2. Protagonist (name, flaw, want)\n"
            "3. 3-act structure summary\n"
            "4. Chapter-by-chapter beats. For EACH chapter, specify 2-3 scene beats, "
            "including at least one dialogue exchange AND one concrete detail or revelation. "
            "Each chapter's beats must be substantial enough to sustain ~" + str(self.w['default_words']) + " words of prose without padding or repetition.\n"
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

    def write_world_bible(self, direction, outline):
        """生成设定圣经：力量体系/世界规则/人物卡，写章时逐章对照保持一致性。"""
        sys_msg = (
            "You are a worldbuilding designer for serialized fiction. "
            "Create a compact, internally consistent world bible."
        )
        user = (
            f"Story direction: {direction}\n\n"
            f"Outline:\n{outline}\n\n"
            "Based on the outline, create a concise world bible covering:\n"
            "1. Power/magic system (if any): levels, rules, costs, limits\n"
            "2. World rules: 3-5 key rules of this world that shape the plot\n"
            "3. Characters: protagonist + key cast (name, flaw, want, role)\n"
            "4. Key items & locations: objects/places that recur\n"
            "Keep under 600 words. Plain prose, no markdown headers. "
            "These settings MUST stay consistent across all chapters."
        )
        r = self.llm.chat("outline", [
            {"role": "system", "content": sys_msg},
            {"role": "user", "content": user},
        ], max_tokens=2200, temperature=0.7)
        return r

    def _minimal_user(self, direction, outline, world_bible, chapter_n, total, target_words, word_feedback):
        """精简包：thinking 强模型用。只给方向+大纲+字数，加两条极简关键约束（禁 on-the-nose 隐喻 + 禁背景堆砌）。"""
        bible = f"\n\nWorld bible (settings that MUST stay consistent):\n{world_bible}" if world_bible else ""
        fb = f"\n\nWord count correction: {word_feedback}" if word_feedback else ""
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
            + anchor
            + forbidden
            + extra
            + fb
            + "\n\nOutput only the chapter text, no headers, no commentary."
        )

    def _write_chapter_segmented(self, direction, outline, world_bible, chapter_n, total, target_words, extra_rules=""):
        """GPT 中转对长输出 60s 超时，分段写（每段 ~300 词）续写拼接。"""
        seg_words = 300
        n_segs = max(1, (target_words + seg_words - 1) // seg_words)
        bible = f"\n\nWorld bible (settings that MUST stay consistent):\n{world_bible}" if world_bible else ""
        base = (
            f"Story direction: {direction}\n\nOutline:\n{outline}\n" + bible +
            f"\n\nWrite Chapter {chapter_n} of {total}, following the outline's beats. Third person, past tense.\n"
            "Two hard rules: no on-the-nose metaphors mapping a profession onto emotions; no opening exposition dumps.\n"
        )
        full = ""
        r = {}
        for i in range(n_segs):
            if i == 0:
                user = base + f"Write the opening ~{seg_words} words of this chapter. Output only prose."
            else:
                user = base + f"Continue the chapter seamlessly, writing the next ~{seg_words} words. Output only prose.\n\nWritten so far:\n{full[-1500:]}"
            r = self.llm.chat("writer", [
                {"role": "system", "content": "You are a literary fiction writer. Write vivid, natural English prose."},
                {"role": "user", "content": user},
            ], max_tokens=2000, temperature=self.w["temperature"])
            if "error" in r:
                return r
            seg = r["content"].strip()
            full = (full + "\n\n" + seg).strip()
            if count_words(full) >= target_words:
                break
        return {**r, "content": full}

    def write_chapter(self, direction, outline, world_bible, chapter_n, total, target_words, word_feedback="", extra_rules=""):
        sys_msg = (
            "You are a literary fiction writer. Write vivid, natural, contemporary English prose. "
            "You are writing one chapter of a serialized short story."
        )
        profile = get_writer_profile(self.llm.models.get("writer", ""))
        if profile["backend"] == "relay":
            return self._write_chapter_segmented(direction, outline, world_bible, chapter_n, total, target_words, extra_rules)
        if profile["prompt_pack"] == "minimal":
            user = self._minimal_user(direction, outline, world_bible, chapter_n, total, target_words, word_feedback)
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
            fb = "" if attempt == 0 else self._word_feedback(text, target_words, lo, hi)
            r = self.write_chapter(direction, outline, world_bible, chapter_n, total, target_words, fb, extra_rules)
            if "error" in r:
                return r
            text = r["content"]
            wc = count_words(text)
            if lo <= wc <= hi:
                return {**r, "text": text, "words": wc, "rewrites": attempt}
        # 重试耗尽：返回最后一次
        return {**r, "text": text, "words": count_words(text), "rewrites": self.w["max_rewrite"], "warn": "word target missed"}

    def generate_cn_summary(self, text):
        """生成中文摘要，供看不懂英文的人判断方向对不对。"""
        r = self.llm.chat("cn_summary", [
            {"role": "system", "content": "你是小说内容摘要助手，用中文输出。"},
            {"role": "user", "content": f"把下面这段英文小说用中文概括成 200 字以内的情节摘要（发生了什么，主角是谁，关键冲突）：\n\n{text[:5000]}"},
        ], max_tokens=600, temperature=0.4)
        return r


def main():
    ap = argparse.ArgumentParser(description="InkOS 替代：英文写作引擎")
    ap.add_argument("--direction", help="故事方向文本（与 --direction-file 二选一）")
    ap.add_argument("--direction-file", help="方向文件路径（由 direction_manager 生成）")
    ap.add_argument("--chapters", type=int, default=12)
    ap.add_argument("--words", type=int, default=700)
    ap.add_argument("--story-id", default="test")
    ap.add_argument("--genre", default=None, help="题材目录名：domestic_suspense / webfiction / literary；不填则从 direction 自动检测")
    ap.add_argument("--outline-only", action="store_true", help="只出大纲，不写正文")
    ap.add_argument("--chapter", type=int, default=0, help="只写指定章（0=全部）")
    ap.add_argument("--extra-rules", default="", help="额外写作规则（从盲评迭代回灌），追加注入写章 prompt")
    ap.add_argument("--quality-gate", action="store_true", help="质量门槛：每章盲评 naturalness < quality-min 则带反馈重写")
    ap.add_argument("--quality-min", type=int, default=4, help="质量门槛最低 naturalness（默认 4）")
    args = ap.parse_args()

    # 解析方向：--direction-file 优先，否则 --direction
    if args.direction_file:
        direction = open(args.direction_file, encoding="utf-8").read().strip()
        print(f"[direction] loaded from {args.direction_file}")
    elif args.direction:
        direction = args.direction
    else:
        print("需要 --direction 或 --direction-file")
        sys.exit(1)

    load_env()
    engine = WritingEngine()
    genre = engine.set_genre(direction, args.genre)
    print(f"[genre] {genre or 'general'} ({len(engine.anchor_examples)} anchor sample(s))")
    out_dir = os.path.join(HERE, engine.cfg["paths"]["output"], args.story_id)
    os.makedirs(out_dir, exist_ok=True)

    # 1. 大纲
    print(f"[1/4] generating outline for: {direction[:60]}...")
    outline = engine.write_outline(direction)
    if "error" in outline:
        print("OUTLINE ERROR:", outline["error"])
        sys.exit(1)
    outline_text = outline["content"]
    open(os.path.join(out_dir, "outline.md"), "w", encoding="utf-8").write(outline_text)
    print(f"    outline done ({outline['elapsed']}s, {len(outline_text)} chars)")

    if args.outline_only:
        print("\n" + outline_text)
        return

    # 2. 设定圣经（world bible）
    print("[2/4] generating world bible ...")
    bible = engine.write_world_bible(direction, outline_text)
    bible_text = bible["content"] if "error" not in bible else ""
    if bible_text:
        open(os.path.join(out_dir, "world_bible.md"), "w", encoding="utf-8").write(bible_text)
        print(f"    world bible done ({bible['elapsed']}s, {len(bible_text)} chars)")
    else:
        print(f"    world bible FAILED: {bible.get('error')}")

    # 3. 写章
    n_chapters = args.chapters
    if args.chapter > 0:
        chapter_range = [args.chapter]
    else:
        chapter_range = list(range(1, n_chapters + 1))

    # 质量门槛：可选盲评重写（确保每章 naturalness 达标）
    judge = None
    if args.quality_gate:
        from native_check import QualityJudge
        judge = QualityJudge(os.environ)

    full_text = []
    for n in chapter_range:
        progress.update("writing", f"ch {n}/{n_chapters}", args.story_id)
        print(f"[3/4] writing chapter {n}/{n_chapters} ...", flush=True)
        quality_feedback = ""
        r = None
        text = ""
        for qa in range(1 + (4 if judge else 0)):
            # 每次写前读本书最新优化词条（只影响本书，不碰全局/其他书）
            book_lessons = load_lessons(args.story_id)
            lessons_block = ""
            if book_lessons:
                lessons_block = ("\n\nPrevious quality issues to avoid in THIS book (hard rules for this book only):\n"
                                 + "\n".join(f"- {x}" for x in book_lessons[-20:]))
            r = engine.write_with_retry(direction, outline_text, bible_text, n, n_chapters, args.words, args.extra_rules + lessons_block + quality_feedback)
            if "error" in r:
                break
            text = r["text"]
            if not judge:
                break
            wc = r.get("words", 0)
            if wc < engine.w["word_min"]:
                quality_feedback = f"\n\nWord count too short ({wc} words). Write the FULL {args.words} words, do not stop early."
                print(f"    ch{n} 字数不足 {wc}<{engine.w['word_min']}, 重写...", flush=True)
                continue
            score, reason, provider = judge.blind(text)
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
            print(f"    ch{n} 质量 {score}<{args.quality_min}[{provider}], 带反馈重写...", flush=True)
        if "error" in r:
            print(f"    CH{n} ERROR: {r['error']}")
            continue
        issues, dens = engine.ai_tell_check(text)
        q = f", quality={r.get('quality','?')}" if judge else ""
        print(f"    ch{n}: {r['words']} words, {r['elapsed']}s, rewrites={r['rewrites']}{q}, ai-tells={issues or 'clean'}")
        chap_file = os.path.join(out_dir, f"ch{n:04d}.md")
        open(chap_file, "w", encoding="utf-8").write(text)
        full_text.append(text)

    if not full_text:
        print("no chapters written")
        return

    # 4. 中文摘要
    print("[4/4] generating cn summary ...")
    joined = "\n\n".join(full_text)
    full_file = os.path.join(out_dir, "full.md")
    open(full_file, "w", encoding="utf-8").write(joined)
    summ = engine.generate_cn_summary(joined)
    if "error" not in summ:
        open(os.path.join(out_dir, "summary_cn.md"), "w", encoding="utf-8").write(summ["content"])
        print("    summary done")
    print(f"\nDONE: {out_dir}")


if __name__ == "__main__":
    main()
