# -*- coding: utf-8 -*-
"""
radar.py — 英文选品雷达（M2 增强版）

流程：抓 RoyalRoad 榜单 → 题材粗分类 → 热点归档 trends 表
     → LLM 一次调用做「题材分布统计(空位量化) + 推荐(失败题材降权)」
     → 相似度去重(24h) → 推荐入库 recommendations 表

用法:
    py -3 radar.py                     # 完整流程
    py -3 radar.py --no-llm            # 只抓榜+归档，不调 LLM
    py -3 radar.py --json              # 结果打 stdout
"""
import argparse
import json
import os
import re
import time
import urllib.request
from datetime import datetime, timezone

from engine import LLM, load_json
from state_store import StateStore, today

HERE = os.path.dirname(os.path.abspath(__file__))

RR_LISTS = {
    "best-rated": "https://www.royalroad.com/fictions/best-rated",
    "complete": "https://www.royalroad.com/fictions/complete",
    "trending": "https://www.royalroad.com/fictions/trending",
}
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# 题材粗分类关键词（用于 trends 归档；精确统计交给 LLM）
GENRE_KEYWORDS = [
    ("litrpg", ["litrpg", "rpg", "level", "stats", "system", "gamer", "dungeon"]),
    ("timeloop", ["loop", "time travel", "rewind", "repeat", "time loop"]),
    ("fantasy", ["fantasy", "magic", "mage", "wizard", "dragon", "elf", "kingdom", "witch", "sword"]),
    ("romance", ["romance", "love", "engagement", "marriage", "betrothed", "villainess", "heart"]),
    ("horror", ["horror", "haunted", "ghost", "eldritch", "monster", "dark fantasy"]),
    ("scifi", ["scifi", "sci-fi", "cyberpunk", "space", "star wars", "warhammer", "mech", "ai"]),
    ("progression", ["progression", "cultivation", "xianxia", "immortal", "ascend"]),
    ("cozy", ["cozy", "slice of life", "tea", "bakery", "shop", "chicken"]),
]


def fetch(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "ignore")


def parse_rr(html, limit):
    """提取书名 + id + slug + 粗分类。"""
    entries = []
    h2_pat = re.compile(r'<h2[^>]*>\s*<a href="/fiction/(\d+)/([^"]+)"[^>]*>(.*?)</a>\s*</h2>', re.S)
    for m in h2_pat.finditer(html):
        fid, slug, raw_title = m.group(1), m.group(2), m.group(3)
        title = re.sub(r"\s+", " ", raw_title).strip()
        title = re.sub(r"&#x27;|&amp;", "'", title)
        entries.append({"title": title, "id": fid, "slug": slug})
        if len(entries) >= limit:
            break
    return entries


def classify(title):
    """粗分类：按关键词映射给书名打题材标签。"""
    tl = title.lower()
    for genre, kws in GENRE_KEYWORDS:
        if any(kw in tl for kw in kws):
            return genre
    return "other"


def llm_analyze(entries, validated_failures, llm):
    """一次 LLM 调用：题材分布统计（硬数字）+ 空位 + 推荐（失败题材降权）。"""
    titles = "\n".join(f"- {e['title']}" for e in entries[:40])
    fail_block = ""
    if validated_failures:
        fail_block = "\nIMPORTANT: these titles/genres were validated as failures before, deprioritize or avoid them:\n" + \
            "\n".join(f"- {t} ({c or 'unknown'})" for t, c in validated_failures[:10])

    sys_msg = (
        "You are a market analyst for English web fiction sold on RoyalRoad and Amazon KDP. "
        "You know what sells, what is saturated, and where the gaps are."
    )
    user = (
        "Here is the current RoyalRoad best-rated list:\n\n" + titles + "\n\n"
        "Step 1: classify these titles into genres and give the DISTRIBUTION "
        "(count + percentage per genre). This is hard data, be precise.\n"
        "Step 2: identify the GAPS: genres with reader demand but few competing titles.\n"
        "Step 3: output 3-5 original short-fiction concepts (US-market English, 12-18 chapters).\n"
        + fail_block + "\n\n"
        "Output STRICT JSON only:\n"
        '{"genreDistribution": {"litrpg": 8, "fantasy": 5}, "marketSummary": "...", '
        '"recommendations": [{"genre": "...", "concept": "...", "confidence": 0.9, '
        '"reasoning": "...", "benchmarkTitles": ["..."]}]}'
    )
    r = llm.chat("radar", [
        {"role": "system", "content": sys_msg},
        {"role": "user", "content": user},
    ], max_tokens=10000, temperature=0.5)
    return r


def parse_llm_json(content):
    """容错解析 LLM 返回的 JSON。"""
    if not content:
        return {}
    c = re.sub(r"^```(?:json)?\s*", "", content.strip())
    c = re.sub(r"\s*```$", "", c)
    try:
        return json.loads(c)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", c, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
        return {"raw": c[:500]}


def dedup(recommendations, store):
    """相似度去重：genre 归一化 + 查 24h 内已推荐的 genre。"""
    recent = store.recent_recommendations(24)
    recent_genres = {normalize_genre(r["genre"]) for r in recent if r.get("genre")}
    kept, dropped = [], []
    for rec in recommendations:
        g = normalize_genre(rec.get("genre", ""))
        if g in recent_genres:
            dropped.append(rec.get("genre", ""))
        else:
            recent_genres.add(g)
            kept.append(rec)
    return kept, dropped


def normalize_genre(genre):
    """标签归一化：litrpg/LitRPG/Progression Fantasy → litrpg/progression。"""
    g = genre.lower().strip()
    mapping = {
        "progression fantasy": "progression",
        "lit rpg": "litrpg",
        "dark fantasy": "fantasy",
        "urban fantasy": "fantasy",
        "cozy fantasy": "cozy",
        "romantasy": "romance",
        "sci-fi": "scifi",
        "science fiction": "scifi",
    }
    return mapping.get(g, g)


def main():
    ap = argparse.ArgumentParser(description="英文选品雷达 M2")
    ap.add_argument("--list", type=int, default=20, help="每榜抓取数")
    ap.add_argument("--lists", default="best-rated,complete,trending")
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    cfg = load_json(os.path.join(HERE, "config.json"))
    store = StateStore()

    scan = {"timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": "royalroad", "entries": {}, "genreDistribution": {},
            "recommendations": [], "dropped": []}

    # 1. 抓榜 + 粗分类 + 归档
    all_entries = []
    for key in [k for k in args.lists.split(",") if k in RR_LISTS]:
        try:
            html = fetch(RR_LISTS[key])
            entries = parse_rr(html, args.list)
            for e in entries:
                e["category"] = classify(e["title"])
            store.archive_trends(key, entries)
            scan["entries"][key] = entries
            all_entries += entries
            print(f"[ok] {key}: {len(entries)} titles archived")
        except Exception as e:
            print(f"[fail] {key}: {e}")
            scan["entries"][key] = []

    # 2. LLM 分析（统计 + 推荐 + 失败降权）
    if not args.no_llm:
        llm = LLM(cfg)
        validated = store.validated_failures()
        print(f"[info] validated failures to deprioritize: {len(validated)}")
        r = llm_analyze(all_entries, validated, llm)
        if "error" in r:
            print(f"[fail] llm analyze: {r['error']}")
        else:
            result = parse_llm_json(r["content"])
            scan["genreDistribution"] = result.get("genreDistribution", {})
            scan["marketSummary"] = result.get("marketSummary", "")
            recs = result.get("recommendations", [])
            print(f"[ok] llm recommendations: {len(recs)}, distribution: {scan['genreDistribution']}")
            # 3. 去重
            kept, dropped = dedup(recs, store)
            scan["dropped"] = dropped
            # 4. 入库
            for rec in kept:
                rec["scan_file"] = f"radar_en/scan-{today()}.json"
                store.add_recommendation(rec)
            scan["recommendations"] = kept
            print(f"[ok] after dedup: {len(kept)} kept, dropped: {dropped or 'none'}")

    # 保存
    out_dir = os.path.join(HERE, "radar_en")
    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    path = os.path.join(out_dir, f"scan-{ts}.json")
    json.dump(scan, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"[saved] {path}")

    if args.json:
        print(json.dumps(scan, ensure_ascii=False, indent=2))

    store.close()


if __name__ == "__main__":
    main()
