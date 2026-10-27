# -*- coding: utf-8 -*-
"""Apply approved final-editor revisions to an in-memory candidate only."""
import copy
import hashlib
import re

from final_editor_contract import (
    FinalEditorContractError,
    count_words,
    sha256_text,
    validate_contract,
    word_budget_report,
)
from final_editor_dossier import _contract_hash, dossier_is_usable
from final_editor_plan import plan_is_usable


class FinalEditorCandidateError(ValueError):
    """Raised when a proposed candidate revision violates its repair plan."""


def _source_words(contract, filename):
    return next(item["words"] for item in contract["source"]["chapters"] if item["file"] == filename)


def _target_files(item):
    return [f"ch{chapter:04d}.md" for chapter in item["chapter_targets"]]


def _validate_inputs(source_texts, contract, dossier, plan):
    try:
        validate_contract(contract)
    except FinalEditorContractError as exc:
        raise FinalEditorCandidateError(str(exc)) from exc
    if not dossier_is_usable(dossier):
        raise FinalEditorCandidateError("candidate editing requires a complete verified dossier")
    if dossier.get("contract_sha256") != _contract_hash(contract):
        raise FinalEditorCandidateError("dossier is not linked to the supplied contract")
    if not plan_is_usable(plan):
        raise FinalEditorCandidateError("candidate editing requires a verified repair plan")
    if dossier.get("contract_sha256") != plan.get("dossier_contract_sha256"):
        raise FinalEditorCandidateError("plan is not linked to the supplied dossier")
    expected = {item["file"]: item["source_sha256"] for item in contract["source"]["chapters"]}
    if set(source_texts) != set(expected):
        raise FinalEditorCandidateError("source_texts must match the contracted chapter set")
    for filename, digest in expected.items():
        if sha256_text(source_texts[filename]) != digest:
            raise FinalEditorCandidateError(f"source chapter changed: {filename}")


def apply_repair_wave(candidate_texts, source_texts, contract, item, revision):
    """Apply one approved plan item without writing source or candidate files."""
    if not isinstance(revision, dict) or revision.get("plan_id") != item.get("id"):
        raise FinalEditorCandidateError("revision plan_id does not match the approved plan item")
    replacements = revision.get("chapters")
    targets = _target_files(item)
    if not isinstance(replacements, dict) or set(replacements) != set(targets):
        raise FinalEditorCandidateError("revision chapters must exactly match the approved chapter targets")
    next_texts = dict(candidate_texts)
    audit = {
        "plan_id": item["id"], "status": "rejected", "chapters": [],
        "editorial_decision": item["editorial_decision"],
        "decision_basis": item["decision_basis"],
        "decision_confidence": item["decision_confidence"],
    }
    for filename in targets:
        proposal = replacements[filename]
        if not isinstance(proposal, dict):
            raise FinalEditorCandidateError(f"revision for {filename} must be an object")
        before = candidate_texts[filename]
        expected_hash = sha256_text(before)
        if proposal.get("source_sha256") != expected_hash:
            raise FinalEditorCandidateError(f"revision source hash does not match current candidate: {filename}")
        after = proposal.get("revised_text")
        if not isinstance(after, str) or not after.strip():
            raise FinalEditorCandidateError(f"revision for {filename} must contain non-empty revised_text")
        if after == before:
            raise FinalEditorCandidateError(f"revision for {filename} does not change the candidate")
        source_words = _source_words(contract, filename)
        after_words = count_words(after)
        delta = (after_words - source_words) / source_words
        chapter = int(filename[2:6])
        deltas = item["allowed_word_deltas"]
        allowed_delta = deltas.get(chapter, deltas.get(str(chapter)))
        if not isinstance(allowed_delta, (int, float)) or isinstance(allowed_delta, bool):
            raise FinalEditorCandidateError(f"revision is missing a numeric word delta for {filename}")
        allowed = abs(allowed_delta)
        if abs(delta) > allowed:
            raise FinalEditorCandidateError(f"revision for {filename} exceeds its approved word delta")
        next_texts[filename] = after
        audit["chapters"].append({
            "file": filename,
            "before_sha256": expected_hash,
            "after_sha256": sha256_text(after),
            "source_words": source_words,
            "candidate_words": after_words,
            "delta": delta,
            "finding_ids": list(item["finding_ids"]),
        })
    audit["status"] = "applied"
    return next_texts, audit


def build_candidate(source_texts, contract, dossier, plan, revisions):
    """Apply every approved plan item in order and return an unaccepted candidate."""
    _validate_inputs(source_texts, contract, dossier, plan)
    if not isinstance(revisions, dict) or set(revisions) != {item["id"] for item in plan["items"]}:
        raise FinalEditorCandidateError("revisions must contain exactly one revision for every approved plan item")
    candidate = copy.deepcopy(source_texts)
    audit = []
    for item in plan["items"]:
        candidate, wave = apply_repair_wave(candidate, source_texts, contract, item, revisions[item["id"]])
        audit.append(wave)
    budget = word_budget_report(contract, candidate)
    return candidate, {
        "schema_version": "final_editor_candidate_v1",
        "story_id": contract["story_id"],
        "candidate_story_id": contract["candidate"]["story_id"],
        "source_chapter_sha256": {name: sha256_text(text) for name, text in source_texts.items()},
        "candidate_chapter_sha256": {name: sha256_text(text) for name, text in candidate.items()},
        "waves": audit,
        "word_budget": budget,
        "status": "ready_for_independent_verification" if budget["accepted"] else "rejected_word_budget",
    }


def candidate_diff_summary(source_texts, candidate_texts):
    """Provide an auditable file-level change inventory without a prose diff engine."""
    if set(source_texts) != set(candidate_texts):
        raise FinalEditorCandidateError("source and candidate chapter sets differ")
    rows = []
    for filename in sorted(source_texts):
        before, after = source_texts[filename], candidate_texts[filename]
        rows.append({
            "file": filename,
            "changed": before != after,
            "before_sha256": sha256_text(before),
            "after_sha256": sha256_text(after),
            "before_words": count_words(before),
            "after_words": count_words(after),
        })
    return rows
