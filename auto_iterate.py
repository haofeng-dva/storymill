# -*- coding: utf-8 -*-
"""
auto_iterate.py — 自动迭代闭环（v1 核心改进机制）

写 → 审(挑问题+出规则) → 盲评(判断AI/真人) → 回灌规则 → 再写
直到盲评误判 human 且地道性≥4，或达到最大迭代次数。
"""
import json
import os
import re

from engine import WritingEngine
from native_check import GeminiReviewer, load_env

HERE = os.path.dirname(os.path.abspath(__file__))
MAX_ITER = 8
TARGET_WORDS = 700
STORY_ID = "iterate_v1"

DIRECTION = ("US-market domestic suspense: a woman's husband vanishes overnight "
             "and she finds a second identity and a rented apartment he kept hidden")


def review_round(gemini, text):
    """审稿：挑问题 + 输出可回灌的写作规则。"""
    return gemini._call(
        "You are a ruthless native English editor and literary critic.",
        ("Review this English fiction chapter. List its REAL flaws:\n"
         "1. Logic / consistency errors\n"
         "2. AI tells (template character names, clichéd sensory detail, formulaic emotions)\n"
         "3. Unnatural collocations\n\n"
         "Then output a section headed 'RULES:' with one concrete writing rule per line, "
         "each starting with '- '. Keep rules imperative, specific, and directly fix the flaws.\n\n"
         "Chapter:\n---\n" + text + "\n---")
    )


def blind_round(gemini, text):
    """盲评：判断 AI/真人 + 地道性评分（严格格式）。"""
    return gemini._call(
        "You are a literary critic. Judge objectively and reply in a strict format.",
        ("Read this English fiction chapter. Reply EXACTLY in this format, nothing else:\n"
         "verdict: ai|human\n"
         "naturalness: 1-5\n\n"
         "Chapter:\n---\n" + text + "\n---")
    )


def parse_verdict(content):
    m = re.search(r"verdict\s*:\s*(ai|human)", content, re.I)
    s = re.search(r"naturalness\s*:\s*([1-5])", content)
    return (m.group(1).lower() if m else "ai"), (int(s.group(1)) if s else 3)


def extract_rules(content):
    """只提取 'RULES:' 之后的 '- ' 行。"""
    rules = []
    in_rules = False
    for line in content.splitlines():
        s = line.strip()
        if re.search(r"^\s*RULES\s*[:：]", line, re.I):
            in_rules = True
            continue
        if in_rules and s.startswith("-"):
            rule = s.lstrip("- ").strip()
            if len(rule) > 6:
                rules.append(rule)
        elif in_rules and s and not s.startswith("-"):
            break
    return rules


def main():
    env = load_env()
    gemini = GeminiReviewer(env)
    if not gemini.key:
        print("ERROR: 缺 GEMINI_API_KEY")
        return

    engine = WritingEngine()
    engine.set_genre(DIRECTION, None)
    out_dir = os.path.join(HERE, "shorts", STORY_ID)
    os.makedirs(out_dir, exist_ok=True)

    print("[init] generating outline ...")
    outline = engine.write_outline(DIRECTION)["content"]
    print("[init] generating world bible ...")
    bible_r = engine.write_world_bible(DIRECTION, outline)
    bible = bible_r["content"] if "error" not in bible_r else ""

    extra_rules = []
    results = []

    for i in range(1, MAX_ITER + 1):
        print(f"\n===== 迭代 {i}/{MAX_ITER} =====")
        rules_text = "\n".join(extra_rules)

        r = engine.write_with_retry(DIRECTION, outline, bible, 1, 12, TARGET_WORDS, rules_text)
        if "error" in r:
            print(f"  [write] ERROR: {r['error']}")
            continue
        text = r["text"]

        rev = review_round(gemini, text)
        rev_content = rev.get("content", "")
        issues = extract_rules(rev_content)
        print(f"  [review] {len(issues)} 条规则")

        blind = blind_round(gemini, text)
        verdict, score = parse_verdict(blind.get("content", ""))
        print(f"  [blind] verdict={verdict} naturalness={score}")

        results.append({"iter": i, "verdict": verdict, "naturalness": score, "rules": len(issues)})
        open(os.path.join(out_dir, f"iter{i:02d}_{verdict}.md"), "w", encoding="utf-8").write(
            f"# 迭代 {i}\nverdict={verdict} naturalness={score}\n\n## 规则\n" +
            "\n".join(issues) + "\n\n## 正文\n" + text)

        if verdict == "human" and score >= 4:
            print(f"\n[达标] 迭代 {i}：盲评误判 human 且地道性 {score}>=4")
            break

        for rule in issues:
            if rule not in extra_rules and len(extra_rules) < 40:
                extra_rules.append(rule)
        print(f"  [rules] 累积 {len(extra_rules)} 条")

    print("\n===== 迭代汇总 =====")
    for x in results:
        print(f"  迭代 {x['iter']}: {x['verdict']} (地道 {x['naturalness']}) +{x['rules']}规则")
    json.dump(results, open(os.path.join(out_dir, "iterate_result.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print(f"结果已存: {out_dir}")


if __name__ == "__main__":
    main()
