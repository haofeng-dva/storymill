# -*- coding: utf-8 -*-
"""Frozen, atomic acceptance contracts for one fiction chapter."""
import hashlib
import json
import re


SCHEMA_VERSION = "chapter_acceptance_contract_v1"
ARTIFACT_NAME = "chapter_acceptance_contract.json"
ITEM_TYPES = {"event", "fact", "dialogue", "context", "recognition"}
MAX_REQUIREMENTS_PER_CHAPTER = 8


class ChapterContractError(ValueError):
    """Raised when a chapter contract cannot safely constrain production."""


def planning_hash(direction, outline, world_bible):
    source = "\0".join(((direction or "").strip(), outline or "", world_bible or ""))
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _require_string(value, label, minimum=1, maximum=500):
    if not isinstance(value, str) or not value.strip():
        raise ChapterContractError(f"{label} must be a non-empty string")
    value = value.strip()
    if not minimum <= len(value) <= maximum:
        raise ChapterContractError(f"{label} must be {minimum}-{maximum} characters")
    return value


def _chapter_key(chapter_no):
    return f"{int(chapter_no):04d}"


def validate_contract(document, story_id, chapter_numbers, expected_source_sha256, outline_text):
    """Validate a compact, frozen contract without interpreting chapter prose."""
    if not isinstance(document, dict):
        raise ChapterContractError("chapter contract must be an object")
    required_top_level = {"schema_version", "story_id", "source_sha256", "chapters"}
    if set(document) != required_top_level:
        raise ChapterContractError("chapter contract has an invalid top-level shape")
    if document.get("schema_version") != SCHEMA_VERSION:
        raise ChapterContractError("unsupported chapter contract schema")
    if document.get("story_id") != story_id:
        raise ChapterContractError("chapter contract story_id mismatch")
    if document.get("source_sha256") != expected_source_sha256:
        raise ChapterContractError("chapter contract source hash mismatch")
    chapters = document.get("chapters")
    if not isinstance(chapters, dict):
        raise ChapterContractError("chapter contract chapters must be an object")
    expected_keys = {_chapter_key(number) for number in chapter_numbers}
    if set(chapters) != expected_keys:
        raise ChapterContractError("chapter contract does not cover exactly the requested chapters")
    seen_ids = set()
    for chapter_no in chapter_numbers:
        key = _chapter_key(chapter_no)
        chapter = chapters[key]
        if not isinstance(chapter, dict) or set(chapter) != {"chapter", "requirements"}:
            raise ChapterContractError(f"chapter contract {key} has an invalid shape")
        if chapter.get("chapter") != int(chapter_no):
            raise ChapterContractError(f"chapter contract {key} has the wrong chapter number")
        requirements = chapter.get("requirements")
        if not isinstance(requirements, list) or not 1 <= len(requirements) <= MAX_REQUIREMENTS_PER_CHAPTER:
            raise ChapterContractError(f"chapter contract {key} must contain 1-{MAX_REQUIREMENTS_PER_CHAPTER} requirements")
        for index, requirement in enumerate(requirements, start=1):
            if not isinstance(requirement, dict) or set(requirement) != {"id", "type", "requirement", "source_quote"}:
                raise ChapterContractError(f"chapter contract {key} requirement {index} has an invalid shape")
            expected_id = f"ch{int(chapter_no):04d}-contract-{index:02d}"
            if requirement.get("id") != expected_id or requirement["id"] in seen_ids:
                raise ChapterContractError(f"chapter contract {key} requirement ids must be stable and unique")
            seen_ids.add(requirement["id"])
            if requirement.get("type") not in ITEM_TYPES:
                raise ChapterContractError(f"chapter contract {key} requirement {index} has an invalid type")
            _require_string(requirement.get("requirement"), f"chapter contract {key} requirement {index}", 8, 360)
            source_quote = _require_string(
                requirement.get("source_quote"), f"chapter contract {key} requirement {index} source_quote", 8, 500,
            )
            if source_quote not in (outline_text or ""):
                raise ChapterContractError(f"chapter contract {key} requirement {index} source_quote is not in the outline")
    return document


def parse_compiler_response(content, story_id, chapter_numbers, expected_source_sha256, outline_text):
    """Parse and validate compiler JSON before it is allowed to guide prose."""
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", (content or "").strip(), flags=re.I)
    try:
        document = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ChapterContractError(f"chapter contract compiler returned invalid JSON: {exc}") from exc
    return validate_contract(document, story_id, chapter_numbers, expected_source_sha256, outline_text)


def requirements_for_chapter(document, chapter_no):
    """Return the exact acceptance input for a frozen chapter contract."""
    chapter = document["chapters"][_chapter_key(chapter_no)]
    return [
        {"id": item["id"], "requirement": item["requirement"]}
        for item in chapter["requirements"]
    ]


def writer_contract_block(document, chapter_no):
    """Render the frozen contract as compact hard requirements for the writer."""
    chapter = document["chapters"][_chapter_key(chapter_no)]
    rows = ["Frozen chapter contract. Make every item explicit; do not add an alternate outcome:"]
    rows.extend(
        f"- [{item['id']}] {item['type']}: {item['requirement']}"
        for item in chapter["requirements"]
    )
    return "\n".join(rows)
