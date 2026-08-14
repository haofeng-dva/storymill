# -*- coding: utf-8 -*-
"""
blind_review.py — 英文质量盲评（解黑盒的最后一步）

让 Gemini（母语级，独立评委）盲评对比：
  自研引擎产出（AI 写） vs 真人写的网文范文
不告诉它哪篇是谁写的，看它能不能分辨出 AI 篇，并逐篇评分。

用法:
    py -3 blind_review.py
"""
import json
import os
import random
import re

from native_check import GeminiReviewer, load_env

HERE = os.path.dirname(os.path.abspath(__file__))


def load_ai_samples():
    """收集自研引擎产出的章节（AI 写）。"""
    samples = []
    shorts = os.path.join(HERE, "shorts")
    for sid in sorted(os.listdir(shorts)):
        d = os.path.join(shorts, sid)
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if re.match(r"ch\d+\.md$", fn) and ".revised" not in fn:
                text = open(os.path.join(d, fn), encoding="utf-8").read()
                samples.append({"source": f"AI ({sid}/{fn})", "text": text[:1500]})
                break
        if len(samples) >= 2:
            break
    return samples


def load_human_samples():
    """真人写的范文（anchor_examples 里的名著/网文开头）。"""
    samples = []
    anchor = os.path.join(HERE, "anchor_examples")
    for genre in ["webfiction", "literary"]:
        d = os.path.join(anchor, genre)
        if os.path.isdir(d):
            for fn in sorted(os.listdir(d)):
                if fn.endswith(".md"):
                    text = open(os.path.join(d, fn), encoding="utf-8").read()
                    samples.append({"source": f"human ({genre}/{fn})", "text": text[:1500]})
                    break
    return samples


def main():
    env = load_env()
    gemini = GeminiReviewer(env)
    if not gemini.key:
        print("ERROR: .env 缺少 GEMINI_API_KEY")
        return

    ai_samples = load_ai_samples()
    human_samples = load_human_samples()
    print(f"[samples] AI {len(ai_samples)} 篇, human {len(human_samples)} 篇")

    # 打乱顺序，编匿名标签
    pool = ai_samples[:2] + human_samples[:2]
    random.shuffle(pool)
    labeled = [{"label": f"样本{chr(65+i)}", "text": s["text"], "true": s["source"]} for i, s in enumerate(pool)]

    # 构造盲评 prompt（不给真实来源）
    blocks = "\n\n".join(f"[{s['label']}]\n{s['text']}\n[/{s['label']}]" for s in labeled)
    prompt = (
        "下面是四段英文小说开头，其中混有 AI 写的和真人写的。\n"
        "请对每段分别给出：\n"
        "1. 地道性 1-5（1=明显机翻/不自然，5=母语级流畅）\n"
        "2. 可读性 1-5\n"
        "3. 判断：这段是 AI 写的还是真人写的（ai / human），并给一句理由\n\n"
        + blocks
    )
    r = gemini._call(
        "You are a literary critic with a sharp eye for machine-written prose. Judge each sample objectively.",
        prompt,
    )
    if "error" in r:
        print("盲评失败:", r["error"])
        return
    print("\n===== Gemini 盲评结果 =====\n")
    print(r["content"])
    print("\n===== 真实答案 =====")
    for s in labeled:
        print(f"  {s['label']}: {s['true']}")


if __name__ == "__main__":
    main()
