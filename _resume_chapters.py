# -*- coding: utf-8 -*-
"""历史试验补跑脚本，直接执行已禁用。

生产续写必须通过 ``engine.py --append-chapters`` 或受审计的 ``--resume``
进入完整的章节验收、修复和复查闭环。此脚本保留为实验实现参考。
"""
if __name__ == "__main__":
    raise SystemExit(
        "_resume_chapters.py is archived and cannot produce chapters; "
        "use engine.py --append-chapters or engine.py --resume instead."
    )

import os
import re
import sys
import time

from engine import WritingEngine, load_env, load_lessons, add_lesson
from native_check import QualityJudge
import progress

load_env()
SID = "trial_2000_v2"
D = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shorts", SID)

outline = open(os.path.join(D, "outline.md"), encoding="utf-8").read()
bible = open(os.path.join(D, "world_bible.md"), encoding="utf-8").read()
m = re.search(r"Logline:\s*(.+)", outline)
direction = m.group(1).strip() if m else "progression fantasy serial"

eng = WritingEngine()
eng.set_genre(direction, None)
judge = QualityJudge(os.environ)
wmin = eng.w["word_min"]
# 字数/章数从 config 读，改 default_words/default_chapters 一处即联动
WORDS = eng.w["default_words"]
TOTAL = eng.w["default_chapters"]


def write_one(n):
    progress.update("writing", f"ch {n}/{TOTAL}", SID)
    print(f"[3/4] writing chapter {n}/{TOTAL} ...", flush=True)
    quality_feedback = ""
    r = None
    text = ""
    for qa in range(1 + 4):
        lessons = load_lessons(SID)
        block = ""
        if lessons:
            block = ("\n\nPrevious quality issues to avoid in THIS book (hard rules for this book only):\n"
                     + "\n".join(f"- {x}" for x in lessons[-20:]))
        r = eng.write_with_retry(direction, outline, bible, n, TOTAL, WORDS, block + quality_feedback)
        if "error" in r:
            print(f"    CH{n} ERROR: {r['error']}", flush=True)
            return False
        text = r["text"]
        wc = r.get("words", 0)
        if wc < wmin:
            quality_feedback = f"\n\nWord count too short ({wc}). Write the FULL {WORDS} words."
            print(f"    字数不足 {wc}, 重写", flush=True)
            continue
        score, reason, provider = judge.blind(text)
        if score >= 4:
            r["quality"] = score
            break
        v = re.search(r"verdict\s*:\s*(ai|human)", reason, re.I)
        if v and v.group(1).lower() == "ai":
            mm = re.search(r"reason\s*:\s*(.+)\.?", reason, re.I)
            rule = mm.group(1).strip() if mm else ""
            if rule:
                add_lesson(SID, rule, f"ch{n} 盲评 {score} 分")
        quality_feedback = f"\n\nQuality issue to fix (rewrite to avoid this): {reason}"
        print(f"    质量 {score}<4[{provider}], 带反馈重写", flush=True)
    issues, _ = eng.ai_tell_check(text)
    print(f"    ch{n}: {r['words']} words, quality={r.get('quality','?')}, ai-tells={issues or 'clean'}", flush=True)
    open(os.path.join(D, f"ch{n:04d}.md"), "w", encoding="utf-8").write(text)
    return True


# 主循环：跳过已存在章节（断点续跑）+ 失败自动补跑（最多 3 轮）
pending = []
for n in range(3, TOTAL + 1):
    if os.path.exists(os.path.join(D, f"ch{n:04d}.md")):
        print(f"    ch{n} 已存在，跳过", flush=True)
        continue
    if not write_one(n):
        pending.append(n)
for round_i in range(3):
    if not pending:
        break
    print(f"\n[retry] 补跑 {len(pending)} 个失败章节（第 {round_i+1} 轮，等 90s 让通道恢复）...", flush=True)
    time.sleep(90)
    retry = []
    for n in pending:
        if not write_one(n):
            retry.append(n)
    pending = retry
if pending:
    print(f"WARNING: 3 轮补跑后仍失败，需人工处理: {pending}", flush=True)

# 合订 + 摘要（按章节号顺序）
parts = []
for n in range(1, TOTAL + 1):
    p = os.path.join(D, f"ch{n:04d}.md")
    if os.path.exists(p):
        parts.append(open(p, encoding="utf-8").read())
joined = "\n\n".join(parts)
open(os.path.join(D, "full.md"), "w", encoding="utf-8").write(joined)
print(f"\nfull.md 合订: {len(joined)} 字符")
summ = eng.generate_cn_summary(joined)
if "error" not in summ:
    open(os.path.join(D, "summary_cn.md"), "w", encoding="utf-8").write(summ["content"])
    print("摘要已生成")
print("DONE")
