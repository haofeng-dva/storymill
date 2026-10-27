# -*- coding: utf-8 -*-
"""Coordinate-first repair preparation. This module never edits chapters."""
import argparse
import hashlib
import json
import re
from pathlib import Path

from engine import LLM, load_env, load_json
from repair_validator import local_validate


ROOT = Path(__file__).resolve().parent
SCORE_LINE = re.compile(r"^\s*[-*]?\s*`?(?:native|engagement)_score\b", re.I)
SCORE_TAIL = re.compile(
    r"(?:^|\n)\s*(?:[-*]\s*)?`?(?:native|engagement)_score`?\s*:\s*[^\n]*"
    r"|\s+`?(?:native|engagement)_score`?\s*:\s*[^\n]*",
    re.I,
)
ISSUE_START = re.compile(r"(?m)^\s*(?:\d+\.|[-*])\s+")
QUOTED = re.compile(r"[\"“]([^\"”]{4,700})[\"”]|`([^`]{4,700})`")


def file_sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def line_number(text, offset):
    return text.count("\n", 0, offset) + 1


def chapter_number(filename):
    match = re.fullmatch(r"ch(\d+)\.md", filename or "")
    return int(match.group(1)) if match else 0


def split_issues(content):
    """Return reviewer findings while excluding score lines."""
    # Reviewers sometimes append score fields to the final issue rather than
    # placing them on their own line. They are metadata, never repair evidence.
    content = SCORE_TAIL.sub("", content or "")
    content = re.sub(r"(?im)^\s*\*{0,2}scores\*{0,2}\s*$", "", content)
    starts = list(ISSUE_START.finditer(content or ""))
    issues = []
    for index, start in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(content)
        item = re.sub(r"\s+", " ", content[start.end():end]).strip()
        if item and not SCORE_LINE.match(item):
            issues.append(item)
    return issues


def quoted_terms(issue):
    terms = []
    for match in QUOTED.finditer(issue or ""):
        term = (match.group(1) or match.group(2) or "").strip()
        if term and term not in terms:
            terms.append(term)
    return terms


def locate_term(text, term):
    words = re.split(r"\s+", term.strip())
    pattern = r"\s+".join(re.escape(word) for word in words if word)
    return list(re.finditer(pattern, text, re.I)) if pattern else []


def context_for(text, start, end, radius=2):
    lines = text.splitlines()
    first, last = line_number(text, start), line_number(text, end)
    before, after = max(0, first - 1 - radius), min(len(lines), last + radius)
    return {
        "line_start": first,
        "line_end": last,
        "excerpt": text[start:end],
        "context_before": lines[before:first - 1],
        "context_after": lines[last:after],
    }


def locate_issue(text, issue):
    """Keep only unique exact quote anchors; ambiguity is deferred safely."""
    anchors = []
    for term in quoted_terms(issue):
        matches = locate_term(text, term)
        if len(matches) == 1:
            match = matches[0]
            anchor = context_for(text, match.start(), match.end())
            anchor["quoted_term"] = term
            anchors.append(anchor)
    return anchors


def anchors_from_excerpts(text, excerpts):
    """Accept only exact, unique excerpts returned by the model locator."""
    anchors = []
    for excerpt in excerpts or []:
        if not isinstance(excerpt, str) or not excerpt.strip():
            continue
        matches = list(re.finditer(re.escape(excerpt.strip()), text))
        if len(matches) != 1:
            continue
        match = matches[0]
        anchor = context_for(text, match.start(), match.end())
        anchor["quoted_term"] = excerpt.strip()
        anchors.append(anchor)
    return anchors


MODEL_LOCATOR_SYSTEM = (
    "You locate evidence for one fiction-review finding. Return JSON only. "
    "Do not propose edits or infer missing text. Copy one or more exact passages "
    "from the numbered source that prove the issue; use [] if the source does not "
    "support it."
)


def model_locate_card(llm, card, text):
    """Use the small reviewer only to find anchors; program verifies every byte."""
    numbered_source = "\n".join(
        f"{number:04d}: {line}" for number, line in enumerate(text.splitlines(), 1)
    )
    prompt = {
        "task": "Return exactly {excerpts: [exact source passages], reason: string} as JSON.",
        "review_issue": card["review_issue"],
        "source": numbered_source,
        "rules": [
            "Each excerpt must be copied exactly from the source, without line numbers.",
            "Return at most three short excerpts.",
            "Return [] when the issue is only a broad style opinion or cannot be localized.",
        ],
    }
    result = llm.chat("reviewer", [
        {"role": "system", "content": MODEL_LOCATOR_SYSTEM},
        {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
    ], max_tokens=700, temperature=0.0)
    if "error" in result:
        return {"locator_status": "model_location_error", "locator_error": result["error"]}
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", result.get("content", "").strip(), flags=re.I)
    try:
        located = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return {"locator_status": "model_location_error", "locator_error": f"invalid locator JSON: {exc}"}
    excerpts = located.get("excerpts", []) if isinstance(located, dict) else []
    anchors = anchors_from_excerpts(text, excerpts)
    if not anchors:
        return {"locator_status": "needs_human_location", "locator_note": located.get("reason", "") if isinstance(located, dict) else ""}
    return {"locator_status": "model_located", "anchors": anchors, "locator_note": located.get("reason", "")}


def build_cards(report_path, story_dir):
    report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    cards = []
    for row in report:
        filename = row.get("file", "")
        chapter_path = Path(story_dir) / filename
        if not chapter_path.is_file():
            continue
        text = chapter_path.read_text(encoding="utf-8")
        for ordinal, issue in enumerate(split_issues(row.get("content", "")), 1):
            anchors = locate_issue(text, issue)
            cards.append({
                "id": f"{Path(filename).stem}-i{ordinal:02d}",
                "chapter": chapter_number(filename),
                "file": filename,
                "file_sha256": file_sha256(text),
                "review_issue": issue,
                "quoted_terms": quoted_terms(issue),
                "anchors": anchors,
                "locator_status": "located" if anchors else "needs_model_location",
                "planning_status": "pending",
            })
    return {
        "schema": "repair_cards_v1",
        "source_report": str(Path(report_path).resolve()),
        "story_dir": str(Path(story_dir).resolve()),
        "cards": cards,
    }


def build_cards_from_queue(queue_path, story_dir):
    """Convert deterministic/reviewer queue groups into normal anchored cards.

    This keeps the model-facing format identical regardless of whether an issue
    originated in a reviewer, a local rule, or a fact contract.
    """
    queue = json.loads(Path(queue_path).read_text(encoding="utf-8"))
    cards = []
    for group in queue.get("groups", []):
        filename = next((card.get("file") for card in group.get("cards", []) if card.get("file")), "")
        path = Path(story_dir) / filename
        if not filename or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        anchors, seen = [], set()
        messages, canonical_facts = [], []
        for item in group.get("cards", []):
            message = item.get("message") or item.get("review_issue")
            if message and message not in messages:
                messages.append(message)
            if item.get("canonical_fact") and item["canonical_fact"] not in canonical_facts:
                canonical_facts.append(item["canonical_fact"])
            item_anchors = item.get("anchors", [])
            if not item_anchors and item.get("excerpt"):
                item_anchors = [{
                    key: item[key] for key in (
                        "line_start", "line_end", "excerpt", "context_before", "context_after"
                    ) if key in item
                }]
            for anchor in item_anchors:
                excerpt = anchor.get("excerpt")
                if excerpt and excerpt not in seen and text.count(excerpt) == 1:
                    seen.add(excerpt)
                    anchors.append(anchor)
        cards.append({
            "id": group.get("id"),
            "chapter": group.get("chapter", chapter_number(filename)),
            "file": filename,
            "file_sha256": file_sha256(text),
            "review_issue": " ".join(messages),
            "category": group.get("category"),
            "severity": group.get("severity"),
            "root_cause_key": group.get("root_cause_key"),
            "canonical_facts": canonical_facts,
            "anchors": anchors,
            "locator_status": "located" if anchors else "needs_model_location",
            "planning_status": "pending",
        })
    return {
        "schema": "repair_cards_v2",
        "source_queue": str(Path(queue_path).resolve()),
        "story_dir": str(Path(story_dir).resolve()),
        "cards": cards,
    }


def resume_cards(payload, out_path):
    """Reuse only outputs tied to the unchanged source hash and card id."""
    out_path = Path(out_path)
    if not out_path.is_file():
        return payload
    try:
        existing = json.loads(out_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return payload
    previous = {card.get("id"): card for card in existing.get("cards", [])}
    preserved = (
        "anchors", "locator_status", "locator_note", "locator_error",
        "planning_status", "planning_reason", "plan", "error",
        "draft_status", "draft", "reason",
    )
    for card in payload["cards"]:
        old = previous.get(card["id"])
        if not old or old.get("file_sha256") != card["file_sha256"]:
            continue
        # The current deterministic locator is authoritative when it succeeds.
        if not card["anchors"]:
            for key in preserved:
                if key in old:
                    card[key] = old[key]
        else:
            for key in preserved[4:]:
                if key in old:
                    card[key] = old[key]
    return payload


PLANNER_SYSTEM = (
    "You are a lightweight fiction repair planner. Return JSON only. "
    "Do not rewrite prose or invent facts. Classify the issue and produce a "
    "narrow plan that preserves plot, character agency, timeline, and world rules."
)


def plan_card(llm, card):
    if not card.get("anchors"):
        return {"planning_status": "deferred", "planning_reason": "no verified source anchor"}
    prompt = {
        "task": "Classify and plan one repair. Return exactly the required JSON object.",
        "required_shape": {
            "category": "continuity|science_medical|motivation_consent|language_clarity|style_pacing",
            "severity": "blocking|important|polish",
            "root_cause_key": "short stable key",
            "canonical_fact": "fact to preserve or decide",
            "change_strategy": "delete|replace|add_clarification|defer",
            "allowed_scope": "one sentence|one paragraph|multi_chapter",
            "dependencies": ["facts or repair card ids"],
            "instructions_for_editor": "specific and concise",
            "requires_human_decision": False,
        },
        "review_issue": card["review_issue"],
        "fixed_story_facts": card.get("canonical_facts", []),
        "anchors": card.get("anchors", []),
        "rule": "Use defer and requires_human_decision=true when the correct story fact is not established.",
    }
    result = llm.chat("reviewer", [
        {"role": "system", "content": PLANNER_SYSTEM},
        {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
    ], max_tokens=900, temperature=0.0)
    if "error" in result:
        return {"planning_status": "error", "error": result["error"]}
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", result.get("content", "").strip(), flags=re.I)
    try:
        plan = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return {"planning_status": "error", "error": f"invalid planner JSON: {exc}", "raw": raw[:1000]}
    return {"planning_status": "planned", "plan": plan} if isinstance(plan, dict) else {
        "planning_status": "error", "error": "planner returned non-object JSON"
    }


DRAFTER_SYSTEM = (
    "You are a precision fiction editor. Return JSON only. Make a single local "
    "replacement inside the supplied anchor. Preserve plot, facts, names, timing, "
    "and unaffected prose. Never add characters, scenes, or outside explanations."
)


def draft_card(llm, card):
    if len(card.get("anchors", [])) != 1:
        return {"draft_status": "deferred", "reason": "requires exactly one verified anchor"}
    plan = card.get("plan", {})
    if plan.get("requires_human_decision") or plan.get("allowed_scope") == "multi_chapter":
        return {"draft_status": "deferred", "reason": "requires cross-chapter or human fact decision"}
    anchor = card["anchors"][0]
    prompt = {
        "task": "Return exactly {find, replace, reason} as JSON.",
        "review_issue": card["review_issue"],
        "repair_plan": plan,
        "anchor": anchor,
        "constraints": [
            "find must equal anchor.excerpt exactly",
            "replace must be local and grammatical",
            "do not exceed the supplied anchor and context",
            "do not change plot or introduce facts",
        ],
    }
    result = llm.chat("writer", [
        {"role": "system", "content": DRAFTER_SYSTEM},
        {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
    ], max_tokens=900, temperature=0.15)
    if "error" in result:
        return {"draft_status": "error", "error": result["error"]}
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", result.get("content", "").strip(), flags=re.I)
    try:
        draft = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return {"draft_status": "error", "error": f"invalid draft JSON: {exc}", "raw": raw[:1000]}
    if not isinstance(draft, dict) or draft.get("find") != anchor["excerpt"]:
        return {"draft_status": "rejected", "reason": "draft find does not equal verified anchor"}
    errors = local_validate(anchor["excerpt"], draft.get("replace", ""))
    if errors:
        return {"draft_status": "rejected", "reason": "; ".join(errors)}
    return {"draft_status": "drafted", "draft": draft}


def save_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Anchored repair planning; never applies chapter edits")
    parser.add_argument("--story-id", required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--report", help="Review JSON relative to the project root or absolute")
    source.add_argument("--queue", help="Merged repair queue JSON relative to the project root or absolute")
    parser.add_argument("--out", default="", help="Repair cards JSON output path")
    parser.add_argument("--locate-model", action="store_true", help="Use the reviewer to locate unresolved issues in numbered source")
    parser.add_argument("--plan", action="store_true", help="Use the lightweight reviewer to create repair plans")
    parser.add_argument("--draft", action="store_true", help="Use the writer for local replacement drafts")
    parser.add_argument("--limit", type=int, default=0, help="Maximum cards sent to a model; 0 means all")
    parser.add_argument("--ids", default="", help="Optional comma-separated repair card IDs")
    parser.add_argument("--resume", action="store_true", help="Resume matching cards from an existing output file")
    args = parser.parse_args()
    story_dir = ROOT / "shorts" / args.story_id
    out = Path(args.out) if args.out else ROOT / "out" / args.story_id / "repair_cards.json"
    if not out.is_absolute():
        out = ROOT / out
    if args.queue:
        queue = Path(args.queue)
        if not queue.is_absolute():
            queue = ROOT / queue
        payload = build_cards_from_queue(queue, story_dir)
    else:
        report = Path(args.report)
        if not report.is_absolute():
            report = ROOT / report
        payload = build_cards(report, story_dir)
    if args.resume:
        payload = resume_cards(payload, out)
    if args.locate_model or args.plan or args.draft:
        load_env()
        llm = LLM(load_json(ROOT / "config.json"))
    if args.locate_model:
        unresolved = [card for card in payload["cards"] if not card["anchors"]]
        for card in (unresolved[:args.limit] if args.limit else unresolved):
            chapter_text = (story_dir / card["file"]).read_text(encoding="utf-8")
            card.update(model_locate_card(llm, card, chapter_text))
            save_json(out, payload)
    selected = [card for card in payload["cards"] if card.get("anchors")]
    selected_ids = {item.strip() for item in args.ids.split(",") if item.strip()}
    if selected_ids:
        selected = [card for card in selected if card["id"] in selected_ids]
    if args.limit:
        selected = selected[:args.limit]
    if args.plan:
        for card in [card for card in selected if card.get("planning_status") in {"pending", "error", "deferred"}]:
            card.update(plan_card(llm, card))
            save_json(out, payload)
    if args.draft:
        for card in [
            card for card in selected
            if card.get("planning_status") == "planned"
            and card.get("draft_status") not in {"drafted", "rejected", "deferred"}
        ]:
            card.update(draft_card(llm, card))
            save_json(out, payload)
    save_json(out, payload)
    located = sum(bool(card["anchors"]) for card in payload["cards"])
    print(json.dumps({"cards": len(payload["cards"]), "located": located, "output": str(out)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
