# -*- coding: utf-8 -*-
"""Apply only verified local repair drafts, with a per-run backup and audit."""
import argparse
import json
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from repair_pipeline import file_sha256
from repair_validator import local_validate
import quality_gate
from engine import assemble_full_book
from writing_status import load_status


ROOT = Path(__file__).resolve().parent


def _load_json(path):
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _chapter_no(filename):
    match = re.fullmatch(r"ch(\d+)\.md", filename)
    return int(match.group(1)) if match else None


def revision_evidence(story_dir, filename, before, after):
    """Build a non-model revision decision before replacing a chapter file."""
    chapter_no = _chapter_no(filename)
    if chapter_no is None:
        return None, None
    report_path = story_dir / f"ch{chapter_no:04d}.quality_gate.json"
    before_report = _load_json(report_path) or quality_gate.build_report(before, chapter_no)
    # A local repair does not rerun blind review. Preserve its last evidence
    # until an explicit review replaces it.
    after_report = quality_gate.build_report(
        after, chapter_no, blind=before_report.get("blind_review", {}),
        local_warnings=before_report.get("local_warnings", []),
    )
    state_path = story_dir / "control" / "state" / f"state_delta_ch{chapter_no:04d}.json"
    before_state = _load_json(state_path)
    manifest = quality_gate.build_revision_manifest(
        chapter_no, before, after,
        before_report=before_report, after_report=after_report,
        before_state=before_state,
    )
    return manifest, after_report


def eligible(card):
    plan = card.get("plan", {})
    draft = card.get("draft", {})
    anchors = card.get("anchors", [])
    return (
        card.get("draft_status") == "drafted"
        and len(anchors) == 1
        and draft.get("find") == anchors[0].get("excerpt")
        and draft.get("find") != draft.get("replace")
        and plan.get("change_strategy") in {"replace", "delete"}
        and plan.get("allowed_scope") in {"one sentence", "one paragraph"}
        and not plan.get("requires_human_decision")
        and not plan.get("dependencies")
    )


def apply_cards_to_text(text, cards):
    """Return a candidate text and audit records without mutating any file."""
    original = text
    audit = []
    for card in cards:
        draft = card["draft"]
        find, replace = draft["find"], draft.get("replace", "")
        if text.count(find) != 1:
            audit.append({"id": card["id"], "status": "rejected", "reason": "find is not unique in current chapter"})
            continue
        text = text.replace(find, replace, 1)
        audit.append({"id": card["id"], "status": "applied", "reason": draft.get("reason", "")})
    errors = local_validate(original, text)
    if errors:
        return original, [{"id": card["id"], "status": "rejected", "reason": "; ".join(errors)} for card in cards]
    return text, audit


def main():
    parser = argparse.ArgumentParser(description="Apply verified repair drafts only")
    parser.add_argument("--story-id", required=True)
    parser.add_argument("--cards", required=True, help="Repair cards JSON")
    parser.add_argument("--ids", default="", help="Optional comma-separated repair card IDs")
    parser.add_argument("--out", default="", help="Apply audit output path")
    args = parser.parse_args()

    cards_path = Path(args.cards)
    if not cards_path.is_absolute():
        cards_path = ROOT / cards_path
    payload = json.loads(cards_path.read_text(encoding="utf-8"))
    selected_ids = {item.strip() for item in args.ids.split(",") if item.strip()}
    cards = [card for card in payload.get("cards", []) if eligible(card)]
    if selected_ids:
        cards = [card for card in cards if card["id"] in selected_ids]
    story_dir = ROOT / "shorts" / args.story_id
    by_file = defaultdict(list)
    for card in cards:
        by_file[card["file"]].append(card)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = ROOT / "out" / args.story_id / "repair_backups" / timestamp
    candidate_dir = story_dir / "repair_candidates" / timestamp
    result = {"schema": "repair_apply_v1", "cards_source": str(cards_path.resolve()), "backup_dir": str(backup_dir), "files": []}
    for filename, file_cards in by_file.items():
        path = story_dir / filename
        text = path.read_text(encoding="utf-8")
        expected = {card["file_sha256"] for card in file_cards}
        if len(expected) != 1 or file_sha256(text) not in expected:
            result["files"].append({"file": filename, "status": "rejected", "reason": "source hash changed", "cards": [card["id"] for card in file_cards]})
            continue
        candidate, audit = apply_cards_to_text(text, file_cards)
        applied = [row for row in audit if row["status"] == "applied"]
        if not applied:
            result["files"].append({"file": filename, "status": "rejected", "cards": audit})
            continue
        manifest, after_report = revision_evidence(story_dir, filename, text, candidate)
        if manifest and manifest["decision"] != "accepted":
            candidate_dir.mkdir(parents=True, exist_ok=True)
            candidate_path = candidate_dir / filename
            manifest_path = candidate_dir / f"{Path(filename).stem}.revision.json"
            report_path = candidate_dir / f"{Path(filename).stem}.quality_gate.json"
            candidate_path.write_text(candidate, encoding="utf-8")
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            report_path.write_text(json.dumps(after_report, ensure_ascii=False, indent=2), encoding="utf-8")
            result["files"].append({
                "file": filename, "status": "revalidation_required", "cards": audit,
                "candidate": str(candidate_path), "revision_manifest": str(manifest_path),
                "reason": "; ".join(manifest["blockers"]),
            })
            continue
        backup_dir.mkdir(parents=True, exist_ok=True)
        (backup_dir / filename).write_text(text, encoding="utf-8")
        path.write_text(candidate, encoding="utf-8")
        if manifest:
            (story_dir / f"{Path(filename).stem}.quality_gate.json").write_text(
                json.dumps(after_report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            (story_dir / f"{Path(filename).stem}.revision.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        result["files"].append({"file": filename, "status": "applied", "cards": audit, "revision": manifest})

    applied_files = sum(row["status"] == "applied" for row in result["files"])
    if applied_files:
        try:
            chapter_count = int(load_status(story_dir)["expected_chapters"])
            result["full_book"] = assemble_full_book(str(story_dir), chapter_count)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            result["full_book_error"] = str(exc)

    out = Path(args.out) if args.out else ROOT / "out" / args.story_id / f"repair_apply_{timestamp}.json"
    if not out.is_absolute():
        out = ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {
        "files_applied": applied_files,
        "drafts_applied": sum(sum(card["status"] == "applied" for card in row.get("cards", [])) for row in result["files"]),
        "output": str(out),
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if applied_files and "full_book_error" not in result else 2


if __name__ == "__main__":
    raise SystemExit(main())
