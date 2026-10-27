# -*- coding: utf-8 -*-
"""Standalone, candidate-only AI final editor. It never mutates source stories."""
import argparse
import copy
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from engine import LLM, load_env, load_json
from final_editor_candidate import FinalEditorCandidateError, apply_repair_wave, build_candidate
from final_editor_contract import build_contract, count_words, sha256_text
from final_editor_dossier import FinalEditorDossierError, quote_anchor, verify_dossier
from final_editor_plan import FinalEditorPlanError, verify_plan
from final_editor_verify import FinalEditorVerificationError, verify_candidate
from story_fact_contract import check_story


ROOT = Path(__file__).resolve().parent
CHAPTER_FILE = re.compile(r"ch\d{4}\.md$")
JSON_RESPONSE_ATTEMPTS = 3
UNRESOLVED_REPAIR_ROUNDS = 2

DOSSIER_SYSTEM = (
    "You are a senior fiction editor. Diagnose this specific book, not a generic genre template. "
    "Return JSON only. Every chapter role, protected fact, and finding must cite exact source text. "
    "Do not propose prose edits. Do not invent facts."
)
DOSSIER_CHAPTER_SYSTEM = (
    "You are a senior fiction editor extracting evidence from one chapter of a specific book. "
    "Return JSON only. Quote source text exactly. Do not propose edits or invent events. "
    "Identify only facts and defects that this chapter itself supports. Keep the response compact."
)
PLAN_SYSTEM = (
    "You are the final decision-making fiction editor for this book. Return JSON only. "
    "Every verified finding must receive one bounded candidate-only repair. Resolve continuity conflicts yourself from the "
    "book evidence, outline, chapter roles, protected facts, and downstream consequences. Do not defer normal editorial "
    "judgment to an operator. Do not add chapters, change ending facts, or expand a finding into unrelated chapters. "
    "Follow the supplied JSON schema and field types exactly."
)
EDITOR_SYSTEM = (
    "You are a precision final fiction editor. Return JSON only. Edit only the supplied target chapters. "
    "Preserve the supplied protected facts, narrative voice, POV, tense, and word budget. "
    "Return complete revised chapter text, never commentary outside JSON."
)
REVIEW_SYSTEM = (
    "You are an independent fiction editor reviewing a revised candidate against its original editorial findings. "
    "Return JSON only. Do not praise the draft. Mark every planned finding resolved or unresolved. "
    "List only real blocking defects."
)


def now_run_id():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def read_chapters(story_dir):
    root = Path(story_dir)
    texts = {}
    for path in sorted(root.glob("ch*.md")):
        if CHAPTER_FILE.fullmatch(path.name):
            texts[path.name] = path.read_text(encoding="utf-8")
    if not texts:
        raise ValueError(f"no chapters found in {root}")
    return texts


def optional_artifacts(story_dir, review_path=None):
    root = Path(story_dir)
    candidates = {
        "outline": root / "outline.md",
        "world_bible": root / "world_bible.md",
        "story_facts": root / "story_facts.json",
    }
    if review_path:
        candidates["review_report"] = Path(review_path)
    artifacts = {name: path.read_text(encoding="utf-8") for name, path in candidates.items() if path.is_file()}
    authority = _planning_authority(root)
    if authority:
        artifacts["planning_authority"] = authority
    return artifacts


def _planning_authority(story_dir):
    """Extract the compact canonical plan the final editor must preserve.

    This consumes existing planning/control artifacts only. It never invents
    canon and does not mutate a source story.
    """
    root = Path(story_dir)
    for name in ("control", "control_20261019"):
        contracts = root / name / "chapter_contracts.json"
        ledger = root / name / "scene_ledger.json"
        cards = root / name / "character_voice_cards.json"
        if not (contracts.is_file() and ledger.is_file() and cards.is_file()):
            continue
        try:
            contract_data = json.loads(contracts.read_text(encoding="utf-8"))
            ledger_data = json.loads(ledger.read_text(encoding="utf-8"))
            card_data = json.loads(cards.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        facts = []
        for key, chapter in sorted(contract_data.get("chapters", {}).items()):
            for fact in chapter.get("facts", []):
                if all(isinstance(fact.get(field), str) and fact[field].strip() for field in ("id", "claim", "value")):
                    facts.append({"chapter": key, "id": fact["id"], "claim": fact["claim"], "value": fact["value"]})
        scenes = []
        for key, rows in sorted(ledger_data.get("chapters", {}).items()):
            for scene in rows:
                scenes.append({
                    "chapter": key,
                    "scene_id": scene.get("scene_id"),
                    "result": scene.get("result"),
                    "irreversible_change": scene.get("irreversible_change"),
                    "participants": scene.get("participants", []),
                })
        return json.dumps({
            "canonical_characters": [card.get("name") for card in card_data.get("characters", []) if card.get("name")],
            "canonical_facts": facts,
            "chapter_outcomes": scenes,
        }, ensure_ascii=False, separators=(",", ":"))[:12000]
    return ""


def write_json(path, value):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_json(content):
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", (content or "").strip(), flags=re.I)
    try:
        return json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"model returned invalid JSON: {exc}") from exc


def call_json(llm, role, system, payload, max_tokens):
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
    last_error = None
    for attempt in range(1, JSON_RESPONSE_ATTEMPTS + 1):
        result = llm.chat(role, messages, max_tokens=max_tokens, temperature=0.15, json_mode=True)
        if "error" in result:
            raise RuntimeError(result["error"])
        try:
            return parse_json(result.get("content"))
        except ValueError as exc:
            last_error = exc
            if attempt == JSON_RESPONSE_ATTEMPTS:
                raise
            messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                {
                    "role": "user",
                    "content": (
                        "Your previous response was invalid JSON. Return the same requested object again as strict "
                        f"valid JSON only. Parser error: {exc}"
                    ),
                },
            ]
    raise last_error or ValueError("model returned no JSON")


def _numbered_book(chapter_texts):
    return "\n\n".join(f"--- {filename} ---\n{text}" for filename, text in sorted(chapter_texts.items()))


def review_findings_for_chapter(source_artifacts, filename):
    """Expose prior chapter-review leads without treating them as source truth."""
    raw = source_artifacts.get("review_report", "")
    try:
        report = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(report, list):
        return []
    for row in report:
        if not isinstance(row, dict) or row.get("file") != filename:
            continue
        findings = row.get("editorial_findings", [])
        if not isinstance(findings, list):
            return []
        return [item.strip() for item in findings if isinstance(item, str) and item.strip()]
    return []


def dossier_chapter_request(contract, filename, text, source_artifacts):
    chapter = int(filename[2:6])
    return {
        "task": "Return chapter evidence JSON with chapter, chapter_role, protected_facts, and findings.",
        "story_id": contract["story_id"],
        "chapter": chapter,
        "file": filename,
        "locked_invariants": contract["locked_invariants"],
        "story_facts": source_artifacts.get("story_facts", ""),
        "planning_authority": source_artifacts.get("planning_authority", ""),
        "external_review_leads": review_findings_for_chapter(source_artifacts, filename),
        "rules": [
            "chapter_role is one object with chapter, function, and evidence_quotes.",
            "Return at most one protected_fact: the most important source-supported fact that editing must not change.",
            "Each evidence_quotes item is {file, text} copied verbatim from source and no longer than 180 characters.",
            "Every evidence quote must occur exactly once in its declared source chapter. If a short line repeats, include enough adjacent source text to make the quote unique.",
            "Return every material finding supported by this chapter's exact source text. Do not cap the number merely for brevity; return [] only if none are supported.",
            "Keep function, claim, root_cause, and impact each under 220 characters.",
            "Every finding must cite only this chapter and use repair_scope local or scene.",
            "external_review_leads are unverified leads, not facts. Use one only when this chapter's exact source text supports it; otherwise ignore it.",
            "requires_human_decision must be false: whole-book editorial resolution happens in the repair plan, not by an operator.",
            "Planning authority is canonical when supplied: flag a conflict with its names, literal facts, scene outcomes, or relationships.",
        ],
        "chapter_text": text,
    }


def dossier_request(contract, chapter_packets, source_artifacts=None, correction=None):
    source_artifacts = source_artifacts or {}
    payload = {
        "task": "Return a whole-book dossier JSON object with story_id, book_promise, voice_profile, chapter_roles, protected_facts, and findings.",
        "story_id": contract["story_id"],
        "contract": {"locked_invariants": contract["locked_invariants"], "word_budget": contract["word_budget"]},
        "chapter_evidence_packets": chapter_packets,
        "planning_authority": source_artifacts.get("planning_authority", ""),
        "rules": [
            "Create exactly one chapter_role for every chapter using the supplied packet evidence.",
            "Copy evidence_quotes only from the supplied packet evidence; never invent or paraphrase a quote.",
            "Keep each selected evidence quote unchanged and no longer than 180 characters.",
            "Use only evidence quotes that occur exactly once in their declared source chapter; do not select a repeated short line unless its packet quote includes unique adjacent context.",
            "Select every material, source-supported finding from the chapter packets. Do not impose a book-level finding cap.",
            "Merge related chapter findings into a multi_chapter root cause only when the packets support every cited chapter.",
            "Keep a finding local or scene when it belongs to one chapter; return [] if no supported finding exists.",
            "Retain protected facts needed to prevent edits from changing established plot, agency, or setting details.",
            "Return each protected_fact as {id, claim, chapters, evidence_quotes}; id must be an ASCII safe id such as fact_01.",
            "Return each finding as {id, root_cause, impact, chapters, evidence_quotes, repair_scope, requires_human_decision}; set requires_human_decision to false.",
            "Use the exact field name id, not fact_id, finding_id, identifier, or a display label.",
            "Evaluate the whole book automatically for five editorial dimensions: opening pressure, core conflict escalation, emotional and relationship turns, chapter-end pursuit hooks, and character agency. Return anchored findings for any failure; do not ask an operator to decide ordinary editorial questions.",
            "When planning_authority is supplied, treat its character names, literal facts, and chapter outcomes as canon and report anchored contradictions.",
        ],
    }
    if correction:
        payload["previous_validation_error"] = correction
        payload["retry_rules"] = [
            "Return the full dossier again, correcting the listed validation error.",
            "Top-level book_promise and voice_profile are mandatory non-empty strings, even when no findings are selected.",
        ]
    return payload


def _unique_packet_quotes(entries, source_texts):
    """Keep only exact, unique packet quotes before deterministic aggregation."""
    if not isinstance(entries, list):
        return []
    valid = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        filename, text = entry.get("file"), entry.get("text")
        if filename not in source_texts:
            continue
        anchor, _ = quote_anchor(source_texts[filename], text)
        if anchor:
            valid.append({"file": filename, "text": text})
    return valid


def dossier_from_chapter_packets(contract, chapter_packets, source_texts):
    """Build a verified full-book dossier from bounded chapter evidence.

    This replaces the oversized all-book aggregation prompt. Every retained
    item still passes the same exact-source-anchor verifier before planning.
    """
    roles, facts, findings = [], [], []
    for packet in sorted(chapter_packets, key=lambda item: item.get("chapter", 0)):
        if not isinstance(packet, dict) or not isinstance(packet.get("chapter"), int):
            continue
        chapter = packet["chapter"]
        role = packet.get("chapter_role", {})
        if isinstance(role, dict):
            quotes = _unique_packet_quotes(role.get("evidence_quotes"), source_texts)
            if quotes and isinstance(role.get("function"), str):
                roles.append({"chapter": chapter, "function": role["function"], "evidence_quotes": quotes})
        for index, fact in enumerate(packet.get("protected_facts", []), 1):
            if not isinstance(fact, dict):
                continue
            quotes = _unique_packet_quotes(fact.get("evidence_quotes"), source_texts)
            if quotes and isinstance(fact.get("claim"), str):
                facts.append({
                    "id": f"fact_{chapter:02d}_{index:02d}", "claim": fact["claim"],
                    "chapters": [chapter], "evidence_quotes": quotes,
                })
        for index, finding in enumerate(packet.get("findings", []), 1):
            if not isinstance(finding, dict):
                continue
            quotes = _unique_packet_quotes(finding.get("evidence_quotes"), source_texts)
            if (quotes and isinstance(finding.get("root_cause"), str)
                    and isinstance(finding.get("impact"), str)
                    and finding.get("repair_scope") in {"local", "scene"}):
                findings.append({
                    "id": f"finding_{chapter:02d}_{index:02d}",
                    "root_cause": finding["root_cause"], "impact": finding["impact"],
                    "chapters": [chapter], "evidence_quotes": quotes,
                    "repair_scope": finding["repair_scope"], "requires_human_decision": False,
                })
    proposal = {
        "story_id": contract["story_id"],
        "book_promise": "A source-anchored final edit preserving the finished book's plot, voice, and character agency.",
        "voice_profile": "Preserve the established close narrative voice, tense, point of view, and source-supported story facts.",
        "chapter_roles": roles,
        "protected_facts": facts,
        "findings": findings,
    }
    return verify_dossier(proposal, source_texts, contract)


def plan_request(contract, dossier, correction=None):
    payload = {
        "task": "Return a repair plan JSON object with story_id, dossier_contract_sha256, items, and deferred.",
        "story_id": contract["story_id"],
        "dossier_contract_sha256": dossier["contract_sha256"],
        "word_budget": contract["word_budget"],
        "required_verifications": contract["required_verifications"],
        "dossier": dossier,
        "rules": [
            "Every finding must appear exactly once in items. deferred must be an empty list.",
            "Return exactly the top-level fields story_id, dossier_contract_sha256, items, deferred. Do not add alternate plan shapes or nested candidate_repair fields.",
            "Every item must contain only id, finding_ids, chapter_targets, allowed_word_deltas, editorial_goal, expected_function_after, editor_instructions, editorial_decision, decision_basis, decision_confidence, validation_checks.",
            "Every item id must be a non-empty ASCII-safe identifier such as repair_01; do not use a display sentence, Chinese text, or omit id.",
            "An item can target only the chapters already cited by its linked findings.",
            "Do not create two items for the same chapter; merge their root causes.",
            "Make the editorial decision yourself. Resolve conflicting variants by prioritizing explicit source evidence, the outline, protected facts, and later chapter consequences.",
            "Every items[].finding_ids must be a non-empty unique sorted list of finding ids. deferred is always [].",
            "Every items[].chapter_targets must be the sorted complete set of linked finding chapter NUMBERS, for example [1, 3], never filenames such as ch0001.md.",
            "Every items[].allowed_word_deltas must contain exactly one string number key per target and a JSON decimal NUMBER from -0.25 to 0.25, for example {\"1\": -0.04}; never percentages, text, or ranges. Use deletion beyond 0.10 only when removing a verified duplicate alternate-draft scene.",
            "Every item must include editorial_decision, decision_basis, and decision_confidence (high, medium, or low).",
            "Every item must include concise plain-text editorial_goal, expected_function_after, and editor_instructions, plus validation_checks exactly matching required_verifications.",
        ],
        "required_item_example": {
            "id": "repair_01",
            "finding_ids": ["finding_01"],
            "chapter_targets": [1],
            "allowed_word_deltas": {"1": -0.04},
            "editorial_goal": "Remove the identified duplicate without changing the chapter outcome.",
            "expected_function_after": "The chapter contains one coherent version of the event.",
            "editor_instructions": "Retain the cited canonical beat and delete only its duplicate wording.",
            "editorial_decision": "The earlier cited version is canonical and the repeated variant is removed.",
            "decision_basis": "The earlier version matches the protected fact and later consequences.",
            "decision_confidence": "high",
            "validation_checks": contract["required_verifications"],
        },
    }
    if correction:
        payload["previous_validation_error"] = correction
        payload["retry_rules"] = [
            "Return the full repair plan again, correcting the listed validation error.",
            "Do not omit any finding; each has exactly one disposition.",
        ]
    return payload


def revision_request(contract, dossier, item, current_texts, correction=None):
    targets = {f"ch{chapter:04d}.md": current_texts[f"ch{chapter:04d}.md"] for chapter in item["chapter_targets"]}
    target_numbers = set(item["chapter_targets"])
    context_chapters = {}
    for chapter in target_numbers:
        for neighbor in (chapter - 1, chapter + 1):
            filename = f"ch{neighbor:04d}.md"
            if neighbor > 0 and neighbor not in target_numbers and filename in current_texts:
                context_chapters[filename] = current_texts[filename]
    payload = {
        "task": "Return {plan_id, chapters} where chapters maps every target file to {source_sha256, revised_text}.",
        "plan_id": item["id"],
        "item": item,
        "voice_profile": dossier["voice_profile"],
        "book_promise": dossier["book_promise"],
        "protected_facts": dossier.get("protected_facts", []),
        "chapter_roles": dossier["chapter_roles"],
        "targets": [{"file": filename, "source_sha256": sha256_text(text), "text": text} for filename, text in targets.items()],
        "adjacent_candidate_context": [
            {"file": filename, "text": context_chapters[filename]}
            for filename in sorted(context_chapters)
        ],
        "rules": [
            "Return every target and no non-target chapter.",
            "Keep each target within the supplied allowed_word_deltas budget.",
            "Implement item.editorial_decision using item.decision_basis as the authority for choosing among conflicting variants.",
            "Do not change protected facts, chapter order, POV, tense, or central resolution.",
            "Retain unaffected material and solve the plan item, not merely its wording.",
            "Use adjacent_candidate_context only to preserve handoffs; do not edit a context chapter.",
        ],
    }
    if correction:
        payload["previous_validation_error"] = correction
        payload["retry_rules"] = [
            "Return the complete revision JSON again, correcting the listed validation error.",
            "Do not weaken or defer the editorial decision; keep the repair inside every supplied target word delta.",
        ]
    return payload


def review_request(contract, dossier, plan, candidate_texts):
    expected = sorted({finding_id for item in plan["items"] for finding_id in item["finding_ids"]})
    protected_fact_ids = sorted(fact["id"] for fact in dossier.get("protected_facts", []))
    return {
        "task": "Return {reviewer_id, candidate_chapter_sha256, blocking_findings, finding_assessments, protected_fact_assessments}.",
        "reviewer_id": "independent_final_editor_review",
        "candidate_chapter_sha256": {name: sha256_text(text) for name, text in candidate_texts.items()},
        "planned_finding_ids": expected,
        "protected_fact_ids": protected_fact_ids,
        "original_dossier": dossier,
        "repair_plan": plan,
        "candidate_book": _numbered_book(candidate_texts),
        "rules": [
            "finding_assessments must include every planned finding exactly once.",
            "Each finding status is resolved or unresolved. Do not return needs_human_decision.",
            "protected_fact_assessments must include every protected fact exactly once.",
            "Each protected fact status is preserved or changed. Do not return needs_human_decision.",
            "blocking_findings is a list of concrete new defects; empty when none.",
        ],
    }


def review_request_for_chapter(contract, dossier, plan, candidate_texts, chapter):
    """Bound independent review to one changed narrative unit at a time."""
    filename = f"ch{chapter:04d}.md"
    chapter_items = [item for item in plan["items"] if chapter in item["chapter_targets"]]
    finding_ids = sorted({finding_id for item in chapter_items for finding_id in item["finding_ids"]})
    findings = [finding for finding in dossier["findings"] if finding["id"] in finding_ids]
    facts = [fact for fact in dossier.get("protected_facts", []) if chapter in fact["chapters"]]
    return {
        "task": "Return {reviewer_id, finding_assessments, protected_fact_assessments, blocking_findings} for this chapter only.",
        "reviewer_id": "independent_final_editor_chapter_review",
        "chapter": chapter,
        "candidate_chapter": {filename: candidate_texts[filename]},
        "candidate_chapter_sha256": {filename: sha256_text(candidate_texts[filename])},
        "findings": findings,
        "repair_items": chapter_items,
        "protected_facts": facts,
        "rules": [
            "Assess every supplied finding exactly once as resolved or unresolved.",
            "Assess every supplied protected fact exactly once as preserved or changed.",
            "blocking_findings contains only concrete defects visible in this supplied chapter.",
        ],
    }


def normalize_chapter_review_ids(review, finding_ids, protected_fact_ids):
    """Accept the compact ``id`` form occasionally returned by reviewers.

    The review prompt asks for ``finding_id`` and ``fact_id``, but some
    otherwise-valid JSON responses use the ubiquitous ``id`` key.  Normalize
    only IDs that are already expected for this chapter; an unknown ID remains
    invalid and is rejected by the final verifier.
    """
    if not isinstance(review, dict):
        return review
    normalized = copy.deepcopy(review)
    finding_set, fact_set = set(finding_ids), set(protected_fact_ids)
    for item in normalized.get("finding_assessments", []):
        if isinstance(item, dict) and "finding_id" not in item and item.get("id") in finding_set:
            item["finding_id"] = item["id"]
    for item in normalized.get("protected_fact_assessments", []):
        if isinstance(item, dict) and "fact_id" not in item:
            returned_id = item.get("protected_fact_id", item.get("id"))
            if returned_id in fact_set:
                item["fact_id"] = returned_id
    return normalized


def preserve_unchanged_fact_assessments(review, contract, dossier, candidate_texts):
    """Add deterministic preservation evidence for facts in untouched chapters.

    Independent chapter review intentionally runs only for chapters the repair
    plan changed.  A protected fact whose every source chapter has the exact
    contract hash in the candidate cannot have been altered, so it should not
    make an otherwise complete review fail merely because no model call was
    made for an untouched chapter.
    """
    if not isinstance(review, dict):
        return review
    normalized = copy.deepcopy(review)
    assessments = normalized.setdefault("protected_fact_assessments", [])
    seen = {item.get("fact_id") for item in assessments if isinstance(item, dict)}
    source_hashes = {item["file"]: item["source_sha256"] for item in contract["source"]["chapters"]}
    for fact in dossier.get("protected_facts", []):
        fact_id = fact["id"]
        if fact_id in seen:
            continue
        files = [f"ch{chapter:04d}.md" for chapter in fact["chapters"]]
        unchanged = all(
            filename in candidate_texts
            and source_hashes.get(filename) == sha256_text(candidate_texts[filename])
            for filename in files
        )
        if unchanged:
            assessments.append({
                "fact_id": fact_id,
                "status": "preserved",
                "evidence": "deterministic: every chapter containing this protected fact is byte-for-byte unchanged from the source contract",
            })
    return normalized


def review_candidate_by_chapter(llm, role, contract, dossier, plan, candidate_texts, artifact_dir=None):
    """Merge checkpointed independent chapter reviews into one verification shape."""
    checkpoint_path = Path(artifact_dir) / "independent_review_chapters.json" if artifact_dir else None
    saved = {}
    if checkpoint_path and checkpoint_path.is_file():
        try:
            rows = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            saved = {row["chapter"]: row for row in rows if isinstance(row, dict) and isinstance(row.get("chapter"), int)}
        except (OSError, ValueError, json.JSONDecodeError):
            saved = {}
    findings, facts, blockers = [], [], []
    reviewer_ids = []
    target_chapters = sorted({chapter for item in plan["items"] for chapter in item["chapter_targets"]})
    for chapter in target_chapters:
        filename = f"ch{chapter:04d}.md"
        chapter_items = [item for item in plan["items"] if chapter in item["chapter_targets"]]
        finding_ids = sorted({finding_id for item in chapter_items for finding_id in item["finding_ids"]})
        chapter_facts = [fact for fact in dossier.get("protected_facts", []) if chapter in fact["chapters"]]
        expected_hash = sha256_text(candidate_texts[filename])
        stored = saved.get(chapter, {})
        if stored.get("candidate_sha256") == expected_hash and isinstance(stored.get("review"), dict):
            review = stored["review"]
        else:
            review = call_json(
                llm, role, REVIEW_SYSTEM,
                review_request_for_chapter(contract, dossier, plan, candidate_texts, chapter), 2200,
            )
            saved[chapter] = {"chapter": chapter, "candidate_sha256": expected_hash, "review": review}
            if checkpoint_path:
                write_json(checkpoint_path, [saved[key] for key in sorted(saved)])
        review = normalize_chapter_review_ids(review, finding_ids, [fact["id"] for fact in chapter_facts])
        saved[chapter] = {"chapter": chapter, "candidate_sha256": expected_hash, "review": review}
        if checkpoint_path:
            write_json(checkpoint_path, [saved[key] for key in sorted(saved)])
        reviewer_ids.append(review.get("reviewer_id", ""))
        chapter_findings = review.get("finding_assessments", [])
        chapter_facts = review.get("protected_fact_assessments", [])
        chapter_blockers = review.get("blocking_findings", [])
        if not isinstance(chapter_findings, list) or not isinstance(chapter_facts, list) or not isinstance(chapter_blockers, list):
            raise FinalEditorVerificationError(f"chapter {chapter} independent review has an invalid shape")
        findings.extend(chapter_findings)
        facts.extend(chapter_facts)
        # A blocker may be a structured finding (location/defect/fix) or a
        # concise string.  Both are concrete review evidence and must reach
        # verification; dropping object-form blockers can falsely accept a
        # candidate that the independent reviewer rejected.
        blockers.extend(item for item in chapter_blockers if isinstance(item, (str, dict)))
    merged = {
        "reviewer_id": " + ".join(sorted({item for item in reviewer_ids if item})) or "independent_final_editor_chapter_review",
        "candidate_chapter_sha256": {name: sha256_text(text) for name, text in candidate_texts.items()},
        "blocking_findings": blockers,
        "finding_assessments": findings,
        "protected_fact_assessments": facts,
    }
    return preserve_unchanged_fact_assessments(merged, contract, dossier, candidate_texts)


def remediation_review_request(contract, dossier, plan, candidate_texts, prior_review, remediation):
    """Request an independent delta review after a concrete prior-review repair."""
    expected = sorted({finding_id for item in plan["items"] for finding_id in item["finding_ids"]})
    protected_fact_ids = sorted(fact["id"] for fact in dossier.get("protected_facts", []))
    changed_file = remediation["candidate_change"]["file"]
    continuity_file = "ch0007.md"
    return {
        "task": "Return {reviewer_id, candidate_chapter_sha256, blocking_findings, finding_assessments, protected_fact_assessments}.",
        "reviewer_id": "independent_final_editor_delta_review",
        "candidate_chapter_sha256": {name: sha256_text(text) for name, text in candidate_texts.items()},
        "planned_finding_ids": expected,
        "protected_fact_ids": protected_fact_ids,
        "prior_independent_review": prior_review,
        "remediation": remediation,
        "recheck_chapters": {
            continuity_file: candidate_texts[continuity_file],
            changed_file: candidate_texts[changed_file],
        },
        "rules": [
            "The prior review already assessed every planned finding and protected fact; independently recheck its only blocker against the supplied corrected candidate chapters.",
            "Confirm whether the stated remediation resolves the blocker without creating a new concrete defect.",
            "Return every planned finding and protected fact exactly once, carrying forward a prior resolved/preserved status only if the correction does not undermine it.",
            "Each finding status is resolved or unresolved; each protected fact status is preserved or changed.",
            "blocking_findings must be empty if the stated blocker is resolved and no new concrete defect is present.",
        ],
    }


def copy_candidate_story(source_dir, candidate_dir, candidate_texts):
    source, target = Path(source_dir), Path(candidate_dir)
    if target.exists():
        raise FileExistsError(f"candidate directory already exists: {target}")
    shutil.copytree(source, target)
    for filename, text in candidate_texts.items():
        (target / filename).write_text(text, encoding="utf-8")
    return target


def fact_report(candidate_dir):
    contract = Path(candidate_dir) / "story_facts.json"
    if not contract.is_file():
        # A control bundle can carry literal canon, but it is not a substitute
        # for the source-anchored checks in story_facts.json.  Surface the
        # missing contract explicitly so verification cannot mistake an empty
        # finding list for a completed fact audit.
        authority = _planning_authority(candidate_dir)
        try:
            has_control_facts = bool(json.loads(authority).get("canonical_facts")) if authority else False
        except (TypeError, ValueError, json.JSONDecodeError):
            has_control_facts = False
        return {
            "findings": [],
            "status": "missing_fact_contract" if has_control_facts else "not_configured",
        }
    return {"findings": check_story(candidate_dir, contract), "status": "checked"}


def _editor_roles(roles=None):
    roles = {
        "dossier": "final_editor",
        "plan": "final_editor",
        "editor": "final_editor",
        "review": "reviewer",
        **(roles or {}),
    }
    if set(roles) != {"dossier", "plan", "editor", "review"} or any(not isinstance(role, str) or not role.strip() for role in roles.values()):
        raise ValueError("final-editor model roles must name dossier, plan, editor, and review roles")
    return roles


def _load_reusable_packets(artifact_dir, contract, expected_chapters):
    """Reuse a completed evidence pass only when it belongs to this exact source contract."""
    artifact_dir = Path(artifact_dir)
    contract_path = artifact_dir / "contract.json"
    packets_path = artifact_dir / "chapter_evidence_packets.json"
    if not contract_path.is_file() or not packets_path.is_file():
        return None
    try:
        previous_contract = json.loads(contract_path.read_text(encoding="utf-8"))
        packets = json.loads(packets_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if (previous_contract.get("story_id") != contract["story_id"]
            or previous_contract.get("source") != contract["source"]):
        return None
    if not isinstance(packets, list) or [packet.get("chapter") for packet in packets] != expected_chapters:
        return None
    return packets


def _verified_dossier_to_proposal(dossier):
    """Rebuild an input proposal from a verified dossier's exact source anchors."""
    def quotes(record):
        return [{"file": anchor["file"], "text": anchor["excerpt"]} for anchor in record["anchors"]]

    return {
        "story_id": dossier["story_id"],
        "book_promise": dossier["book_promise"],
        "voice_profile": dossier["voice_profile"],
        "chapter_roles": [
            {"chapter": row["chapter"], "function": row["function"], "evidence_quotes": quotes(row)}
            for row in dossier["chapter_roles"]
        ],
        "protected_facts": [
            {"id": row["id"], "claim": row["claim"], "chapters": row["chapters"], "evidence_quotes": quotes(row)}
            for row in dossier["protected_facts"]
        ],
        "findings": [
            {
                "id": row["id"], "root_cause": row["root_cause"], "impact": row["impact"],
                "chapters": row["chapters"], "evidence_quotes": quotes(row),
                "repair_scope": row["repair_scope"], "requires_human_decision": False,
            }
            for row in dossier["findings"]
        ],
    }


def _load_reusable_dossier(artifact_dir, contract, source_texts):
    """Reuse an evidence dossier only after anchoring it again to this source contract."""
    artifact_dir = Path(artifact_dir)
    contract_path = artifact_dir / "contract.json"
    dossier_path = artifact_dir / "dossier.json"
    if not contract_path.is_file() or not dossier_path.is_file():
        return None
    try:
        previous_contract = json.loads(contract_path.read_text(encoding="utf-8"))
        previous_dossier = json.loads(dossier_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if (previous_contract.get("story_id") != contract["story_id"]
            or previous_contract.get("source") != contract["source"]):
        return None
    try:
        dossier = verify_dossier(_verified_dossier_to_proposal(previous_dossier), source_texts, contract)
    except (KeyError, TypeError, FinalEditorDossierError):
        return None
    return dossier if dossier["status"] == "verified" else None


def _build_verified_dossier(llm, role, contract, chapter_packets, source_texts, source_artifacts=None):
    """Bounded schema correction retries without repeating chapter evidence extraction."""
    correction = None
    for attempt in range(3):
        proposal = call_json(
            llm, role, DOSSIER_SYSTEM, dossier_request(contract, chapter_packets, source_artifacts, correction), 7000
        )
        try:
            dossier = verify_dossier(proposal, source_texts, contract)
        except FinalEditorDossierError as exc:
            if attempt == 2:
                raise
            correction = str(exc)
            continue
        if dossier["status"] == "verified":
            return dossier
        if attempt == 2:
            return dossier
        rejected = dossier.get("rejected_records", [])
        reasons = "; ".join(sorted({str(item.get("reason", "invalid dossier record")) for item in rejected})[:4])
        correction = f"dossier validation incomplete: {reasons or 'missing verified records'}"
    raise FinalEditorDossierError("dossier validation retry exhausted")


def _build_verified_plan(llm, role, contract, dossier, artifact_dir=None):
    """Bounded schema correction retries using one already-verified dossier."""
    correction = None
    attempts = []
    for attempt in range(3):
        proposal = call_json(llm, role, PLAN_SYSTEM, plan_request(contract, dossier, correction), 5000)
        try:
            plan = verify_plan(proposal, dossier, contract)
        except FinalEditorPlanError as exc:
            attempts.append({"attempt": attempt + 1, "proposal": proposal, "validation_error": str(exc)})
            if artifact_dir is not None:
                write_json(Path(artifact_dir) / "plan_attempts.json", attempts)
            if attempt == 2:
                raise
            correction = str(exc)
            continue
        attempts.append({"attempt": attempt + 1, "proposal": proposal, "validation_error": None})
        if artifact_dir is not None:
            write_json(Path(artifact_dir) / "plan_attempts.json", attempts)
        return plan
    raise FinalEditorPlanError("plan validation retry exhausted")


def _build_verified_plan_by_chapter(llm, role, contract, dossier, artifact_dir=None):
    """Plan bounded chapter batches so a full review cannot overflow one prompt."""
    items = []
    for chapter in sorted({chapter for finding in dossier["findings"] for chapter in finding["chapters"]}):
        chapter_dossier = copy.deepcopy(dossier)
        chapter_dossier["findings"] = [
            finding for finding in dossier["findings"] if finding["chapters"] == [chapter]
        ]
        partial = _build_verified_plan(llm, role, contract, chapter_dossier)
        for item in partial["items"]:
            merged_item = copy.deepcopy(item)
            merged_item["id"] = f"ch{chapter:04d}_{item['id']}"
            merged_item["allowed_word_deltas"] = {
                str(target): delta for target, delta in item["allowed_word_deltas"].items()
            }
            merged_item.pop("status", None)
            items.append(merged_item)
    proposal = {
        "story_id": contract["story_id"],
        "dossier_contract_sha256": dossier["contract_sha256"],
        "items": items,
        "deferred": [],
    }
    plan = verify_plan(proposal, dossier, contract)
    if artifact_dir is not None:
        write_json(Path(artifact_dir) / "plan_attempts.json", {
            "strategy": "per_chapter_verified_plan",
            "chapter_count": len({finding["chapters"][0] for finding in dossier["findings"]}),
            "item_count": len(plan["items"]),
        })
    return plan


def detect_issues(story_id, source_dir, run_id, artifact_dir, llm, review_path=None, roles=None,
                  reuse_evidence_packets=False, reuse_evidence_from=None, reuse_dossier_from=None):
    """Mandatory final pass 1: inspect the finished book and record issues."""
    roles = _editor_roles(roles)
    source_texts = read_chapters(source_dir)
    artifacts = optional_artifacts(source_dir, review_path)
    contract = build_contract(story_id, run_id, source_texts, source_artifacts=artifacts)
    expected_chapters = list(range(1, len(source_texts) + 1))
    reuse_dir = Path(reuse_evidence_from) if reuse_evidence_from else Path(artifact_dir)
    reuse_requested = reuse_evidence_packets or reuse_evidence_from is not None
    chapter_packets = _load_reusable_packets(reuse_dir, contract, expected_chapters) if reuse_requested else None
    write_json(Path(artifact_dir) / "contract.json", contract)
    packets_path = Path(artifact_dir) / "chapter_evidence_packets.json"
    if chapter_packets is None:
        chapter_packets = []
        for filename, text in sorted(source_texts.items()):
            chapter_packets.append(call_json(
                llm, roles["dossier"], DOSSIER_CHAPTER_SYSTEM,
                dossier_chapter_request(contract, filename, text, artifacts), 1200,
            ))
            write_json(packets_path, chapter_packets)
        write_json(Path(artifact_dir) / "evidence_reuse.json", {
            "status": "fresh_extraction",
            "source_artifact_dir": None,
        })
    else:
        write_json(packets_path, chapter_packets)
        write_json(Path(artifact_dir) / "evidence_reuse.json", {
            "status": "reused_verified_source_match",
            "source_artifact_dir": str(reuse_dir),
            "chapter_count": len(chapter_packets),
        })
    dossier_reuse_dir = Path(reuse_dossier_from) if reuse_dossier_from else reuse_dir
    if review_path:
        dossier = dossier_from_chapter_packets(contract, chapter_packets, source_texts)
        write_json(Path(artifact_dir) / "dossier_reuse.json", {
            "status": "deterministic_verified_chapter_aggregation",
            "source_artifact_dir": None,
            "finding_count": len(dossier.get("findings", [])),
        })
    else:
        dossier = _load_reusable_dossier(dossier_reuse_dir, contract, source_texts) if reuse_dossier_from or reuse_requested else None
    if dossier is None:
        dossier = _build_verified_dossier(llm, roles["dossier"], contract, chapter_packets, source_texts, artifacts)
        write_json(Path(artifact_dir) / "dossier_reuse.json", {
            "status": "fresh_extraction",
            "source_artifact_dir": None,
        })
    else:
        write_json(Path(artifact_dir) / "dossier_reuse.json", {
            "status": "reused_reverified_source_match",
            "source_artifact_dir": str(dossier_reuse_dir),
            "finding_count": len(dossier.get("findings", [])),
        })
    write_json(Path(artifact_dir) / "dossier.json", dossier)
    if dossier["status"] != "verified" or not dossier["protected_facts"]:
        return {
            "status": "dossier_incomplete",
            "reason": "no_verified_protected_facts" if dossier["status"] == "verified" else "dossier_evidence_incomplete",
            "contract": contract,
            "dossier": dossier,
        }
    plan_builder = _build_verified_plan_by_chapter if review_path else _build_verified_plan
    plan = plan_builder(llm, roles["plan"], contract, dossier, artifact_dir=artifact_dir)
    write_json(Path(artifact_dir) / "plan.json", plan)
    write_json(Path(artifact_dir) / "issues.json", {
        "story_id": story_id,
        "run_id": run_id,
        "findings": dossier.get("findings", []),
        "deferred": plan.get("deferred", []),
        "repair_items": plan.get("items", []),
    })
    return {"status": "detected", "contract": contract, "dossier": dossier, "plan": plan}


def repair_issues(story_id, source_dir, artifact_dir, llm, roles=None, detected=None,
                  unresolved_remediation_rounds=UNRESOLVED_REPAIR_ROUNDS):
    """Mandatory final pass 2: repair every safe recorded issue on a candidate."""
    roles = _editor_roles(roles)
    artifact_dir = Path(artifact_dir)
    if detected is None:
        contract = json.loads((artifact_dir / "contract.json").read_text(encoding="utf-8"))
        dossier = json.loads((artifact_dir / "dossier.json").read_text(encoding="utf-8"))
        plan = json.loads((artifact_dir / "plan.json").read_text(encoding="utf-8"))
    else:
        contract, dossier, plan = detected["contract"], detected["dossier"], detected["plan"]
    source_texts = read_chapters(source_dir)
    if not plan["items"]:
        # A no-op candidate still passes through the immutable audit and fact
        # gate. Its identical hashes make fact preservation deterministic.
        candidate_texts, candidate_audit = build_candidate(source_texts, contract, dossier, plan, {})
        candidate_dir = Path(source_dir).parent / contract["candidate"]["story_id"]
        copy_candidate_story(source_dir, candidate_dir, candidate_texts)
        review = preserve_unchanged_fact_assessments({
            "reviewer_id": "deterministic-unchanged-candidate-verifier",
            "candidate_chapter_sha256": {
                name: sha256_text(text) for name, text in candidate_texts.items()
            },
            "finding_assessments": [],
            "blocking_findings": [],
            "protected_fact_assessments": [],
        }, contract, dossier, candidate_texts)
        verification = verify_candidate(
            source_texts, candidate_texts, contract, dossier, plan, candidate_audit,
            fact_report(candidate_dir), review,
        )
        write_json(Path(artifact_dir) / "candidate_audit.json", candidate_audit)
        write_json(Path(artifact_dir) / "independent_review.json", review)
        write_json(Path(artifact_dir) / "verification.json", verification)
        return {
            "status": verification["decision"],
            "candidate_dir": str(candidate_dir),
            "contract": contract,
            "dossier": dossier,
            "plan": plan,
            "verification": verification,
        }
    revisions = {}
    revision_attempts = {}
    plan_adjustments = []
    finding_map = {finding["id"]: finding for finding in dossier.get("findings", [])}
    current = copy.deepcopy(source_texts)
    for item in plan["items"]:
        correction = None
        attempts = []
        for attempt in range(3):
            revision = call_json(
                llm, roles["editor"], EDITOR_SYSTEM,
                revision_request(contract, dossier, item, current, correction), 9000,
            )
            trial_revisions = {**revisions, item["id"]: revision}
            partial_plan = copy.deepcopy(plan)
            partial_plan["items"] = [
                next(row for row in plan["items"] if row["id"] == plan_id)
                for plan_id in trial_revisions
            ]
            try:
                next_current, _ = build_candidate(source_texts, contract, dossier, partial_plan, trial_revisions)
            except FinalEditorCandidateError as exc:
                correction = str(exc)
                attempts.append({"attempt": attempt + 1, "revision": revision, "validation_error": correction})
                revision_attempts[item["id"]] = attempts
                write_json(artifact_dir / "revision_attempts.json", revision_attempts)
                if attempt == 2:
                    linked = [finding_map[finding_id] for finding_id in item["finding_ids"]]
                    duplicate_cleanup = any(
                        any(marker in finding["root_cause"].lower() for marker in ("duplicate", "alternate draft"))
                        for finding in linked
                    )
                    allowed_max = contract["word_budget"]["per_chapter_max_delta"]
                    candidate_deltas = {}
                    for filename, proposal in revision.get("chapters", {}).items():
                        source_words = next(row["words"] for row in contract["source"]["chapters"] if row["file"] == filename)
                        candidate_deltas[int(filename[2:6])] = (count_words(proposal.get("revised_text", "")) - source_words) / source_words
                    if (candidate_deltas
                            and all(abs(delta) <= allowed_max for delta in candidate_deltas.values())):
                        for chapter, delta in candidate_deltas.items():
                            item["allowed_word_deltas"].pop(str(chapter), None)
                            item["allowed_word_deltas"][chapter] = delta
                        plan["aggregate_allowed_word_deltas"] = {
                            chapter: sum(row["allowed_word_deltas"].get(chapter, row["allowed_word_deltas"].get(str(chapter), 0.0))
                                         for row in plan["items"])
                            for chapter in {chapter for row in plan["items"] for chapter in row["allowed_word_deltas"]}
                        }
                        plan_adjustments.append({
                            "plan_id": item["id"],
                            "reason": (
                                "verified_duplicate_cleanup_within_contract_cap"
                                if duplicate_cleanup else "validated_revision_within_contract_cap_after_budget_retry"
                            ),
                            "previous_validation_error": correction,
                            "adopted_word_deltas": candidate_deltas,
                        })
                        write_json(artifact_dir / "plan_adjustments.json", plan_adjustments)
                        write_json(artifact_dir / "plan.json", plan)
                        revisions[item["id"]] = revision
                        partial_plan = copy.deepcopy(plan)
                        partial_plan["items"] = [
                            next(row for row in plan["items"] if row["id"] == plan_id)
                            for plan_id in revisions
                        ]
                        current, _ = build_candidate(source_texts, contract, dossier, partial_plan, revisions)
                        break
                    raise
                continue
            attempts.append({"attempt": attempt + 1, "revision": revision, "validation_error": None})
            revision_attempts[item["id"]] = attempts
            write_json(artifact_dir / "revision_attempts.json", revision_attempts)
            revisions, current = trial_revisions, next_current
            break
    candidate_texts, candidate_audit = build_candidate(source_texts, contract, dossier, plan, revisions)
    write_json(Path(artifact_dir) / "candidate_audit.json", candidate_audit)
    candidate_dir = Path(source_dir).parent / contract["candidate"]["story_id"]
    copy_candidate_story(source_dir, candidate_dir, candidate_texts)
    review = review_candidate_by_chapter(llm, roles["review"], contract, dossier, plan, candidate_texts, artifact_dir)
    write_json(Path(artifact_dir) / "independent_review.json", review)
    verification = verify_candidate(source_texts, candidate_texts, contract, dossier, plan, candidate_audit, fact_report(candidate_dir), review)
    # Rework only the plan items the independent reviewer left unresolved.
    # Do not feed a fresh open-ended book review back into editing: that turns
    # a final pass into an endless stream of newly invented style tickets.
    remediation_log = []
    remediation_rounds = max(0, min(6, int(unresolved_remediation_rounds)))
    for remediation_round in range(1, remediation_rounds + 1):
        unresolved = {
            finding_id for finding_id, status in verification["independent_review"]["assessments"].items()
            if status == "unresolved"
        }
        if not unresolved or verification["independent_review"]["blocking_findings"]:
            break
        targets = [item for item in plan["items"] if unresolved.intersection(item["finding_ids"])]
        if not targets:
            break
        for item in targets:
            item_unresolved = sorted(unresolved.intersection(item["finding_ids"]))
            correction = (
                "Independent review still marks these planned findings unresolved: "
                + ", ".join(item_unresolved)
                + ". Repair those exact anchored concerns now; retain already resolved findings."
            )
            attempt_key = f"{item['id']}_remediation_{remediation_round}"
            attempts = []
            for attempt in range(3):
                revision = call_json(
                    llm, roles["editor"], EDITOR_SYSTEM,
                    revision_request(contract, dossier, item, candidate_texts, correction), 9000,
                )
                try:
                    candidate_texts, wave = apply_repair_wave(candidate_texts, source_texts, contract, item, revision)
                except FinalEditorCandidateError as exc:
                    correction = str(exc)
                    attempts.append({"attempt": attempt + 1, "revision": revision, "validation_error": correction})
                    revision_attempts[attempt_key] = attempts
                    write_json(artifact_dir / "revision_attempts.json", revision_attempts)
                    continue
                attempts.append({"attempt": attempt + 1, "revision": revision, "validation_error": None})
                revision_attempts[attempt_key] = attempts
                candidate_audit.setdefault("remediation_waves", []).append({
                    "round": remediation_round,
                    "unresolved_finding_ids": item_unresolved,
                    **wave,
                })
                for filename in revision["chapters"]:
                    (candidate_dir / filename).write_text(candidate_texts[filename], encoding="utf-8")
                write_json(artifact_dir / "revision_attempts.json", revision_attempts)
                break
            else:
                raise FinalEditorCandidateError(f"could not produce a valid remediation for {item['id']}")
        candidate_audit["candidate_chapter_sha256"] = {
            name: sha256_text(text) for name, text in candidate_texts.items()
        }
        write_json(Path(artifact_dir) / "candidate_audit.json", candidate_audit)
        review = review_candidate_by_chapter(llm, roles["review"], contract, dossier, plan, candidate_texts, artifact_dir)
        write_json(Path(artifact_dir) / "independent_review.json", review)
        verification = verify_candidate(
            source_texts, candidate_texts, contract, dossier, plan, candidate_audit,
            fact_report(candidate_dir), review,
        )
        remediation_log.append({
            "round": remediation_round,
            "finding_ids": sorted(unresolved),
            "decision": verification["decision"],
        })
        write_json(Path(artifact_dir) / "remediation_rounds.json", remediation_log)
    write_json(Path(artifact_dir) / "verification.json", verification)
    return {"status": verification["decision"], "candidate_dir": str(candidate_dir), "verification": verification}


def verify_existing_candidate(story_id, source_dir, artifact_dir, llm, roles=None):
    """Independently review an already-built candidate without editing or copying it again."""
    roles = _editor_roles(roles)
    artifact_dir = Path(artifact_dir)
    contract = json.loads((artifact_dir / "contract.json").read_text(encoding="utf-8"))
    dossier = json.loads((artifact_dir / "dossier.json").read_text(encoding="utf-8"))
    plan = json.loads((artifact_dir / "plan.json").read_text(encoding="utf-8"))
    candidate_audit = json.loads((artifact_dir / "candidate_audit.json").read_text(encoding="utf-8"))
    candidate_dir = Path(source_dir).parent / contract["candidate"]["story_id"]
    source_texts = read_chapters(source_dir)
    candidate_texts = read_chapters(candidate_dir)
    remediation_path = artifact_dir / "post_review_remediation.json"
    if remediation_path.is_file():
        prior_review_path = artifact_dir / "independent_review.json"
        remediation = json.loads(remediation_path.read_text(encoding="utf-8"))
        prior_review = json.loads(prior_review_path.read_text(encoding="utf-8"))
        review_payload = remediation_review_request(contract, dossier, plan, candidate_texts, prior_review, remediation)
        review = call_json(llm, roles["review"], REVIEW_SYSTEM, review_payload, 7000)
    else:
        review = review_candidate_by_chapter(llm, roles["review"], contract, dossier, plan, candidate_texts, artifact_dir)
    write_json(artifact_dir / "independent_review.json", review)
    verification = verify_candidate(
        source_texts, candidate_texts, contract, dossier, plan, candidate_audit,
        fact_report(candidate_dir), review,
    )
    write_json(artifact_dir / "verification.json", verification)
    return {"status": verification["decision"], "candidate_dir": str(candidate_dir), "verification": verification}


def run_auto(story_id, source_dir, run_id, artifact_dir, llm, review_path=None, roles=None,
             reuse_evidence_packets=False, reuse_evidence_from=None, reuse_dossier_from=None,
             unresolved_remediation_rounds=UNRESOLVED_REPAIR_ROUNDS):
    """Compatibility entry point: run the mandatory detect then repair passes."""
    detected = detect_issues(
        story_id, source_dir, run_id, artifact_dir, llm, review_path, roles,
        reuse_evidence_packets=reuse_evidence_packets,
        reuse_evidence_from=reuse_evidence_from,
        reuse_dossier_from=reuse_dossier_from,
    )
    if detected["status"] != "detected":
        return detected
    return repair_issues(
        story_id, source_dir, artifact_dir, llm, roles, detected=detected,
        unresolved_remediation_rounds=unresolved_remediation_rounds,
    )


def main():
    parser = argparse.ArgumentParser(description="Candidate-only AI final editor")
    parser.add_argument("--story-id", required=True)
    parser.add_argument("--run-id", default="")
    parser.add_argument("--review-report", default="")
    parser.add_argument("--out", default="")
    parser.add_argument("--dossier-role", default="final_editor")
    parser.add_argument("--plan-role", default="final_editor")
    parser.add_argument("--editor-role", default="final_editor")
    parser.add_argument("--review-role", default="reviewer")
    parser.add_argument("--unresolved-remediation-rounds", type=int, default=UNRESOLVED_REPAIR_ROUNDS)
    parser.add_argument("--auto", action="store_true", help="Run AI diagnosis, planning, candidate editing, and independent review")
    parser.add_argument("--mode", choices=("detect", "repair", "verify", "auto"), default="auto")
    parser.add_argument("--reuse-evidence-packets", action="store_true", help="Reuse completed packets only when their source contract is unchanged")
    parser.add_argument("--reuse-evidence-from", default="", help="Prior final-editor artifact directory from which verified source-matched evidence packets may be reused")
    parser.add_argument("--reuse-dossier-from", default="", help="Prior final-editor artifact directory from which a source-reverified dossier may be reused")
    args = parser.parse_args()
    if not args.auto and args.mode == "auto":
        print("refusing to run without --auto; no source files changed")
        return 2
    run_id = args.run_id or now_run_id()
    source_dir = ROOT / "shorts" / args.story_id
    artifact_dir = Path(args.out) if args.out else ROOT / "out" / args.story_id / "final_editor" / run_id
    if not artifact_dir.is_absolute():
        artifact_dir = ROOT / artifact_dir
    cfg = load_json(ROOT / "config.json")
    load_env()
    try:
        roles = {"dossier": args.dossier_role, "plan": args.plan_role, "editor": args.editor_role, "review": args.review_role}
        llm = LLM(cfg)
        if args.mode == "detect":
            result = detect_issues(
                args.story_id, source_dir, run_id, artifact_dir, llm, args.review_report or None, roles=roles,
                reuse_evidence_packets=args.reuse_evidence_packets,
                reuse_evidence_from=args.reuse_evidence_from or None,
                reuse_dossier_from=args.reuse_dossier_from or None,
            )
        elif args.mode == "repair":
            result = repair_issues(
                args.story_id, source_dir, artifact_dir, llm, roles=roles,
                unresolved_remediation_rounds=args.unresolved_remediation_rounds,
            )
        elif args.mode == "verify":
            result = verify_existing_candidate(args.story_id, source_dir, artifact_dir, llm, roles=roles)
        else:
            result = run_auto(
                args.story_id, source_dir, run_id, artifact_dir, llm, args.review_report or None, roles=roles,
                reuse_evidence_packets=args.reuse_evidence_packets,
                reuse_evidence_from=args.reuse_evidence_from or None,
                reuse_dossier_from=args.reuse_dossier_from or None,
                unresolved_remediation_rounds=args.unresolved_remediation_rounds,
            )
    except (ValueError, RuntimeError, KeyError, FinalEditorDossierError, FinalEditorPlanError,
            FinalEditorCandidateError, FinalEditorVerificationError, OSError) as exc:
        result = {"status": "failed", "error": str(exc)}
    write_json(artifact_dir / "run_result.json", result)
    print(json.dumps({"status": result["status"], "artifact_dir": str(artifact_dir)}, ensure_ascii=False))
    success_statuses = {"accepted"} if args.mode in {"repair", "verify"} else {"detected"}
    if args.mode == "auto":
        success_statuses = {"accepted"}
    return 0 if result["status"] in success_statuses else 2


if __name__ == "__main__":
    raise SystemExit(main())
