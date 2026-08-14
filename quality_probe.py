# -*- coding: utf-8 -*-
"""
quality_probe.py — 质量探针（P0 第一道门）

对比「InkOS 基线产出」vs「自研写作引擎产出」，用可量化指标判断：
  1. 字数符合度（600-800 达标率）
  2. AI 味密度（复用 ai_tells_en.json）
  3. em-dash 密度
  4. 可读性（简化 Flesch-Kincaid：平均句长 / 平均词长 / 音节近似）
  5. native-check 评分（可选，需 --native 触发，调 reasoning 模型）

用法:
    py -3 quality_probe.py --inkos-baseline C:\\Users\\唐杰\\Desktop\\OH-WorkSpace\\inkos-test\\shorts\\the-ledger-of-grief\\final\\short-story.json --mine shorts\\probe_test

输出: probe/report.md 对比报告
"""
import argparse
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))


def count_words(t):
    return len(re.findall(r"[A-Za-z']+", t))


def count_sentences(t):
    return max(len(re.findall(r"[.!?]+(?:\s|$)", t)), 1)


def count_syllables(word):
    """近似音节计数（元音组法，够做相对对比）。"""
    word = word.lower().strip("'")
    if not word:
        return 0
    vowels = "aeiouy"
    n = 0
    prev = False
    for ch in word:
        is_v = ch in vowels
        if is_v and not prev:
            n += 1
        prev = is_v
    # 词尾 e 通常不发音
    if word.endswith("e") and len(word) > 2:
        n -= 1
    return max(n, 1)


def flesch_reading_ease(text):
    words = count_words(text)
    sentences = count_sentences(text)
    syllables = sum(count_syllables(w) for w in re.findall(r"[A-Za-z']+", text))
    if words == 0 or sentences == 0:
        return 0
    return 206.835 - 1.015 * (words / sentences) - 84.6 * (syllables / words)


def ai_tell_density(text, tells):
    words = max(count_words(text), 1)
    k = words / 1000.0
    hits = {}
    all_terms = tells.get("phrases", []) + tells.get("verbs", []) + tells.get("expressions", [])
    for term in all_terms:
        n = len(re.findall(re.escape(term), text, re.I))
        if n > 0:
            hits[term] = n
    total = sum(hits.values())
    return total / k, hits


def em_dash_rate(text):
    words = max(count_words(text), 1)
    n = text.count("\u2014") + text.count("--")
    return n / (words / 1000.0)


def analyze_chapters(chapters, tells):
    """chapters: list of (name, text)"""
    metrics = {
        "n_chapters": len(chapters),
        "total_words": 0,
        "words_in_range": 0,
        "word_list": [],
        "ai_tell_total": 0.0,
        "ai_hits": {},
        "em_rate": 0.0,
        "fk_ease": 0.0,
    }
    for name, text in chapters:
        wc = count_words(text)
        metrics["total_words"] += wc
        metrics["word_list"].append(wc)
        if 600 <= wc <= 800:
            metrics["words_in_range"] += 1
        dens, hits = ai_tell_density(text, tells)
        metrics["ai_tell_total"] += dens
        for k, v in hits.items():
            metrics["ai_hits"][k] = metrics["ai_hits"].get(k, 0) + v
        metrics["em_rate"] += em_dash_rate(text)
        metrics["fk_ease"] += flesch_reading_ease(text)
    n = metrics["n_chapters"]
    metrics["compliance"] = round(metrics["words_in_range"] / n * 100, 1) if n else 0
    metrics["avg_words"] = round(metrics["total_words"] / n) if n else 0
    metrics["avg_ai_density"] = round(metrics["ai_tell_total"] / n, 2) if n else 0
    metrics["avg_em_rate"] = round(metrics["em_rate"] / n, 1) if n else 0
    metrics["avg_fk"] = round(metrics["fk_ease"] / n, 1) if n else 0
    return metrics


def load_inkos_baseline(path):
    """从 InkOS short-story.json 提取章节。"""
    data = json.load(open(path, encoding="utf-8"))
    return [(f"ch{c['number']}", c["content"]) for c in data.get("chapters", [])]


def load_mine(short_dir):
    """从自研引擎 shorts/<id>/ 提取章节。"""
    chapters = []
    for fn in sorted(os.listdir(short_dir)):
        if re.match(r"ch\d+\.md", fn):
            chapters.append((fn, open(os.path.join(short_dir, fn), encoding="utf-8").read()))
    return chapters


def main():
    ap = argparse.ArgumentParser(description="质量探针：InkOS 基线 vs 自研引擎")
    ap.add_argument("--inkos-baseline", help="InkOS short-story.json 路径")
    ap.add_argument("--mine", required=True, help="自研引擎 shorts/<id> 目录")
    args = ap.parse_args()

    tells = json.load(open(os.path.join(HERE, "ai_tells_en.json"), encoding="utf-8"))

    rows = {}
    mine = load_mine(args.mine)
    rows["自研引擎 (writing-engine)"] = analyze_chapters(mine, tells)
    if args.inkos_baseline and os.path.exists(args.inkos_baseline):
        inkos = load_inkos_baseline(args.inkos_baseline)
        rows["InkOS 基线 (the-ledger-of-grief)"] = analyze_chapters(inkos, tells)

    # 报告
    lines = ["# 质量探针报告", ""]
    for label, m in rows.items():
        lines += [
            f"## {label}",
            f"- 章数: {m['n_chapters']}",
            f"- 平均词数: {m['avg_words']} (目标 600-800)",
            f"- 字数达标率: {m['compliance']}%",
            f"- AI 味密度: {m['avg_ai_density']}/千词 (越低越好)",
            f"- em-dash 密度: {m['avg_em_rate']}/千词",
            f"- Flesch 易读性: {m['avg_fk']} (60-70 为标准英文小说区间)",
            f"- AI 味词命中: {m['ai_hits'] or '无'}",
            "",
        ]
    out = os.path.join(HERE, "probe", "report.md")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    open(out, "w", encoding="utf-8").write("\n".join(lines))
    print("\n".join(lines))
    print(f"[saved] {out}")


if __name__ == "__main__":
    main()
