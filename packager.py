# -*- coding: utf-8 -*-
"""
packager.py — 包装（FR-4，简化版）

读 shorts/<id>/ 的产出，生成标准发布文件包到 out/<id>/：
  1. publish_manifest.json（投放对接契约，含 AI 披露）
  2. publish_manifest.md（人类可读版）
  3. <title>.txt（带目录和章节标题的运营正文）
  4. <title>.epub（用 zipfile 手写最小 EPUB，不依赖第三方库）
  5. 章节 HTML（EPUB 内部）

用法:
    py -3 packager.py --story-id probe_worldbible
    py -3 packager.py --story-id probe_worldbible --no-epub
"""
import argparse
import hashlib
import html
import json
import os
import re
import shutil
import zipfile
from datetime import datetime, timezone

from engine import LLM, load_json, load_env
from writing_status import is_deliverable, load_status
import quality_gate

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


def generate_package_meta(full_text, direction, llm, chapter_count):
    """LLM 生成发布元数据：title / synopsis / selling points / keywords。"""
    sys_msg = (
        "You are a book marketing specialist preparing a serialized English fiction "
        "for publication on Amazon KDP and RoyalRoad. Write persuasive, accurate copy."
    )
    user = (
        f"Story direction: {direction}\n\n"
        f"Full text (excerpt):\n{full_text[:6000]}\n\n"
        "Based on this story, output STRICT JSON:\n"
        '{"title": "catchy title", "synopsis": "120-180 word opening-arc synopsis", '
        '"sellingPoints": ["3-5 marketing bullets"], "keywords": ["5-8 SEO keywords/phrases"], "arcStatus": "opening_arc"}\n\n'
        "Synopsis requirements: write one coherent English synopsis, not a chapter-by-chapter list and not a generic trope pitch. "
        "Summarize the current story arc as a whole: protagonist's starting problem, the central relationship/conflict, the concrete mechanism that makes this story distinctive, the major escalation or reversal, and the unresolved question or continuation drive at the end. "
        "Name the actual stakes and the choice the protagonist faces. Keep it specific to this manuscript and avoid inventing events absent from the text. "
        "The synopsis must explain what the book is fundamentally about beneath its tropes, while remaining suitable for a platform book page and avoiding a full ending spoiler. "
        f"These {chapter_count} chapters are only the current opening installment of a longer serial, not the complete novel: explicitly frame the ending as a provisional milestone, preserve the larger unresolved conflict, and close on a compelling continuation hook. Do not imply that the central mystery, romance, or world conflict is fully resolved."
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


def build_manifest(meta, story_id, chapters_count, total_words, epub_path, cover_path, quality_gate_summary=None,
                   native_review_summary=None, release_status="blocked", release_blockers=None, plain_text_path=""):
    """组装 publish_manifest 契约（含 AI 披露字段）。"""
    manifest = {
        "storyId": story_id,
        "title": meta.get("title", story_id),
        "genre": "",
        "synopsis": meta.get("synopsis", ""),
        "sellingPoints": meta.get("sellingPoints", []),
        "keywords": meta.get("keywords", []),
        "arcStatus": meta.get("arcStatus", "opening_arc"),
        "chapters": chapters_count,
        "totalWords": total_words,
        "coverPath": cover_path or "",
        "epubPath": epub_path or "",
        "plainTextPath": plain_text_path or "",
        "aiDisclosure": True,
        "aiDisclosureNote": "AI-generated content; disclose per platform policy",
        "generatedAt": now_iso(),
        "qualityGate": quality_gate_summary or {"status": "not_required"},
        "nativeReview": native_review_summary or {"status": "not_required"},
        "releaseStatus": release_status,
        "releaseBlockers": list(release_blockers or []),
    }
    return manifest


def validate_manifest(m):
    """缺字段校验：必填字段缺失报错。"""
    required = ["title", "synopsis", "chapters", "aiDisclosure"]
    missing = [k for k in required if not m.get(k)]
    return missing


def validate_delivery_artifacts(short_dir, out_dir, plain_text_path):
    """Require reader-facing copy and the operator TXT before a package is complete."""
    required = {
        "summary_cn.md": os.path.join(short_dir, "summary_cn.md"),
        "plain_text": plain_text_path,
    }
    missing = []
    for name, path in required.items():
        if not os.path.isfile(path):
            missing.append(name)
            continue
        with open(path, encoding="utf-8") as handle:
            if not handle.read().strip():
                missing.append(name)
    return missing


def package_exit_code(release_blockers):
    """Use the manifest release decision as the process result."""
    return 0 if not release_blockers else 2


def summarize_native_review(out_dir, short_dir, chapter_files):
    """Require current, full-book editorial evidence for an explicit release gate."""
    path = os.path.join(out_dir, "native_check_result.json")
    if not os.path.isfile(path):
        return {"status": "missing", "missing_files": list(chapter_files), "review_required": []}
    try:
        rows = json.load(open(path, encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "invalid", "missing_files": list(chapter_files), "review_required": []}
    by_file = {row.get("file"): row for row in rows if isinstance(row, dict)} if isinstance(rows, list) else {}
    missing, stale, review_required, failed = [], [], [], []
    for filename in chapter_files:
        row = by_file.get(filename)
        if row is None:
            missing.append(filename)
            continue
        text = open(os.path.join(short_dir, filename), encoding="utf-8").read()
        current_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if row.get("source_sha256") != current_hash:
            stale.append(filename)
        # New native-check reports distinguish a reviewed chapter from one
        # whose three fallback providers exhausted every scheduled retry.
        # Preserve legacy report compatibility by applying this only when the
        # explicit coverage field is present.
        if "review_coverage" in row and row.get("review_coverage") != "reviewed":
            failed.append(filename)
        if row.get("editorial_findings"):
            review_required.append(filename)
        if row.get("failed") or row.get("error") or row.get("blocking_findings"):
            failed.append(filename)
    return {
        "status": "verified" if not (missing or stale or review_required or failed) else "blocked",
        "missing_files": missing,
        "stale_files": stale,
        "review_required": review_required,
        "failed_files": list(dict.fromkeys(failed)),
    }


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


def build_plain_text(chapters, meta, out_path):
    """Build the required operator TXT with a readable table of contents."""
    title = (meta.get("title") or "Untitled").strip()
    lines = [title, "", "TABLE OF CONTENTS"]
    lines.extend(f"Chapter {number}" for number in range(1, len(chapters) + 1))
    for number, chapter in enumerate(chapters, 1):
        lines.extend(["", f"CHAPTER {number}", "", chapter.strip()])
    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines).rstrip() + "\n")
    return out_path


def main():
    ap = argparse.ArgumentParser(description="包装 FR-4")
    ap.add_argument("--story-id", required=True, help="shorts/ 下的故事 id")
    ap.add_argument("--no-epub", action="store_true", help="跳过 EPUB 生成")
    ap.add_argument("--require-quality-gate", action="store_true", help="仅在全书质量门禁证据通过时包装")
    args = ap.parse_args()

    cfg = load_json(os.path.join(HERE, "config.json"))
    load_env()
    llm = LLM(cfg)

    short_dir = os.path.join(HERE, "shorts", args.story_id)
    if not os.path.isdir(short_dir):
        print(f"story dir not found: {short_dir}")
        return 2

    try:
        writing_status = load_status(short_dir)
    except (OSError, ValueError) as exc:
        print(f"writing status invalid: {exc}")
        return 2
    if not is_deliverable(writing_status):
        print(f"writing status is not deliverable: {writing_status}")
        return 2

    # 读章节
    chapters = []
    for fn in sorted(os.listdir(short_dir)):
        if re.match(r"ch\d+\.md$", fn) and ".revised" not in fn and ".issues" not in fn:
            chapters.append(open(os.path.join(short_dir, fn), encoding="utf-8").read())
    if not chapters:
        print("no chapters found")
        return 2
    quality_summary = quality_gate.summarize_story(short_dir)
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
    r = generate_package_meta(full_text, direction, llm, len(chapters))
    if "error" in r:
        print(f"    meta FAILED: {r['error']} (using fallback title)")
    else:
        meta = parse_meta(r["content"])
        print(f"    title: {meta.get('title', '?')}")
    meta.setdefault("title", args.story_id)

    out_dir = os.path.join(HERE, "out", args.story_id)
    os.makedirs(out_dir, exist_ok=True)
    chapter_files = [
        fn for fn in sorted(os.listdir(short_dir))
        if re.match(r"ch\d+\.md$", fn) and ".revised" not in fn and ".issues" not in fn
    ]
    native_review_summary = summarize_native_review(out_dir, short_dir, chapter_files)
    summary_path = os.path.join(short_dir, "summary_cn.md")
    if os.path.isfile(summary_path):
        shutil.copyfile(summary_path, os.path.join(out_dir, "summary_cn.md"))

    title_slug = re.sub(r"[^a-z0-9]+", "-", meta.get("title", args.story_id).lower()).strip("-")
    plain_text_path = os.path.join(out_dir, f"{title_slug}.txt")
    try:
        build_plain_text(chapters, meta, plain_text_path)
        print(f"    txt -> {plain_text_path}")
    except Exception as e:
        print(f"    txt FAILED: {e}")
        plain_text_path = ""

    # EPUB
    epub_path = ""
    if not args.no_epub:
        epub_path = os.path.join(out_dir, f"{title_slug}.epub")
        try:
            build_epub(chapters, meta, epub_path)
            print(f"    epub -> {epub_path}")
        except Exception as e:
            print(f"    epub FAILED: {e}")
            epub_path = ""

    # Manifest
    provisional = build_manifest(
        meta, args.story_id, len(chapters), total_words, epub_path, "", quality_summary, native_review_summary
    )
    missing = validate_manifest(provisional)
    delivery_missing = validate_delivery_artifacts(short_dir, out_dir, plain_text_path)
    release_blockers = []
    if not quality_summary["release_ready"]:
        release_blockers.append("quality_gate_not_release_ready")
    if args.require_quality_gate and native_review_summary["status"] != "verified":
        release_blockers.append("native_review_not_release_ready")
    release_blockers.extend(f"manifest_missing:{field}" for field in missing)
    release_blockers.extend(f"delivery_missing:{name}" for name in delivery_missing)
    manifest = build_manifest(
        meta, args.story_id, len(chapters), total_words, epub_path, "", quality_summary, native_review_summary,
        release_status="release_ready" if not release_blockers else "blocked",
        release_blockers=release_blockers, plain_text_path=plain_text_path,
    )
    if missing:
        print(f"[FAIL] manifest missing fields: {missing} (not publishing)")
    if delivery_missing:
        print(f"[FAIL] delivery missing Chinese artifacts: {delivery_missing}")
    else:
        print("[2/2] manifest and Chinese delivery artifacts valid")

    json_path = os.path.join(out_dir, "publish_manifest.json")
    json.dump(manifest, open(json_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    # 人类可读版
    md_lines = [f"# {manifest['title']}", "", f"## Synopsis", manifest['synopsis'], "",
                "## Selling Points"]
    md_lines += [f"- {s}" for s in manifest['sellingPoints']]
    md_lines += ["", "## Keywords", ", ".join(manifest['keywords']), "",
                 f"- Chapters: {manifest['chapters']}", f"- Total words: {manifest['totalWords']}",
                 f"- AI disclosure: {'yes' if manifest['aiDisclosure'] else 'no'}",
                 f"- TXT: {manifest['plainTextPath'] or 'missing'}",
                 f"- EPUB: {manifest['epubPath'] or 'skipped'}",
                 f"- Release status: {manifest['releaseStatus']}"]
    if manifest["releaseBlockers"]:
        md_lines += ["- Release blockers: " + ", ".join(manifest["releaseBlockers"])]
    open(os.path.join(out_dir, "publish_manifest.md"), "w", encoding="utf-8").write("\n".join(md_lines))

    print(f"\nDONE: {out_dir}")
    problems = missing + delivery_missing
    print(f"  - publish_manifest.json ({'valid' if not problems else 'INCOMPLETE: ' + str(problems)})")
    print(f"  - publish_manifest.md")
    print(f"  - TXT ({'present' if not 'plain_text' in delivery_missing else 'MISSING'})")
    print(f"  - summary_cn.md ({'present' if not 'summary_cn.md' in delivery_missing else 'MISSING'})")
    print(f"  - promotion_cn.md ({'present' if os.path.isfile(os.path.join(out_dir, 'promotion_cn.md')) else 'optional'})")
    return package_exit_code(release_blockers)


if __name__ == "__main__":
    raise SystemExit(main() or 0)
