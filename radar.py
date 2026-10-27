# -*- coding: utf-8 -*-
"""
radar.py — 英文选品雷达（M2 增强版）

流程：抓多来源榜单 → 题材分类 → 热点归档 trends 表
     → 聚合跨平台信号 → LLM 做「分布统计 + 推荐(失败题材降权)」
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
import sys
import subprocess
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
# 公开页面可按需启用。站点 HTML 经常变化，因此每个来源都必须独立失败、不能阻塞其它来源。
# 默认面向女性向/IP 读者池；Royal Road 仅通过 --sources 显式加入。
SOURCE_CONFIG = {
    "royalroad": {"weight": 0.20, "lists": RR_LISTS},
    "wattpad": {"weight": 0.25, "lists": {
        "werewolf": "https://www.wattpad.com/stories/werewolf",
        "romance": "https://www.wattpad.com/stories/romance",
    }},
    "inkitt": {"weight": 0.20, "lists": {
        "werewolf": "https://www.inkitt.com/topics/werewolf",
        "romance": "https://www.inkitt.com/topics/romance",
    }},
    "inkitt_browser": {"weight": 0.20, "lists": {}},
    "inkitt_browser_validation": {"weight": 0.12, "lists": {}},
    "webnovel": {"weight": 0.15, "lists": {
        "romance": "https://www.webnovel.com/stories/romance",
    }},
    "amazon": {"weight": 0.20, "lists": {
        "romance": "https://www.amazon.com/Best-Sellers-Kindle-Store-Romance/zgbs/digital-text/158566011",
    }},
}
# 研究矩阵：不是把平台混成一个榜单，而是按读者池和商业语义解释信号。
PLATFORM_PROFILES = {
    "royalroad": {"audience": "progression/fantasy web serials", "signal": "best-rated, trending, complete",
                   "commercial": "subscription/serial readership", "explicitness": "low-to-moderate; platform policy applies"},
    "wattpad": {"audience": "mobile-first YA and romance discovery", "signal": "topic/story discovery pages",
                 "commercial": "reading velocity, follows, adaptations", "explicitness": "age-rating and content policy sensitive"},
    "inkitt": {"audience": "romance-focused discovery and publishing", "signal": "topic pages and reader engagement",
               "commercial": "reader engagement and publishing acquisition", "explicitness": "adult content must be labeled and policy-compliant"},
    "webnovel": {"audience": "serialized romance/fantasy and paid chapters", "signal": "genre story lists and rankings",
                 "commercial": "chapter monetization and retention", "explicitness": "market and platform policy dependent"},
    "amazon": {"audience": "retail ebook buyers across genre niches", "signal": "Kindle category bestseller ranks",
                "commercial": "sales velocity, KU page reads, reviews", "explicitness": "KDP content and cover/metadata rules apply"},
}
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# 题材粗分类关键词（用于 trends 归档；精确统计交给 LLM）
GENRE_KEYWORDS = [
    ("werewolf_shifter", ["werewolf", "wolf shifter", "shifter", "alpha", "luna", "pack"]),
    ("dark_romance", ["dark romance", "possessive", "mafia", "obsessive", "dub-con"]),
    ("romantasy", ["romantasy", "fae", "mate bond", "dragon rider"]),
    ("litrpg", ["litrpg", "rpg", "level", "stats", "system", "gamer", "dungeon"]),
    ("timeloop", ["loop", "time travel", "rewind", "repeat", "time loop"]),
    ("fantasy", ["fantasy", "magic", "mage", "wizard", "dragon", "elf", "kingdom", "witch", "sword"]),
    ("romance", ["romance", "love", "engagement", "marriage", "betrothed", "villainess", "heart"]),
    ("horror", ["horror", "haunted", "ghost", "eldritch", "monster", "dark fantasy"]),
    ("scifi", ["scifi", "sci-fi", "cyberpunk", "space", "star wars", "warhammer", "mech", "ai"]),
    ("progression", ["progression", "cultivation", "xianxia", "immortal", "ascend"]),
    ("cozy", ["cozy", "slice of life", "tea", "bakery", "shop", "chicken"]),
]

# Inkitt Romance 页面的小类标签；作为独立信号传给模型，不替代主题材分类。
SUBGENRE_KEYWORDS = {
    "contemporary": ("contemporary", "small town", "workplace"),
    "enemies_to_lovers": ("enemies to lovers", "rivals", "hate to love"),
    "dark": ("dark romance", "dark", "possessive", "obsessive", "dub-con", "captor", "captive"),
    "romantic": ("romantic", "romance", "love", "relationship"),
    "erotic": ("erotic", "explicit", "steamy", "spicy", "sex", "sensual", "adult"),
    "billionaire": ("billionaire", "millionaire", "tycoon", "ceo"),
    "fantasy_romantasy": ("fantasy", "romantasy", "fae", "dragon", "witch", "magic"),
    "new_adult": ("new adult", "college", "campus", "scholarship student"),
    "mafia": ("mafia", "bratva", "crime lord", "mob boss"),
    "second_chance": ("second chance", "ex-husband", "ex boyfriend", "reconnect", "old feelings"),
    "lgbtq": ("lgbtq", "lgbt", "queer", "lesbian", "gay", "bisexual", "transgender", "same-sex"),
    # Inkitt Horror 小类；可与 Romance、LGBTQ+、Werewolf 等母类型交叉组合。
    "horror_supernatural": ("supernatural", "paranormal", "ghost", "haunting"),
    "horror_psychological": ("psychological horror", "psychological thriller", "mind game", "gaslighting"),
    "horror_dark": ("dark horror", "dark"),
    "horror_dark_fantasy": ("dark fantasy", "grimdark"),
    "horror_murder": ("murder mystery", "murder", "murderer"),
    "horror_gothic": ("gothic", "haunted mansion", "decaying estate"),
    "horror_serial_killer": ("serial killer", "serial murder"),
    "horror_zombie": ("zombie", "undead apocalypse"),
    "horror_occult": ("occult", "demon", "ritual", "possession", "exorcism"),
    "horror_body": ("body horror", "mutation", "flesh", "deformity"),
    "horror_eco": ("eco-horror", "environmental horror", "nature takes revenge"),
    "horror_cosmic": ("cosmic horror", "lovecraftian", "eldritch", "ancient one"),
    "horror_folk": ("folk horror", "folk tale", "coven tradition", "village ritual"),
    "horror_comedy": ("horror comedy", "comedy horror"),
    "horror_slasher": ("slasher", "masked killer"),
    "horror_found_footage": ("found footage", "recovered tape", "recorded evidence"),
    "horror_splatterpunk": ("splatterpunk", "extreme horror", "visceral gore"),
}


def fetch(url, timeout=25):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "ignore")


def fetch_inkitt(url, timeout=25):
    """Inkitt 专用只读请求。可使用用户自己的 INKITT_COOKIE，会话缺失时明确返回 403。"""
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.inkitt.com/",
        "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Site": "same-origin",
    }
    cookie = os.environ.get("INKITT_COOKIE", "").strip()
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            html = resp.read().decode("utf-8", "ignore")
            low = html.lower()
            if "challenges.cloudflare.com/turnstile" in low or "cloudflareturnstilesitekey" in low:
                raise RuntimeError("inkitt_challenge: Cloudflare Turnstile challenge returned; browser/session access required")
            return html
    except urllib.error.HTTPError as e:
        if e.code == 403:
            raise RuntimeError("inkitt_forbidden: site requires browser/session access (set INKITT_COOKIE only with your own authorized session)")
        raise


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
    # 只有 RoyalRoad 解析结果包含稳定的 fiction id/slug；其它来源只参与榜单信号。
    chapter_entries = [e for e in entries if e.get("id") and e.get("slug")]
    for e in chapter_entries[:CHAP_LIMIT]:
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
        entries.append({"title": title, "id": fid, "slug": slug, "description": desc, "tags": tags,
                        "rank": len(entries) + 1})
        if len(entries) >= limit:
            break
    return entries


def parse_generic(html, limit):
    """保守的通用解析器：适用于公开榜单页，抓不到时返回空而不是伪造数据。"""
    entries = []
    seen = set()
    # 多数站点榜单至少会输出标题链接；简介和标签缺失时保留空值。
    for m in re.finditer(r'<a[^>]+href="[^"]+"[^>]*>(.*?)</a>', html, re.S | re.I):
        title = re.sub(r"<[^>]+>", " ", m.group(1))
        title = re.sub(r"\s+", " ", title).strip()
        if len(title) < 3 or len(title) > 180 or title.lower() in seen:
            continue
        low = title.lower()
        if any(x in low for x in ("sign in", "login", "home", "category", "browse", "follow", "terms of service", "privacy policy", "main content", "search alt")):
            continue
        seen.add(low)
        entries.append({"title": title, "description": "", "tags": [], "rank": len(entries) + 1})
        if len(entries) >= limit:
            break
    return entries


def parse_wattpad(html, limit):
    """Wattpad 主题页提供稳定的 schema.org ItemList，优先读取其书名和简介。"""
    entries = []
    for raw in re.findall(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S | re.I):
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            continue
        for item in data.get("itemListElement", []) if isinstance(data, dict) else []:
            if not isinstance(item, dict):
                continue
            title = str(item.get("name", "")).strip()
            if len(title) < 3:
                continue
            entries.append({"title": title, "description": str(item.get("description", ""))[:500],
                            "tags": [], "rank": item.get("position") or len(entries) + 1})
            if len(entries) >= limit:
                return entries
    return entries or parse_generic(html, limit)


def parse_inkitt(html, limit):
    """Inkitt 公开页若返回 JSON-LD，读取 ItemList；否则使用保守链接解析。"""
    entries = []
    for raw in re.findall(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S | re.I):
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            continue
        items = data.get("itemListElement", []) if isinstance(data, dict) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            title = str(item.get("name", "")).strip()
            if len(title) < 3:
                continue
            entries.append({"title": title, "description": str(item.get("description", ""))[:500],
                            "tags": [], "rank": item.get("position") or len(entries) + 1})
            if len(entries) >= limit:
                return entries
    return entries or parse_generic(html, limit)


def parse_amazon(html, limit):
    """Amazon bestseller 页面没有统一 JSON-LD，抓商品标题并过滤导航文本。"""
    entries = []
    # 商品卡的封面 alt 是书名，优先于动态 CSS 文本，避免把作者名当书名。
    card_pat = r'p13n-sc-uncoverable-faceout.*?<img[^>]+alt="([^"]+)"'
    for raw in re.findall(card_pat, html, re.S | re.I):
        title = re.sub(r"\s+", " ", raw).strip()
        if len(title) >= 8:
            entries.append({"title": title, "description": "", "tags": [], "rank": len(entries) + 1})
            if len(entries) >= limit:
                return entries
    patterns = [
        r'class="p13n-sc-truncate[^>]*"[^>]*>(.*?)</div>',
        r'class="[^"]*p13n-sc-css-line-clamp-1[^"]*"[^>]*>(.*?)</div>',
        r'<span[^>]+class="[^\"]*a-size-small[^\"]*[^\"]*a-text-normal[^\"]*"[^>]*>(.*?)</span>',
    ]
    seen = set()
    for pat in patterns:
        for raw in re.findall(pat, html, re.S | re.I):
            title = re.sub(r"<[^>]+>", " ", raw)
            title = re.sub(r"\s+", " ", title).strip()
            low = title.lower()
            if len(title) < 8 or len(title) > 220 or low in seen:
                continue
            if any(x in low for x in ("sign in", "search", "main content", "buying choices", "see all")):
                continue
            seen.add(low)
            entries.append({"title": title, "description": "", "tags": [], "rank": len(entries) + 1})
            if len(entries) >= limit:
                return entries
    return entries or parse_generic(html, limit)


def parse_source(source, html, limit):
    if source == "royalroad":
        return parse_rr(html, limit)
    if source == "wattpad":
        return parse_wattpad(html, limit)
    if source == "amazon":
        return parse_amazon(html, limit)
    if source == "inkitt":
        return parse_inkitt(html, limit)
    return parse_generic(html, limit)


def load_inkitt_browser(limit, required_scope="broad_discovery"):
    path = os.path.join(HERE, "radar_en", "inkitt_browser.json")
    if not os.path.exists(path):
        raise RuntimeError("inkitt_browser_bridge_missing: start inkitt_bridge.py and send a page from the extension")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    page_url = data.get("url", "")
    entries = data.get("entries", [])
    if not isinstance(entries, list):
        raise RuntimeError("inkitt_browser_payload_invalid")
    # 防止旧版扩展把评分/导航文本当成书名，服务端再做一次硬过滤。
    entries = [e for e in entries if isinstance(e, dict) and
               len(str(e.get("title", "")).strip()) >= 3 and
               not re.fullmatch(r"\d+(\.\d+)?", str(e.get("title", "")).strip())]
    if required_scope == "broad_discovery" and inkitt_page_scope(page_url) != "broad_discovery":
        # Current browser may be left on a niche page. Reuse the newest archived broad page instead of biasing the main radar.
        out_dir = os.path.join(HERE, "radar_en")
        candidates = sorted(
            (os.path.join(out_dir, n) for n in os.listdir(out_dir) if n.startswith("scan-") and n.endswith(".json")),
            key=os.path.getmtime, reverse=True,
        )
        for candidate in candidates:
            try:
                old = load_json(candidate)
                rows = old.get("entries", {}).get("inkitt_browser:page", [])
                old_url = rows[0].get("list", "") if rows else ""
                if rows and inkitt_page_scope(old_url) == "broad_discovery":
                    return rows[:limit], old_url
            except Exception:
                continue
        raise RuntimeError("inkitt_broad_page_missing: open Inkitt homepage/discovery page and refresh once")
    return entries[:limit], page_url


def load_inkitt_validation(limit):
    """读取桥接归档的 Inkitt 分类页；分类信号独立于首页大盘。"""
    path = os.path.join(HERE, "radar_en", "inkitt_browser_pages.json")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        pages = json.load(f)
    result = []
    for page in pages if isinstance(pages, list) else []:
        scope = page.get("pageScope") or inkitt_validation_scope(page.get("url", ""))
        if scope in ("broad_discovery", "horror_validation") or not isinstance(page.get("entries"), list):
            continue
        for item in page["entries"][:limit]:
            if not isinstance(item, dict):
                continue
            row = dict(item)
            row["source"] = "inkitt_browser_validation"
            row["list"] = page.get("url", scope)
            row["validationScope"] = scope
            result.append(row)
    return result[: max(20, limit * 5)]


def inkitt_page_scope(url):
    """主榜单只接受首页/广泛发现页；主题页仅作为单独验证，不冒充市场份额。"""
    low = (url or "").lower()
    if "/stories/" in low or "/story/" in low or "/books/" in low:
        return "detail_page"
    if "/topics/" in low or "sub_genres" in low or "/genres/" in low:
        return "niche_validation"
    return "broad_discovery"


def inkitt_validation_scope(url):
    low = (url or "").lower()
    if any(x in low for x in ("lgbt", "queer", "gay", "lesbian")):
        return "lgbt_validation"
    if any(x in low for x in ("horror", "thriller", "suspense")):
        return "horror_validation"
    if any(x in low for x in ("werewolf", "shifter", "wolf")):
        return "shifter_validation"
    if any(x in low for x in ("dark", "adult", "erotica", "steamy")):
        return "adult_validation"
    return "niche_validation"


def start_inkitt_bridge():
    """按需启动本地桥接；扩展只需安装一次，日常运行无需手动开终端。"""
    import socket
    sock = socket.socket()
    try:
        sock.settimeout(0.3)
        if sock.connect_ex(("127.0.0.1", 8912)) == 0:
            return None
    finally:
        sock.close()
    script = os.path.join(HERE, "inkitt_bridge", "inkitt_bridge.py")
    if not os.path.exists(script):
        return None
    log_path = os.path.join(HERE, "radar_en", "inkitt_bridge.log")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    log = open(log_path, "a", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, script], cwd=HERE,
                            stdout=log, stderr=log,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    # Give Windows a moment to bind the local port; failure will be visible in the log.
    time.sleep(0.5)
    return proc


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


def classify_detail(title, description="", tags=None):
    """多字段分类并返回置信度，抑制 Alpha/Dragon 等单词造成的误判。"""
    text = " ".join([title or "", description or "", " ".join(tags or [])]).lower()
    scores = {}
    matched = {}
    for genre, kws in GENRE_KEYWORDS:
        hits = [kw for kw in kws if kw in text]
        if hits:
            scores[genre] = sum(2 if " " in h else 1 for h in hits)
            matched[genre] = hits
    # Alpha/pack/luna 等词只有在出现两个以上，或伴随直接 werewolf/shifter 词时才算狼人。
    wolf_hits = matched.get("werewolf_shifter", [])
    direct_wolf = any(x in wolf_hits for x in ("werewolf", "wolf shifter", "shifter"))
    if not direct_wolf and len(wolf_hits) < 2:
        scores.pop("werewolf_shifter", None)
        matched.pop("werewolf_shifter", None)
    subgenres = [name for name, kws in SUBGENRE_KEYWORDS.items() if any(kw in text for kw in kws)]
    horror_subgenres = [name for name in subgenres if name.startswith("horror_")]
    # Horror tags are a parent-genre signal, not decorative metadata. Keep Romance as a
    # cross-genre modifier when both are present, but make the horror path explicit.
    if horror_subgenres and ("horror" not in scores or scores.get("horror", 0) < len(horror_subgenres)):
        scores["horror"] = max(scores.get("horror", 0), len(horror_subgenres) + 1)
    if not scores:
        return {"genre": "other", "confidence": 0.0, "traits": [], "subgenres": subgenres,
                "horror_subgenres": horror_subgenres}
    genre = max(scores, key=scores.get)
    # 多字段命中且含标签/简介时提高置信度；仍保留可解释的原始词条。
    confidence = min(0.99, 0.35 + 0.12 * scores[genre] + (0.12 if description else 0) + (0.1 if tags else 0))
    return {"genre": genre, "confidence": round(confidence, 2), "traits": matched.get(genre, horror_subgenres if genre == "horror" else []),
            "subgenres": subgenres, "horror_subgenres": horror_subgenres}


def classify(title, description="", tags=None):
    return classify_detail(title, description, tags)["genre"]


def aggregate_signals(entries):
    """跨来源聚合：同一题材在多个读者池出现时提高信号，排名按 log 衰减。"""
    import math
    buckets = {}
    unique = {}
    for e in entries:
        key = (e.get("source", "royalroad"), (e.get("title") or "").strip().lower())
        if key[1] and (key not in unique or int(e.get("rank") or 9999) < int(unique[key].get("rank") or 9999)):
            unique[key] = e
    for e in unique.values():
        source = e.get("source", "royalroad")
        weight = SOURCE_CONFIG.get(source, {}).get("weight", 0.1)
        rank = max(1, int(e.get("rank") or 1))
        score = weight / math.log2(rank + 1)
        g = e.get("category", "other")
        b = buckets.setdefault(g, {"score": 0.0, "sources": set(), "titles": []})
        b["score"] += score
        b["sources"].add(source)
        b["titles"].append(e.get("title", ""))
    result = {}
    for genre, b in sorted(buckets.items(), key=lambda x: x[1]["score"], reverse=True):
        result[genre] = {"signal": round(b["score"], 4), "sourceCount": len(b["sources"]),
                         "sources": sorted(b["sources"]), "titleCount": len(b["titles"]),
                         "titles": b["titles"][:8]}
    return result


def llm_analyze(entries, validated_failures, llm, signal_summary=None, recommendation_count=5,
                portfolio="weighted"):
    """一次 LLM 调用：题材分布统计（硬数字）+ 空位 + 推荐（失败题材降权）。"""
    titles = "\n".join(
        f"- [{e.get('source','unknown')}/{e.get('list','unknown')} rank {e.get('rank','?')}] {e['title']} :: {e.get('description', '')[:120]} :: tags={','.join(e.get('tags', []))} :: inkitt_subgenres={','.join(x for x in e.get('subgenres', []) if not x.startswith('horror_'))}"
        for e in entries[:80]
    )
    fail_block = ""
    if validated_failures:
        fail_block = "\nIMPORTANT: these titles/genres were validated as failures before, deprioritize or avoid them:\n" + \
            "\n".join(f"- {t} ({c or 'unknown'})" for t, c in validated_failures[:10])

    sys_msg = (
        "You are a market analyst for English web fiction across Wattpad, Inkitt, WebNovel/RoyalRoad-style platforms, and Amazon KDP. "
        "Treat each platform as a separate reader pool. Only call a trend cross-market when it appears in multiple sources; do not generalize from RoyalRoad alone."
    )
    portfolio_instruction = (
        "Portfolio weighting for this run: when requesting 8 items, output exactly 5 werewolf/shifter romance concepts, "
        "exactly 2 dark/adult romance concepts, and exactly 1 LGBTQ+ romance concept. These eight slots are mutually exclusive. "
        "For other list sizes, preserve these proportions as closely as possible. Werewolf/shifter and dark/adult romance should be prioritized "
        "when supported by the evidence, but do not force a label unsupported by titles or synopses. "
        "Hard requirements for an 8-item weighted run: at least one werewolf/shifter item must have adult_intensity 3 or 4; "
        "at least one dark romance item must have adult_intensity 4 or 5; "
        "and the LGBTQ+ romance must have adult_intensity 4 or 5. Do not count the LGBTQ+ item as one of the five werewolf or two dark-romance slots. "
        "Do not recommend Horror, Thriller, Suspense, Slasher, serial-killer, zombie, or other fear-led concepts as a parent genre or subgenre. "
        "Treat Inkitt tags as reusable modifiers, not exclusive genres: Contemporary, Enemies to Lovers, Dark, Erotic, Billionaires, "
        "Fantasy/Romantasy, New Adult, Mafia, and Second Chance may be paired independently with the parent genres werewolf/shifter, dark romance, or contemporary romance. "
        "For every recommendation, identify parent_genre separately from the reusable subgenres list. "
        "Most important: write a high-concept sellable hook, not a generic trope bundle. Each item must include a one-sentence click_hook, "
        "a first_chapter_inciting_event that changes the heroine's situation immediately, a power_reversal, a dangerous_secret, and an irreversible_twist. "
        "The hook must contain a specific unusual mechanism, cost, rule, or deadline. 'Fated mates', 'mafia boss', 'fake marriage', or 'second chance' alone are rejected as hooks. "
        "Do not reduce adult intensity to possessive or dangerous. Classify it across explicit adult dimensions: sexual_erotic (sexual desire/relationship and consequences), "
        "psychological_dark (control, trauma, obsession, manipulation), taboo_moral (crime, betrayal, power imbalance, ethical gray zones), "
        "and mature_romance (adult responsibility, sex/emotion consequences, irreversible relationship cost). Each item must list adult_dimensions and explain the concrete scene-level source of tension without writing graphic sexual prose. "
        "Because this is for an overseas adult Romance market, every romance item must explicitly state sex_tag=true and sensuality_level 1-5. For sensuality 4-5, write mature, strongly suggestive tension—adult desire, charged touch, proximity, consent negotiation, temptation, and emotional/relationship aftermath—without graphic anatomical sex prose. "
        "The concept field must be a clear 120-180 word commercial synopsis, not a one-line logline. It must explain protagonist goal, immediate threat, relationship dynamic, adult tension mechanism, escalation, and a concrete end-of-book question. "
        if portfolio != "natural" else
        "Do not impose a quota for werewolf, shifter, or dark/adult romance. Let the evidence determine the natural mix of genres, while keeping each concept distinct. "
    )
    user = (
        "Platform research matrix (interpret signals using these reader-pool definitions):\n" + json.dumps(PLATFORM_PROFILES, ensure_ascii=False) + "\n\n"
        "Here are current multi-source lists (source/list/rank :: title :: synopsis :: tags):\n\n" + titles + "\n\n"
        "Cross-source signal summary (computed, not a guess):\n" + json.dumps(signal_summary or {}, ensure_ascii=False) + "\n\n"
        "Step 1: classify these titles into genres and give per-source and cross-source DISTRIBUTION "
        "(count + percentage per genre). This is hard data, be precise. "
        "Use the synopsis to judge the genre, not just the title.\n"
        "Step 2: identify the GAPS: genres with reader demand but few competing titles.\n"
        f"Step 3: output exactly {recommendation_count} original short-fiction concepts (US-market English, 12-18 chapters). "
        + portfolio_instruction
        + "Adult/explicitness is a market-positioning tag only; "
        "do not generate graphic sexual content. Every concept must have a distinct core_trope and a concrete unique_hook; reject renamed copies of another item. "
        "Score each item from 1-10 for differentiation and market fit, and use adult_intensity 1-5 (mature market positioning, not graphic detail).\n"
        + fail_block + "\n\n"
        "Output STRICT JSON only:\n"
        '{"genreDistribution": {"litrpg": 8, "fantasy": 5}, "marketSummary": "...", '
        '"recommendations": [{"genre": "...", "parent_genre": "...", "subgenres": ["..."], "concept": "...", "click_hook": "...", '
        '"first_chapter_inciting_event": "...", "power_reversal": "...", "dangerous_secret": "...", "irreversible_twist": "...", "core_trope": "...", "unique_hook": "...", '
        '"adult_intensity": 1, "adult_dimensions": ["sexual_erotic", "psychological_dark"], "content_warnings": ["..."], "adult_angle": "...", "differentiation_score": 8, "market_fit_score": 8, "confidence": 0.9, '
        '"marketSignal": "cross_source|single_source|experimental", "reasoning": "...", "benchmarkTitles": ["..."]}]}'
    )
    messages = [
        {"role": "system", "content": sys_msg},
        {"role": "user", "content": user},
    ]
    r = llm.chat("radar", messages, max_tokens=10000, temperature=0.5)
    # Ark 上的轻量 radar 模型偶发只返回 reasoning；handoff 模型作为同端点的 JSON 兜底。
    if ("error" in r or not r.get("content")) and "handoff" in llm.models:
        fallback = llm.chat("handoff", messages, max_tokens=10000, temperature=0.3)
        if "error" not in fallback and fallback.get("content"):
            fallback["fallbackFrom"] = "radar"
            return fallback
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


def llm_retry_recommendations(entries, signal_summary, count, llm, portfolio="weighted"):
    """主分析返回空/少量推荐时，用精简证据重试，避免重复抓榜。"""
    evidence = "\n".join(
        f"- [{e.get('source')}] {e.get('title')} :: {e.get('description', '')[:160]} :: subgenres={','.join(x for x in e.get('subgenres', []) if not x.startswith('horror_'))}"
        for e in entries[:50]
    )
    portfolio_instruction = (
        "For count 8, output exactly 5 werewolf/shifter romance items, exactly 2 dark/adult romance items, and exactly 1 LGBTQ+ romance item. The three groups are mutually exclusive. Exclude Horror, Thriller, Suspense, Slasher, serial-killer, zombie, and other fear-led concepts as parent genres or subgenres. "
        if portfolio != "natural" else
        "Do not impose a quota for werewolf, shifter, or dark/adult romance; let the evidence determine the mix. "
    )
    prompt = (
        f"Create exactly {count} commercially plausible English short-fiction recommendations from the evidence below. "
        + portfolio_instruction
        + ("For count 8, hard requirements: one werewolf/shifter item adult_intensity 3-4; one dark romance item adult_intensity 4-5; "
           "at least one of the two dark-romance items adult_intensity 4-5; and the one explicitly LGBTQ+ romance item adult_intensity 4-5. Do not overlap the three category groups. "
           if portfolio != "natural" else "")
        + "Do not force unsupported genres. Adult/explicitness is only a market-positioning label; "
        + "Use reusable subgenre tags across parent genres (for example, Dark + Contemporary, Erotic + Werewolf/Shifter, or Mafia + Contemporary); do not treat these tags as mutually exclusive. "
        + "Adult intensity must be supported by concrete mature dimensions, not generic words like spicy or possessive. Use adult_dimensions from sexual_erotic, psychological_dark, taboo_moral, mature_romance, and include content_warnings. "
        + "Do not return safe filler such as a generic small-town reunion, ordinary fake dating, or a standard rejected mate unless the unusual mechanism is the main selling point. "
        "do not generate graphic sexual content. Every romance item must set sex_tag=true and sensuality_level 1-5, using mature suggestive tension rather than anatomical sex prose. "
        "Each concept must be a clear 120-180 word commercial synopsis explaining protagonist goal, immediate threat, relationship dynamic, adult tension mechanism, escalation, and the end-of-book question. "
        "Each item must include a distinct core_trope, unique_hook, adult_intensity, differentiation_score, and market_fit_score. "
        "Reject renamed copies of another item. Each item must cite benchmark titles from the evidence and identify whether its genre signal is cross-source or single-source. "
        "Return STRICT JSON only, with no markdown or commentary:\n"
        '{"recommendations":[{"genre":"...","parent_genre":"...","subgenres":["..."],"concept":"...","click_hook":"...",'
        '"first_chapter_inciting_event":"...","power_reversal":"...","dangerous_secret":"...","irreversible_twist":"...",'
        '"core_trope":"...","unique_hook":"...",'
        '"sex_tag":true,"sensuality_level":4,"adult_intensity":1,"adult_dimensions":["..."],"content_warnings":["..."],"adult_angle":"...",'
        '"differentiation_score":8,"market_fit_score":8,"confidence":0.8,'
        '"marketSignal":"cross_source|single_source","reasoning":"...","benchmarkTitles":["..."]}]}\n\n'
        "Computed signals:\n" + json.dumps(signal_summary, ensure_ascii=False) + "\n\nEvidence:\n" + evidence
    )
    messages = [{"role": "system", "content": "You are a precise English fiction market analyst. Output valid JSON only."},
                {"role": "user", "content": prompt}]
    for role in ("handoff", "radar"):
        if role not in llm.models:
            continue
        r = llm.chat(role, messages, max_tokens=10000, temperature=0.35)
        if "error" not in r:
            parsed = parse_llm_json(r.get("content", ""))
            recs = parsed.get("recommendations", [])
            if isinstance(recs, list) and recs:
                return recs[:count]
    return []


def recommendation_constraints_met(recs, count, portfolio):
    """轻量硬约束检查；只在加权 8 条结果不达标时触发一次模型重试。"""
    if any(is_removed_horror_recommendation(r) for r in recs):
        return False
    if portfolio == "natural" or count != 8:
        return True
    if len(recs) != 8:
        return False
    def genre_text(r):
        return " ".join(str(r.get(k, "")) for k in ("genre", "concept", "core_trope", "unique_hook", "adult_angle")).lower()
    def primary_text(r):
        subgenres = r.get("subgenres", []) if isinstance(r.get("subgenres"), list) else []
        return " ".join([str(r.get("genre", "")), str(r.get("parent_genre", "")), *map(str, subgenres)]).lower()
    wolf_items = [r for r in recs if any(x in primary_text(r) for x in ("werewolf", "wolf", "shifter"))]
    wolf = len(wolf_items) == 5 and any(3 <= int(r.get("adult_intensity", 0) or 0) <= 4 for r in wolf_items)
    lgbtq_items = [r for r in recs if any(x in primary_text(r) for x in ("lgbtq", "lgbt", "queer", "lesbian", "gay", "bisexual", "trans "))]
    dark_items = [r for r in recs if "dark" in primary_text(r) and r not in wolf_items and r not in lgbtq_items]
    dark = len(dark_items) == 2 and any(int(r.get("adult_intensity", 0) or 0) >= 4 for r in dark_items)
    lgbtq_adult = len(lgbtq_items) == 1 and 4 <= int(lgbtq_items[0].get("adult_intensity", 0) or 0) <= 5
    exclusive_groups = not any(r in wolf_items for r in lgbtq_items)
    return wolf and dark and lgbtq_adult and exclusive_groups


def is_removed_horror_recommendation(rec):
    """禁止恐怖/惊悚作为推荐板块，但不限制暗黑爱情中的一般危险情节。"""
    fields = [rec.get("genre", ""), rec.get("parent_genre", ""), rec.get("core_trope", "")]
    fields.extend(rec.get("subgenres", []) if isinstance(rec.get("subgenres"), list) else [])
    category_text = " ".join(str(x) for x in fields).lower()
    removed = (
        "horror", "thriller", "suspense", "slasher", "splatterpunk", "serial killer",
        "zombie", "body horror", "found footage", "cosmic horror", "folk horror",
    )
    return any(term in category_text for term in removed)


def dedup(recommendations, store):
    """按题材+概念去重；同一大类允许保留不同细分，避免“推荐过 romance 就屏蔽所有 romance”。"""
    recent = store.recent_recommendations(24)
    kept, dropped = [], []
    for rec in recommendations:
        g = normalize_genre(rec.get("genre", ""))
        concept = re.sub(r"[^a-z0-9 ]", " ", (rec.get("concept") or "").lower())
        tokens = {x for x in concept.split() if len(x) > 3}
        duplicate = False
        for old in recent:
            if normalize_genre(old.get("genre", "")) != g:
                continue
            old_tokens = {x for x in re.sub(r"[^a-z0-9 ]", " ", (old.get("concept") or "").lower()).split() if len(x) > 3}
            if tokens and len(tokens & old_tokens) / max(1, len(tokens | old_tokens)) >= 0.45:
                duplicate = True
                break
        if duplicate:
            dropped.append(rec.get("genre", ""))
        else:
            recent.append({"genre": g, "concept": concept})
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


def attach_signal_evidence(recommendations, signal_summary):
    """校正 LLM 的 marketSignal，防止“混合两个单源题材”被夸大成跨平台共识。"""
    for rec in recommendations:
        text = " ".join([rec.get("genre", ""), rec.get("concept", "")]).lower()
        evidence = []
        for genre, info in (signal_summary or {}).items():
            aliases = [genre.replace("_", " "), genre.replace("_", ""),
                       {"werewolf_shifter": "werewolf", "dark_romance": "dark romance"}.get(genre, "")]
            if any(a and a in text for a in aliases):
                evidence.append({"genre": genre, "sourceCount": info.get("sourceCount", 0),
                                 "sources": info.get("sources", []), "signal": info.get("signal", 0)})
        rec["signalEvidence"] = evidence
        direct_cross = any(x["sourceCount"] >= 2 for x in evidence)
        rec["marketSignal"] = "cross_source" if direct_cross else "single_source"
    return recommendations


def main():
    ap = argparse.ArgumentParser(description="英文选品雷达 M2")
    ap.add_argument("--list", type=int, default=20, help="每榜抓取数")
    ap.add_argument("--lists", default="best-rated,complete,trending", help="RoyalRoad 榜单，兼容旧用法")
    ap.add_argument("--sources", default=os.environ.get("RADAR_SOURCES", "inkitt_browser,amazon"),
                    help="逗号分隔来源（默认不含 Royal Road；可显式加入）")
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--no-chapters", action="store_true", help="跳过第一章爬取")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--recommend-count", type=int, default=5, help="要求 LLM 输出的推荐数量")
    ap.add_argument("--portfolio", choices=("weighted", "natural"), default="weighted",
                    help="推荐组合：weighted 为狼人/暗黑优先，natural 由数据自然决定")
    ap.add_argument("--inkitt-scope", choices=("broad_discovery", "current"), default="broad_discovery",
                    help="Inkitt 主雷达默认只用首页/广泛发现页；current 用于专题验证")
    args = ap.parse_args()

    cfg = load_json(os.path.join(HERE, "config.json"))
    store = StateStore()

    scan = {"timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": args.sources, "entries": {}, "genreDistribution": {},
            "recommendations": [], "dropped": []}

    # 1. 抓榜 + 粗分类 + 归档
    all_entries = []
    scan["sourceStatus"] = {}
    # 兼容 PowerShell/聊天复制时把下划线写成 ``\_`` 的情况。
    sources = [s.strip().lower().replace("\\_", "_") for s in args.sources.split(",") if s.strip()]
    for source in sources:
        cfg_source = SOURCE_CONFIG.get(source)
        if not cfg_source:
            print(f"[skip] unknown source: {source}")
            continue
        lists = cfg_source["lists"]
        if source == "inkitt_browser":
            try:
                start_inkitt_bridge()
                entries, page_url = [], ""
                last_error = None
                for _ in range(12):
                    try:
                        entries, page_url = load_inkitt_browser(args.list, args.inkitt_scope)
                        break
                    except Exception as exc:
                        last_error = exc
                        time.sleep(2.5)
                if last_error and not entries:
                    raise last_error
                page_scope = inkitt_page_scope(page_url)
                if page_scope == "niche_validation":
                    print("[warn] Inkitt niche page detected: use for sub-genre validation, not broad market share")
                for e in entries:
                    detail = classify_detail(e.get("title", ""), e.get("description", ""), e.get("tags", []))
                    e.update({"source": source, "list": page_url or "inkitt",
                              "category": detail["genre"], "categoryConfidence": detail["confidence"],
                              "traits": detail["traits"], "subgenres": detail["subgenres"],
                              "horror_subgenres": detail["horror_subgenres"]})
                key = "inkitt_browser:page"
                store.archive_trends(key, entries)
                scan["entries"][key] = entries
                all_entries += entries
                scan["sourceStatus"][key] = {"status": "ok", "count": len(entries), "pageScope": page_scope,
                                              "url": page_url}
                print(f"[ok] {key}: {len(entries)} titles archived")
            except Exception as e:
                key = "inkitt_browser:page"
                scan["sourceStatus"][key] = {"status": "error", "error": str(e)[:240]}
                scan["entries"][key] = []
                print(f"[fail] {key}: {e}")
            continue
        if source == "inkitt_browser_validation":
            key = "inkitt_browser_validation:pages"
            try:
                entries = load_inkitt_validation(args.list)
                for e in entries:
                    detail = classify_detail(e.get("title", ""), e.get("description", ""), e.get("tags", []))
                    e.update({"category": detail["genre"], "categoryConfidence": detail["confidence"],
                              "traits": detail["traits"], "subgenres": detail["subgenres"],
                              "horror_subgenres": detail["horror_subgenres"]})
                scan["entries"][key] = entries
                all_entries += entries
                scan["sourceStatus"][key] = {"status": "ok" if entries else "empty", "count": len(entries),
                                              "scopes": sorted({e.get("validationScope") for e in entries})}
                print(f"[ok] {key}: {len(entries)} titles archived")
            except Exception as e:
                scan["sourceStatus"][key] = {"status": "error", "error": str(e)[:240]}
                scan["entries"][key] = []
                print(f"[fail] {key}: {e}")
            continue
        if source == "royalroad":
            lists = {k: v for k, v in lists.items() if k in [x.strip() for x in args.lists.split(",")]}
        for list_name, url in lists.items():
            key = f"{source}:{list_name}"
            try:
                html = fetch_inkitt(url) if source == "inkitt" else fetch(url)
                entries = parse_source(source, html, args.list)
                for e in entries:
                    detail = classify_detail(e["title"], e.get("description", ""), e.get("tags", []))
                    e.update({"source": source, "list": list_name,
                              "category": detail["genre"], "categoryConfidence": detail["confidence"],
                              "traits": detail["traits"], "subgenres": detail["subgenres"],
                              "horror_subgenres": detail["horror_subgenres"]})
                store.archive_trends(key, entries)
                scan["entries"][key] = entries
                all_entries += entries
                scan["sourceStatus"][key] = {"status": "ok", "count": len(entries)}
                print(f"[ok] {key}: {len(entries)} titles archived")
            except Exception as e:
                scan["sourceStatus"][key] = {"status": "error", "error": str(e)[:240]}
                print(f"[fail] {key}: {e}")
                scan["entries"][key] = []

    scan["crossSourceSignals"] = aggregate_signals(all_entries)

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

    # 2. LLM 分析（统计 + 推荐 + 失败降权）。没有任何有效来源时禁止生成无依据推荐。
    if not all_entries:
        print("[skip] llm analyze: no valid entries collected")
    elif not args.no_llm:
        llm = LLM(cfg)
        validated = store.validated_failures()
        print(f"[info] validated failures to deprioritize: {len(validated)}")
        r = llm_analyze(all_entries, validated, llm, scan["crossSourceSignals"],
                        max(1, min(20, args.recommend_count)), args.portfolio)
        if "error" in r:
            print(f"[fail] llm analyze: {r['error']}")
        else:
            result = parse_llm_json(r["content"])
            scan["genreDistribution"] = result.get("genreDistribution", {})
            scan["marketSummary"] = result.get("marketSummary", "")
            recs = result.get("recommendations", [])
            requested_count = max(1, min(20, args.recommend_count))
            if len(recs) < requested_count or not recommendation_constraints_met(recs, requested_count, args.portfolio):
                reason = f"{len(recs)}/{requested_count}" if len(recs) < requested_count else "hard constraints unmet"
                print(f"[retry] recommendations {reason}; using compact evidence retry")
                for _ in range(2):
                    retry_recs = llm_retry_recommendations(all_entries, scan["crossSourceSignals"], requested_count, llm,
                                                           args.portfolio)
                    if retry_recs:
                        recs = retry_recs
                    if len(recs) == requested_count and recommendation_constraints_met(recs, requested_count, args.portfolio):
                        break
            removed_recs = [r for r in recs if is_removed_horror_recommendation(r)]
            if removed_recs:
                print(f"[filter] removed horror/thriller recommendations: {len(removed_recs)}")
                recs = [r for r in recs if not is_removed_horror_recommendation(r)]
            recs = attach_signal_evidence(recs, scan["crossSourceSignals"])
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
        # Windows 默认 GBK 无法输出部分榜单书名；扫描文件已经 UTF-8 保存。
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        print(json.dumps(scan, ensure_ascii=False, indent=2))

    store.close()


if __name__ == "__main__":
    main()
