# -*- coding: utf-8 -*-
"""
packager.py — 包装（FR-4，简化版）

读 shorts/<id>/ 的产出，生成标准发布文件包到 out/<id>/：
  1. publish_manifest.json（投放对接契约，含 AI 披露）
  2. publish_manifest.md（人类可读版）
  3. <title>.epub（用 zipfile 手写最小 EPUB，不依赖第三方库）
  4. 章节 HTML（EPUB 内部）

用法:
    py -3 packager.py --story-id probe_worldbible
    py -3 packager.py --story-id probe_worldbible --no-epub
"""
import argparse
import html
import json
import os
import re
import zipfile
from datetime import datetime, timezone

from engine import LLM, load_json

HERE = os.path.dirname(os.path.abspath(__file__))


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def md_to_html(md_text):
    """极简 markdown → xhtml（按空行分段落，处理粗体/斜体）。"""
    paras = [p.strip() for p in md_text.split("\n\n") if p.strip()]
    out = []
    for p in paras:
        p = html.escape(p)
        p = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", p)
        p = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", p)
        out.append(f"<p>{p}</p>")
    return "\n".join(out)


def generate_package_meta(full_text, direction, llm):
    """LLM 生成发布元数据：title / synopsis / selling points / keywords。"""
    sys_msg = (
        "You are a book marketing specialist preparing a serialized English fiction "
        "for publication on Amazon KDP and RoyalRoad. Write persuasive, accurate copy."
    )
    user = (
        f"Story direction: {direction}\n\n"
        f"Full text (excerpt):\n{full_text[:6000]}\n\n"
        "Based on this story, output STRICT JSON:\n"
        '{"title": "catchy title", "synopsis": "100-150 word hook synopsis, no spoilers", '
        '"sellingPoints": ["3-5 marketing bullets"], "keywords": ["5-8 SEO keywords/phrases"]}'
    )
    r = llm.chat("outline", [
        {"role": "system", "content": sys_msg},
        {"role": "user", "content": user},
    ], max_tokens=1500, temperature=0.6)
    return r


def parse_meta(content):
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
        return {}


def build_manifest(meta, story_id, chapters_count, total_words, epub_path, cover_path):
    """组装 publish_manifest 契约（含 AI 披露字段）。"""
    manifest = {
        "storyId": story_id,
        "title": meta.get("title", story_id),
        "genre": "",
        "synopsis": meta.get("synopsis", ""),
        "sellingPoints": meta.get("sellingPoints", []),
        "keywords": meta.get("keywords", []),
        "chapters": chapters_count,
        "totalWords": total_words,
        "coverPath": cover_path or "",
        "epubPath": epub_path or "",
        "aiDisclosure": True,
        "aiDisclosureNote": "AI-generated content; disclose per platform policy",
        "generatedAt": now_iso(),
    }
    return manifest


def validate_manifest(m):
    """缺字段校验：必填字段缺失报错。"""
    required = ["title", "synopsis", "chapters", "aiDisclosure"]
    missing = [k for k in required if not m.get(k)]
    return missing


def build_epub(chapters, meta, out_path):
    """用 zipfile 手写最小 EPUB 3.0。"""
    title = meta.get("title", "Untitled")
    book_id = "urn:uuid:inkos-" + re.sub(r"[^a-z0-9]", "", title.lower())[:16]

    content_opf = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="bookid">{book_id}</dc:identifier>
    <dc:title>{html.escape(title)}</dc:title>
    <dc:creator>AI Writing Engine</dc:creator>
    <dc:language>en</dc:language>
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    {"".join(f'<item id="ch{i}" href="ch{i}.xhtml" media-type="application/xhtml+xml"/>' for i in range(1, len(chapters) + 1))}
  </manifest>
  <spine>
    {"".join(f'<itemref idref="ch{i}"/>' for i in range(1, len(chapters) + 1))}
  </spine>
</package>"""

    def chapter_xhtml(i, text):
        body = md_to_html(text)
        return f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Chapter {i}</title></head>
<body><h1>Chapter {i}</h1>{body}</body></html>"""

    nav = f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Contents</title></head>
<body><nav epub:type="toc" xmlns:epub="http://www.idpf.org/2007/ops">
<h1>Contents</h1><ol>{''.join(f'<li><a href="ch{i}.xhtml">Chapter {i}</a></li>' for i in range(1, len(chapters) + 1))}</ol></nav></body></html>"""

    with zipfile.ZipFile(out_path, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                   '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        z.writestr("OEBPS/content.opf", content_opf)
        z.writestr("OEBPS/nav.xhtml", nav)
        for i, text in enumerate(chapters, 1):
            z.writestr(f"OEBPS/ch{i}.xhtml", chapter_xhtml(i, text))
    return out_path


def main():
    ap = argparse.ArgumentParser(description="包装 FR-4")
    ap.add_argument("--story-id", required=True, help="shorts/ 下的故事 id")
    ap.add_argument("--no-epub", action="store_true", help="跳过 EPUB 生成")
    args = ap.parse_args()

    cfg = load_json(os.path.join(HERE, "config.json"))
    llm = LLM(cfg)

    short_dir = os.path.join(HERE, "shorts", args.story_id)
    if not os.path.isdir(short_dir):
        print(f"story dir not found: {short_dir}")
        return

    # 读章节
    chapters = []
    for fn in sorted(os.listdir(short_dir)):
        if re.match(r"ch\d+\.md$", fn) and ".revised" not in fn and ".issues" not in fn:
            chapters.append(open(os.path.join(short_dir, fn), encoding="utf-8").read())
    if not chapters:
        print("no chapters found")
        return
    full_text = "\n\n".join(chapters)
    total_words = len(re.findall(r"[A-Za-z']+", full_text))

    # 方向：从 story 自己的 outline.md 读（不用全局 directions/，避免题材串）
    direction = ""
    ol = os.path.join(short_dir, "outline.md")
    if os.path.exists(ol):
        direction = open(ol, encoding="utf-8").read()[:500]

    # LLM 生成元数据
    print(f"[1/2] generating package meta ({len(chapters)} chapters, {total_words} words) ...")
    meta = {}
    r = generate_package_meta(full_text, direction, llm)
    if "error" in r:
        print(f"    meta FAILED: {r['error']} (using fallback title)")
    else:
        meta = parse_meta(r["content"])
        print(f"    title: {meta.get('title', '?')}")

    out_dir = os.path.join(HERE, "out", args.story_id)
    os.makedirs(out_dir, exist_ok=True)

    # EPUB
    epub_path = ""
    if not args.no_epub:
        title_slug = re.sub(r"[^a-z0-9]+", "-", meta.get("title", args.story_id).lower()).strip("-")
        epub_path = os.path.join(out_dir, f"{title_slug}.epub")
        try:
            build_epub(chapters, meta, epub_path)
            print(f"    epub -> {epub_path}")
        except Exception as e:
            print(f"    epub FAILED: {e}")
            epub_path = ""

    # Manifest
    manifest = build_manifest(meta, args.story_id, len(chapters), total_words, epub_path, "")
    missing = validate_manifest(manifest)
    if missing:
        print(f"[FAIL] manifest missing fields: {missing} (not publishing)")
    else:
        print("[2/2] manifest valid")

    json_path = os.path.join(out_dir, "publish_manifest.json")
    json.dump(manifest, open(json_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    # 人类可读版
    md_lines = [f"# {manifest['title']}", "", f"## Synopsis", manifest['synopsis'], "",
                "## Selling Points"]
    md_lines += [f"- {s}" for s in manifest['sellingPoints']]
    md_lines += ["", "## Keywords", ", ".join(manifest['keywords']), "",
                 f"- Chapters: {manifest['chapters']}", f"- Total words: {manifest['totalWords']}",
                 f"- AI disclosure: {'yes' if manifest['aiDisclosure'] else 'no'}",
                 f"- EPUB: {manifest['epubPath'] or 'skipped'}"]
    open(os.path.join(out_dir, "publish_manifest.md"), "w", encoding="utf-8").write("\n".join(md_lines))

    print(f"\nDONE: {out_dir}")
    print(f"  - publish_manifest.json ({'valid' if not missing else 'INCOMPLETE: ' + str(missing)})")
    print(f"  - publish_manifest.md")


if __name__ == "__main__":
    main()
