# -*- coding: utf-8 -*-
"""Immutable source and candidate-budget contracts for the final editor."""
import hashlib
import re
from datetime import datetime, timezone


SCHEMA_VERSION = "final_editor_contract_v1"
# Whole alternate-draft remnants can exceed a 10% deletion even when the
# remaining chapter is more coherent. The book-level cap stays conservative.
MAX_PER_CHAPTER_WORD_DELTA = 0.25
MAX_BOOK_WORD_DELTA = 0.05
CHAPTER_FILE = re.compile(r"ch(\d{4})\.md$")
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
REQUIRED_INVARIANTS = (
    "chapter_count",
    "chapter_order",
    "established_facts",
    "character_agency",
    "narrative_pov",
    "narrative_tense",
    "narrative_voice",
)
REQUIRED_VERIFICATIONS = (
    "fact_contract",
    "word_budget",
    "change_audit",
    "independent_review",
)
CANDIDATE_ID_LIMIT = 128


class FinalEditorContractError(ValueError):
    """Raised when a final-editor run cannot safely use a contract."""


def sha256_text(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def count_words(text):
    return len(re.findall(r"[A-Za-z']+", text or ""))


def candidate_story_id(story_id, run_id):
    """Derive a stable candidate ID that remains valid across repair rounds."""
    raw = f"{story_id}_final_edit_{run_id}"
    if len(raw) <= CANDIDATE_ID_LIMIT:
        return raw
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    suffix = f"_final_edit_{run_id[-24:]}_{digest}"
    if len(suffix) >= CANDIDATE_ID_LIMIT:
        return f"candidate_final_edit_{digest}"
    return f"{story_id[:CANDIDATE_ID_LIMIT - len(suffix)]}{suffix}"


def _require_safe_id(value, label):
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise FinalEditorContractError(f"{label} must be a safe non-empty id")
    return value


def _require_hash(value, label):
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise FinalEditorContractError(f"{label} must be a lowercase SHA-256 hash")
    return value


def _chapter_number(filename):
    match = CHAPTER_FILE.fullmatch(filename or "")
    if not match:
        raise FinalEditorContractError(f"invalid chapter filename {filename!r}")
    return int(match.group(1))


def chapter_manifest(chapter_texts):
    """Create an ordered, contiguous source manifest from chapter text."""
    if not isinstance(chapter_texts, dict) or not chapter_texts:
        raise FinalEditorContractError("chapter_texts must be a non-empty filename-to-text mapping")
    rows = []
    for filename, text in chapter_texts.items():
        number = _chapter_number(filename)
        if not isinstance(text, str):
            raise FinalEditorContractError(f"{filename} must contain text")
        words = count_words(text)
        if words < 1:
            raise FinalEditorContractError(f"{filename} must contain at least one word")
        rows.append({
            "file": filename,
            "chapter": number,
            "source_sha256": sha256_text(text),
            "words": words,
        })
    rows.sort(key=lambda item: item["chapter"])
    expected = list(range(1, len(rows) + 1))
    actual = [item["chapter"] for item in rows]
    if actual != expected:
        raise FinalEditorContractError("chapter files must be contiguous from ch0001.md")
    return rows


def _validate_word_budget(value):
    if not isinstance(value, dict):
        raise FinalEditorContractError("word_budget must be an object")
    chapter_delta = value.get("per_chapter_max_delta")
    book_delta = value.get("book_max_delta")
    for label, delta, maximum in (
        ("word_budget.per_chapter_max_delta", chapter_delta, MAX_PER_CHAPTER_WORD_DELTA),
        ("word_budget.book_max_delta", book_delta, MAX_BOOK_WORD_DELTA),
    ):
        if not isinstance(delta, (int, float)) or isinstance(delta, bool) or not 0 <= delta <= maximum:
            raise FinalEditorContractError(f"{label} must be between 0 and {maximum}")
    return {
        "per_chapter_max_delta": float(chapter_delta),
        "book_max_delta": float(book_delta),
    }


def build_contract(story_id, run_id, chapter_texts, *, created_at=None,
                   source_artifacts=None,
                   per_chapter_max_delta=MAX_PER_CHAPTER_WORD_DELTA,
                   book_max_delta=MAX_BOOK_WORD_DELTA):
    """Build a final-editor contract without reading or modifying any files."""
    _require_safe_id(story_id, "story_id")
    _require_safe_id(run_id, "run_id")
    manifest = chapter_manifest(chapter_texts)
    artifacts = {}
    for name, text in (source_artifacts or {}).items():
        _require_safe_id(name, "source_artifact name")
        if not isinstance(text, str):
            raise FinalEditorContractError(f"source artifact {name!r} must contain text")
        artifacts[name] = sha256_text(text)
    derived_candidate_story_id = candidate_story_id(story_id, run_id)
    contract = {
        "schema_version": SCHEMA_VERSION,
        "story_id": story_id,
        "run_id": run_id,
        "created_at": created_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": {
            "chapter_count": len(manifest),
            "book_words": sum(item["words"] for item in manifest),
            "chapters": manifest,
            "artifact_sha256": artifacts,
        },
        "candidate": {
            "story_id": derived_candidate_story_id,
            "source_mutation": "forbidden",
        },
        "locked_invariants": {key: True for key in REQUIRED_INVARIANTS},
        "word_budget": {
            "per_chapter_max_delta": per_chapter_max_delta,
            "book_max_delta": book_max_delta,
        },
        "required_verifications": list(REQUIRED_VERIFICATIONS),
    }
    return validate_contract(contract, expected_story_id=story_id, expected_manifest=manifest)


def _validate_manifest(rows, label):
    if not isinstance(rows, list) or not rows:
        raise FinalEditorContractError(f"{label} must be a non-empty chapter list")
    seen = set()
    expected_number = 1
    normalized = []
    for item in rows:
        if not isinstance(item, dict):
            raise FinalEditorContractError(f"{label} contains an invalid chapter record")
        filename = item.get("file")
        chapter = _chapter_number(filename)
        if chapter != expected_number or chapter in seen:
            raise FinalEditorContractError(f"{label} must use ordered contiguous chapters")
        if item.get("chapter") != chapter:
            raise FinalEditorContractError(f"{label} chapter number does not match filename")
        _require_hash(item.get("source_sha256"), f"{label}.{filename}.source_sha256")
        words = item.get("words")
        if not isinstance(words, int) or isinstance(words, bool) or words < 1:
            raise FinalEditorContractError(f"{label}.{filename}.words must be a positive integer")
        normalized.append({
            "file": filename,
            "chapter": chapter,
            "source_sha256": item["source_sha256"],
            "words": words,
        })
        seen.add(chapter)
        expected_number += 1
    return normalized


def validate_contract(contract, *, expected_story_id=None, expected_manifest=None):
    """Validate an immutable final-editor contract and optional source snapshot."""
    if not isinstance(contract, dict):
        raise FinalEditorContractError("contract must be an object")
    if contract.get("schema_version") != SCHEMA_VERSION:
        raise FinalEditorContractError(f"unsupported schema_version {contract.get('schema_version')!r}")
    story_id = _require_safe_id(contract.get("story_id"), "story_id")
    if expected_story_id and story_id != expected_story_id:
        raise FinalEditorContractError("story_id mismatch")
    _require_safe_id(contract.get("run_id"), "run_id")
    if not isinstance(contract.get("created_at"), str) or not contract["created_at"].strip():
        raise FinalEditorContractError("created_at must be a non-empty string")

    source = contract.get("source")
    if not isinstance(source, dict):
        raise FinalEditorContractError("source must be an object")
    manifest = _validate_manifest(source.get("chapters"), "source.chapters")
    if source.get("chapter_count") != len(manifest):
        raise FinalEditorContractError("source.chapter_count does not match source.chapters")
    book_words = sum(item["words"] for item in manifest)
    if source.get("book_words") != book_words:
        raise FinalEditorContractError("source.book_words does not match source.chapters")
    if expected_manifest is not None and manifest != _validate_manifest(expected_manifest, "expected_manifest"):
        raise FinalEditorContractError("source manifest changed; rebuild the final-editor contract")

    artifacts = source.get("artifact_sha256")
    if not isinstance(artifacts, dict):
        raise FinalEditorContractError("source.artifact_sha256 must be an object")
    for name, digest in artifacts.items():
        _require_safe_id(name, "source artifact name")
        _require_hash(digest, f"source artifact {name}")

    candidate = contract.get("candidate")
    if not isinstance(candidate, dict):
        raise FinalEditorContractError("candidate must be an object")
    candidate_story_id = _require_safe_id(candidate.get("story_id"), "candidate.story_id")
    if candidate_story_id == story_id:
        raise FinalEditorContractError("candidate.story_id must differ from story_id")
    if candidate.get("source_mutation") != "forbidden":
        raise FinalEditorContractError("candidate.source_mutation must be forbidden")

    invariants = contract.get("locked_invariants")
    if not isinstance(invariants, dict) or set(invariants) != set(REQUIRED_INVARIANTS):
        raise FinalEditorContractError("locked_invariants must contain the required invariant keys")
    if any(value is not True for value in invariants.values()):
        raise FinalEditorContractError("all locked invariants must be true")

    _validate_word_budget(contract.get("word_budget"))
    checks = contract.get("required_verifications")
    if not isinstance(checks, list) or set(checks) != set(REQUIRED_VERIFICATIONS):
        raise FinalEditorContractError("required_verifications must contain the required checks")
    return contract


def word_budget_report(contract, candidate_texts):
    """Compare candidate word counts without accepting or writing a candidate."""
    validate_contract(contract)
    candidate_manifest = chapter_manifest(candidate_texts)
    source_manifest = contract["source"]["chapters"]
    if [item["file"] for item in candidate_manifest] != [item["file"] for item in source_manifest]:
        raise FinalEditorContractError("candidate chapters must match the source chapter set and order")
    per_limit = contract["word_budget"]["per_chapter_max_delta"]
    book_limit = contract["word_budget"]["book_max_delta"]
    chapters = []
    for source, candidate in zip(source_manifest, candidate_manifest):
        delta = (candidate["words"] - source["words"]) / source["words"]
        chapters.append({
            "file": source["file"],
            "source_words": source["words"],
            "candidate_words": candidate["words"],
            "delta": delta,
            "within_budget": abs(delta) <= per_limit,
        })
    source_words = contract["source"]["book_words"]
    candidate_words = sum(item["words"] for item in candidate_manifest)
    book_delta = (candidate_words - source_words) / source_words
    return {
        "source_words": source_words,
        "candidate_words": candidate_words,
        "book_delta": book_delta,
        "book_within_budget": abs(book_delta) <= book_limit,
        "chapters": chapters,
        "accepted": abs(book_delta) <= book_limit and all(item["within_budget"] for item in chapters),
    }
