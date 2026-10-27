# -*- coding: utf-8 -*-
"""Small per-book memory ledger for continuity-aware generation and repair."""
import hashlib, json, os, re, time

class BookMemory:
    def __init__(self, story_dir):
        self.root = os.path.join(story_dir, "memory")
        self.chapters = os.path.join(self.root, "chapters")
        os.makedirs(self.chapters, exist_ok=True)

    @staticmethod
    def _entities(text):
        # Conservative proper-name capture; no model call and no invented facts.
        names = sorted(set(re.findall(r"\b[A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,})?\b", text or "")))
        return names[:80]

    def save_chapter(self, chapter_no, text, handoff="", outline=""):
        payload = {
            "chapter": int(chapter_no),
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "sha256": hashlib.sha256((text or "").encode("utf-8")).hexdigest(),
            "word_count": len(re.findall(r"[A-Za-z']+", text or "")),
            "ending_excerpt": re.sub(r"\s+", " ", text or "")[-1200:],
            "handoff": handoff or "",
            "entities": self._entities(text),
            "outline_excerpt": (outline or "")[:1200],
        }
        path = os.path.join(self.chapters, f"ch{int(chapter_no):04d}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        return payload

    def load_chapter(self, chapter_no):
        path = os.path.join(self.chapters, f"ch{int(chapter_no):04d}.json")
        if not os.path.exists(path):
            return {}
        try:
            return json.load(open(path, encoding="utf-8"))
        except Exception:
            return {}

    def bootstrap_existing(self, chapter_dir, max_chapters=200):
        """Create minimal memory cards for legacy runs without re-calling a model."""
        made = 0
        for name in sorted(os.listdir(chapter_dir)) if os.path.isdir(chapter_dir) else []:
            m = re.fullmatch(r"ch(\d{4})\.md", name)
            if not m or int(m.group(1)) > max_chapters:
                continue
            n = int(m.group(1))
            if self.load_chapter(n):
                continue
            text = open(os.path.join(chapter_dir, name), encoding="utf-8").read()
            self.save_chapter(n, text, "", "")
            made += 1
        return made
    def context_block(self, chapter_no):
        prev = self.load_chapter(int(chapter_no) - 1)
        if not prev:
            return ""
        return (
            "Book memory from the immediately previous chapter (continuity authority):\n"
            f"Ending excerpt: {prev.get('ending_excerpt','')}\n"
            f"Handoff: {prev.get('handoff','')}\n"
            f"Known entities: {', '.join(prev.get('entities',[]))}"
        )

    def save_repair(self, chapter_no, issue_ids, patches, before_hash, after_text, status="accepted", reason=""):
        path = os.path.join(self.root, "repair_history.jsonl")
        row = {
            "chapter": int(chapter_no),
            "issue_ids": issue_ids,
            "patches_applied": len(patches),
            "before_sha256": before_hash,
            "after_sha256": hashlib.sha256((after_text or "").encode("utf-8")).hexdigest(),
            "status": status,
            "reason": reason,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
