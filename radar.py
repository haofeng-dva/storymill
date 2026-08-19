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


# ===== 第一章爬取（限流保护）=====
CHAP_LIMIT = 8        # 最多抓多少本的第一章（防止被限流）
CHAP_DELAY = 2.5      # 每本间隔秒数（伪装正常浏览）
CHAP_TIMEOUT = 25     # 单次请求超时


def fetch_chapter(url, timeout=CHAP_TIMEOUT):
    """抓单个页面，失败返回 None（不抛异常，方便跳过）。"""
    try:
        return fetch(url, timeout)
    except Exception:
        return None


def get_first_chapter(entry):
    """抓一本书的第一章正文。返回 (chapter_text, chapter_url) 或 (None, None)。"""
    # 1. 详情页
    detail = fetch_chapter(f"https://www.royalroad.com/fiction/{entry['id']}/{entry['slug']}")
    if not detail:
        return None, None
    # 2. 详情页里找第一章链接
    ch_links = re.findall(r'href="(/fiction/\d+/[^"]*/chapter/[^"]*)"', detail)
    if not ch_links:
        return None, None
    # 3. 抓第一章正文
    ch_page = fetch_chapter("https://www.royalroad.com" + ch_links[0])
    if not ch_page:
        return None, None
    m = re.search(r'class="[^"]*chapter-content[^"]*"[^>]*>(.*?)</div>', ch_page, re.S)
    if not m:
        return None, None
    txt = re.sub(r"<[^>]+>", " ", m.group(1))
    txt = re.sub(r"\s+", " ", txt).strip()
    return (txt[:2000], ch_links[0]) if len(txt) > 50 else (None, None)


def crawl_first_chapters(entries):
    """带限流保护地抓前 CHAP_LIMIT 本的第一章，返回 {title: {chapter, url}}。"""
    import time
    result = {}
    for e in entries[:CHAP_LIMIT]:
        txt, url = get_first_chapter(e)
        if txt:
            result[e["title"]] = {"chapter": txt, "url": url, "id": e["id"]}
            print(f"    [chapter] {e['title'][:30]}: 第一章已抓 ({len(txt)} 字)", flush=True)
        else:
            print(f"    [chapter] {e['title'][:30]}: 跳过（详情页/正文未拿到）", flush=True)
        time.sleep(CHAP_DELAY)   # 节流：每本间隔 2.5 秒，伪装正常浏览
    return result


def llm_tag_terms(chapters, llm):
    """把抓到的第一章喂给 LLM，输出题材特征词条（供分类/推荐参考）。"""
    if not chapters:
        return {}
    sample = "\n\n".join(f"[{t}]\n{c['chapter'][:500]}" for t, c in list(chapters.items())[:6])
    r = llm.chat("radar", [
        {"role": "system", "content": "You are a genre analyst for web fiction. For each opening chapter, extract the genre traits."},
        {"role": "user", "content": (
            "Below are opening chapters. For EACH, output STRICT JSON: "
            '{"<title>": {"genre": "...", "traits": ["..."], "hook": "..."}}}\n\n' + sample
        )},
    ], max_tokens=4000, temperature=0.3)
    if "error" in r:
        return {}
    return parse_llm_json(r["content"])


def parse_rr(html, limit):
    """提取书名 + id + slug + 粗分类 + 简介（简介在书名后的一段文本里）。"""
    entries = []
    h2_pat = re.compile(r'<h2[^>]*>\s*<a href="/fiction/(\d+)/([^"]+)"[^>]*>(.*?)</a>\s*</h2>', re.S)
    for m in h2_pat.finditer(html):
        fid, slug, raw_title = m.group(1), m.group(2), m.group(3)
        title = re.sub(r"\s+", " ", raw_title).strip()
        title = re.sub(r"&#x27;|&amp;", "'", title)
        desc = extract_description(html, m.start())
        tags = extract_tags(html, m.start())
        entries.append({"title": title, "id": fid, "slug": slug, "description": desc, "tags": tags})
        if len(entries) >= limit:
            break
    return entries


def extract_tags(html, start_idx):
    """从书名位置往后抠题材标签（fiction-tag 元素），如 Time Loop / Fantasy。"""
    seg = html[start_idx:start_idx + 4000]
    tags = re.findall(r'class="[^"]*fiction-tag[^"]*"[^>]*>(.*?)</a>', seg, re.S)
    return [re.sub(r"<[^>]+>", "", t).strip() for t in tags if t.strip()]


def extract_description(html, start_idx):
    """从书名位置往后挖简介：去标签 → 跳过统计信息 → 取第一段像简介的长文本。"""
    for span in (6000, 10000, 14000):
        desc = _try_extract(html, start_idx, span)
        if desc:
            return desc
    return ""


def _try_extract(html, start_idx, span):
    seg = html[start_idx:start_idx + span]
    seg = re.sub(r"<script.*?</script>", " ", seg, flags=re.S)
    seg = re.sub(r"<style.*?</style>", " ", seg, flags=re.S)
    clean = re.sub(r"<[^>]+>", " ", seg)
    clean = re.sub(r"\s+", " ", clean).strip()
    skip_kw = ("followers", "chapters", "views", "pages", "original", "ongoing",
               "completed", "amazon", "audible", "audiobook", "cover by",
               "royal road", "home of web novels", "star-")
    parts = re.split(r"(?<=[.])\s", clean)
    buf = ""
    for part in parts:
        buf += part + " "
        if len(buf) > 60:
            low = buf.lower()
            if not any(k in low for k in skip_kw) and "www.royalroadcdn" not in low:
                return buf.strip()[:400]
            buf = ""
    return ""


def classify(title):
    """粗分类：按关键词映射给书名打题材标签。"""
    tl = title.lower()
    for genre, kws in GENRE_KEYWORDS:
        if any(kw in tl for kw in kws):
            return genre
    return "other"


def llm_analyze(entries, validated_failures, llm):
    """一次 LLM 调用：题材分布统计（硬数字）+ 空位 + 推荐（失败题材降权）。"""
    titles = "\n".join(
        f"- {e['title']} :: {e.get('description', '')[:120]}" for e in entries[:40]
    )
    fail_block = ""
    if validated_failures:
        fail_block = "\nIMPORTANT: these titles/genres were validated as failures before, deprioritize or avoid them:\n" + \
            "\n".join(f"- {t} ({c or 'unknown'})" for t, c in validated_failures[:10])

    sys_msg = (
        "You are a market analyst for English web fiction sold on RoyalRoad and Amazon KDP. "
        "You know what sells, what is saturated, and where the gaps are."
    )
    user = (
        "Here is the current RoyalRoad best-rated list (title :: short synopsis):\n\n" + titles + "\n\n"
        "Step 1: classify these titles into genres and give the DISTRIBUTION "
        "(count + percentage per genre). This is hard data, be precise. "
        "Use the synopsis to judge the genre, not just the title.\n"
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
    ap.add_argument("--no-chapters", action="store_true", help="跳过第一章爬取")
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

    # 1.5 第一章爬取（限流保护：前 8 本，每本间隔 2.5 秒）
    scan["chapters"] = {}
    if not args.no_chapters:
        print(f"[chapters] 抓前 {CHAP_LIMIT} 本第一章（节流 {CHAP_DELAY}s/本）...")
        chapters = crawl_first_chapters(all_entries)
        scan["chapters"] = chapters
        # 第一章解读成题材特征词条（存快照，供后续选品/写作参考）
        if chapters and not args.no_llm:
            llm = LLM(cfg)
            terms = llm_tag_terms(chapters, llm)
            scan["chapterTerms"] = terms
            print(f"[chapters] 解读词条: {list(terms.keys()) or '无'}")

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
