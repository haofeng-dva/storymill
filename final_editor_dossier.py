# -*- coding: utf-8 -*-
"""Validate evidence-bound whole-book editorial dossiers without model calls."""
import copy
import hashlib
import json
import re

from final_editor_contract import (
    FinalEditorContractError,
    chapter_manifest,
    validate_contract,
)


SCHEMA_VERSION = "final_editor_dossier_v1"
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
REPAIR_SCOPES = {"local", "scene", "multi_chapter"}


class FinalEditorDossierError(ValueError):
    """Raised when a dossier cannot be linked to its immutable source."""


def _contract_hash(contract):
    raw = json.dumps(contract, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _require_text(value, label, *, minimum=1):
    if not isinstance(value, str) or len(value.strip()) < minimum:
        raise FinalEditorDossierError(f"{label} must be a non-empty string")
    return value


def _require_id(value, label):
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise FinalEditorDossierError(f"{label} must be a safe non-empty id")
    return value


def quote_anchor(text, quote):
    """Return an exact unique quotation anchor, or a reason it cannot be used."""
    if not isinstance(quote, str) or len(quote.strip()) < 12:
        return None, "quote is too short"
    if quote != quote.strip():
        return None, "quote must be copied exactly without surrounding whitespace"
    if text.count(quote) != 1:
        return None, "quote is absent or not unique in declared source"
    start = text.index(quote)
    end = start + len(quote)
    lines = text.splitlines()
    line_start = text.count("\n", 0, start) + 1
    line_end = text.count("\n", 0, end) + 1
    return {
        "excerpt": quote,
        "line_start": line_start,
        "line_end": line_end,
        "context_before": lines[max(0, line_start - 3):line_start - 1],
        "context_after": lines[line_end:min(len(lines), line_end + 2)],
    }, ""


def _chapter_text_lookup(chapter_texts):
    try:
        manifest = chapter_manifest(chapter_texts)
    except FinalEditorContractError as exc:
        raise FinalEditorDossierError(str(exc)) from exc
    return manifest, chapter_texts


def _verify_quotes(entries, chapter_texts, permitted_chapters):
    if not isinstance(entries, list) or not entries:
        return None, "evidence_quotes must be a non-empty list"
    anchors = []
    for entry in entries:
        if not isinstance(entry, dict):
            return None, "evidence quote must be an object"
        filename, quote = entry.get("file"), entry.get("text")
        if filename not in chapter_texts:
            return None, f"unknown evidence file {filename!r}"
        chapter = int(filename[2:6]) if re.fullmatch(r"ch\d{4}\.md", filename or "") else None
        if chapter not in permitted_chapters:
            return None, "evidence file is outside declared chapters"
        anchor, reason = quote_anchor(chapter_texts[filename], quote)
        if not anchor:
            return None, reason
        anchor["file"] = filename
        anchors.append(anchor)
    return anchors, ""


def _verify_role(item, chapter_texts, valid_chapters):
    if not isinstance(item, dict):
        return None, "chapter role must be an object"
    chapter = item.get("chapter")
    if not isinstance(chapter, int) or chapter not in valid_chapters:
        return None, "chapter role has an unknown chapter"
    function = _require_text(item.get("function"), "chapter role function", minimum=4)
    filename = f"ch{chapter:04d}.md"
    anchors, reason = _verify_quotes(item.get("evidence_quotes"), chapter_texts, {chapter})
    if not anchors:
        return None, reason
    return {
        "chapter": chapter,
        "function": function,
        "anchors": anchors,
        "status": "verified",
    }, ""


def _verify_finding(item, chapter_texts, valid_chapters, seen_ids):
    if not isinstance(item, dict):
        return None, "finding must be an object"
    finding_id = _require_id(item.get("id"), "finding id")
    if finding_id in seen_ids:
        return None, f"duplicate finding id {finding_id!r}"
    chapters = item.get("chapters")
    if (not isinstance(chapters, list) or not chapters
            or any(not isinstance(chapter, int) or chapter not in valid_chapters for chapter in chapters)
            or chapters != sorted(set(chapters))):
        return None, "finding chapters must be sorted unique existing chapter numbers"
    scope = item.get("repair_scope")
    if scope not in REPAIR_SCOPES:
        return None, "finding repair_scope is invalid"
    if scope == "multi_chapter" and len(chapters) < 2:
        return None, "multi_chapter finding must affect at least two chapters"
    if scope != "multi_chapter" and len(chapters) != 1:
        return None, "local or scene finding must affect exactly one chapter"
    if not isinstance(item.get("requires_human_decision"), bool):
        return None, "finding requires_human_decision must be boolean"
    root_cause = _require_text(item.get("root_cause"), "finding root_cause", minimum=4)
    impact = _require_text(item.get("impact"), "finding impact", minimum=8)
    anchors, reason = _verify_quotes(item.get("evidence_quotes"), chapter_texts, set(chapters))
    if not anchors:
        return None, reason
    seen_ids.add(finding_id)
    return {
        "id": finding_id,
        "root_cause": root_cause,
        "impact": impact,
        "chapters": chapters,
        "anchors": anchors,
        "repair_scope": scope,
        "requires_human_decision": item["requires_human_decision"],
        "status": "verified",
    }, ""


def _verify_protected_fact(item, chapter_texts, valid_chapters, seen_ids):
    if not isinstance(item, dict):
        return None, "protected fact must be an object"
    fact_id = _require_id(item.get("id"), "protected fact id")
    if fact_id in seen_ids:
        return None, f"duplicate protected fact id {fact_id!r}"
    chapters = item.get("chapters")
    if (not isinstance(chapters, list) or not chapters
            or any(not isinstance(chapter, int) or chapter not in valid_chapters for chapter in chapters)
            or chapters != sorted(set(chapters))):
        return None, "protected fact chapters must be sorted unique existing chapter numbers"
    claim = _require_text(item.get("claim"), "protected fact claim", minimum=8)
    anchors, reason = _verify_quotes(item.get("evidence_quotes"), chapter_texts, set(chapters))
    if not anchors:
        return None, reason
    seen_ids.add(fact_id)
    return {
        "id": fact_id,
        "claim": claim,
        "chapters": chapters,
        "anchors": anchors,
        "status": "verified",
    }, ""


def verify_dossier(proposal, chapter_texts, contract):
    """Verify model-proposed editorial evidence against an unchanged source book."""
    manifest, source = _chapter_text_lookup(chapter_texts)
    try:
        validate_contract(contract, expected_manifest=manifest)
    except FinalEditorContractError as exc:
        raise FinalEditorDossierError(str(exc)) from exc
    if not isinstance(proposal, dict):
        raise FinalEditorDossierError("dossier proposal must be an object")
    story_id = proposal.get("story_id")
    if story_id != contract["story_id"]:
        raise FinalEditorDossierError("dossier story_id does not match contract")
    book_promise = _require_text(proposal.get("book_promise"), "book_promise", minimum=12)
    voice_profile = _require_text(proposal.get("voice_profile"), "voice_profile", minimum=12)
    valid_chapters = {item["chapter"] for item in manifest}
    rejected = []

    verified_roles = []
    seen_role_chapters = set()
    for index, item in enumerate(proposal.get("chapter_roles", []), 1):
        try:
            role, reason = _verify_role(item, source, valid_chapters)
        except FinalEditorDossierError as exc:
            role, reason = None, str(exc)
        if role and role["chapter"] not in seen_role_chapters:
            verified_roles.append(role)
            seen_role_chapters.add(role["chapter"])
        else:
            rejected.append({"kind": "chapter_role", "index": index, "reason": reason or "duplicate chapter role"})

    seen_finding_ids = set()
    verified_findings = []
    for index, item in enumerate(proposal.get("findings", []), 1):
        try:
            finding, reason = _verify_finding(item, source, valid_chapters, seen_finding_ids)
        except FinalEditorDossierError as exc:
            finding, reason = None, str(exc)
        if finding:
            verified_findings.append(finding)
        else:
            rejected.append({"kind": "finding", "index": index, "reason": reason})

    seen_fact_ids = set()
    protected_facts = []
    for index, item in enumerate(proposal.get("protected_facts", []), 1):
        try:
            fact, reason = _verify_protected_fact(item, source, valid_chapters, seen_fact_ids)
        except FinalEditorDossierError as exc:
            fact, reason = None, str(exc)
        if fact:
            protected_facts.append(fact)
        else:
            rejected.append({"kind": "protected_fact", "index": index, "reason": reason})

    missing_roles = sorted(valid_chapters - seen_role_chapters)
    if missing_roles:
        rejected.append({"kind": "chapter_role", "reason": f"missing verified chapter roles: {missing_roles}"})
    return {
        "schema_version": SCHEMA_VERSION,
        "story_id": contract["story_id"],
        "contract_sha256": _contract_hash(contract),
        "source_chapter_sha256": {item["file"]: item["source_sha256"] for item in manifest},
        "book_promise": book_promise,
        "voice_profile": voice_profile,
        "chapter_roles": verified_roles,
        "protected_facts": protected_facts,
        "findings": verified_findings,
        "rejected_records": rejected,
        "status": "verified" if not rejected else "incomplete",
    }


def dossier_is_usable(dossier):
    """A candidate editor may proceed only from a complete verified dossier."""
    return isinstance(dossier, dict) and dossier.get("schema_version") == SCHEMA_VERSION and dossier.get("status") == "verified"


def copy_verified_dossier(dossier):
    """Return a detached copy for downstream planning without exposing proposal state."""
    if not dossier_is_usable(dossier):
        raise FinalEditorDossierError("only a complete verified dossier can enter repair planning")
    return copy.deepcopy(dossier)
