# -*- coding: utf-8 -*-
"""Schema and loading helpers for opt-in controlled fiction generation.

This module is deliberately local-only.  It validates compact JSON control
artifacts but does not call a model, read chapter prose, or modify a story.
"""
import hashlib
import json
import re
from pathlib import Path


SCHEMA_VERSION = "controlled_generation_v2"
MAX_CONTEXT_BYTES = 2048
# Compilation needs headroom for the bounded actual-state handoff that is only
# available after the preceding chapter has been written.
MAX_COMPILED_CONTEXT_BYTES = 1700
CONTROL_FILENAMES = {
    "chapter_contracts": "chapter_contracts.json",
    "scene_ledger": "scene_ledger.json",
    "opening_contract": "opening_contract.json",
    "character_voice_cards": "character_voice_cards.json",
    "prose_budget": "prose_budget.json",
}
REQUIRED_METADATA = ("schema_version", "story_id", "source_sha256", "created_at")
NON_DISCRETE_FACT_VALUE = re.compile(
    r"\b(?:all|any|every|many|several|some|various|shared|strange|tightly|loosely|worded)\b",
    re.I,
)


class ControlContractError(ValueError):
    """Raised when a control artifact cannot safely constrain generation."""


def sha256_text(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def source_hash(direction, outline, world_bible):
    """Hash the planning inputs shared by compiler and writer."""
    return sha256_text("\0".join(((direction or "").strip(), outline or "", world_bible or "")))


def context_size_bytes(text):
    return len((text or "").encode("utf-8"))


def validate_context_budget(text, max_bytes=MAX_CONTEXT_BYTES):
    size = context_size_bytes(text)
    if size > max_bytes:
        raise ControlContractError(
            f"control context is {size} bytes; limit is {max_bytes} bytes"
        )
    return size


def _require_mapping(value, label):
    if not isinstance(value, dict):
        raise ControlContractError(f"{label} must be an object")
    return value


def _require_nonempty_string(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ControlContractError(f"{label} must be a non-empty string")
    return value.strip()


def _require_string_list(value, label, allow_empty=False):
    if (not isinstance(value, list)
            or (not allow_empty and not value)
            or any(not isinstance(item, str) or not item.strip() for item in value)):
        raise ControlContractError(f"{label} must be a list of non-empty strings")
    return value


def _require_discrete_fact_value(value, label):
    """Reject vague descriptions that cannot be proven by one literal quote."""
    value = _require_nonempty_string(value, label)
    if NON_DISCRETE_FACT_VALUE.search(value):
        raise ControlContractError(
            f"{label} must be a concrete literal value, not a vague or explanatory description"
        )
    return value


def _validate_metadata(document, expected_story_id=None, expected_source_sha256=None):
    _require_mapping(document, "control document")
    for field in REQUIRED_METADATA:
        _require_nonempty_string(document.get(field), field)
    if document["schema_version"] != SCHEMA_VERSION:
        raise ControlContractError(
            f"unsupported schema_version {document['schema_version']!r}; expected {SCHEMA_VERSION!r}"
        )
    if expected_story_id and document["story_id"] != expected_story_id:
        raise ControlContractError(
            f"story_id mismatch: {document['story_id']!r} != {expected_story_id!r}"
        )
    if expected_source_sha256 and document["source_sha256"] != expected_source_sha256:
        raise ControlContractError("source_sha256 mismatch; recompile the control artifacts")


def _chapter_key(value, label):
    if not isinstance(value, str) or len(value) != 4 or not value.isdigit():
        raise ControlContractError(f"{label} must use a four-digit chapter key")
    return int(value)


def validate_chapter_contracts(document, expected_story_id=None, expected_source_sha256=None):
    _validate_metadata(document, expected_story_id, expected_source_sha256)
    chapters = _require_mapping(document.get("chapters"), "chapters")
    if not chapters:
        raise ControlContractError("chapters must not be empty")
    fact_ids = set()
    assertion_ids = set()
    for key, contract in chapters.items():
        chapter_no = _chapter_key(key, "chapter key")
        _require_mapping(contract, f"chapters.{key}")
        if contract.get("chapter") != chapter_no:
            raise ControlContractError(f"chapters.{key}.chapter must equal {chapter_no}")
        _require_mapping(contract.get("pre_state"), f"chapters.{key}.pre_state")
        _require_string_list(contract.get("must_change"), f"chapters.{key}.must_change", allow_empty=True)
        for field, required in (("end_assertions", True), ("must_not_regress", False)):
            assertions = contract.get(field)
            if not isinstance(assertions, list) or (required and not assertions):
                message = "must be a non-empty assertion list" if required else "must be an assertion list"
                raise ControlContractError(f"chapters.{key}.{field} {message}")
            for index, assertion in enumerate(assertions):
                prefix = f"chapters.{key}.{field}[{index}]"
                _require_mapping(assertion, prefix)
                assertion_id = _require_nonempty_string(assertion.get("id"), f"{prefix}.id")
                if assertion_id in assertion_ids:
                    raise ControlContractError(f"duplicate assertion id {assertion_id!r}")
                assertion_ids.add(assertion_id)
                _require_nonempty_string(assertion.get("claim"), f"{prefix}.claim")
                if field == "end_assertions" and assertion.get("required") is not True:
                    raise ControlContractError(f"{prefix}.required must be true")
                if field == "must_not_regress":
                    _require_nonempty_string(
                        assertion.get("evidence_requirement"),
                        f"{prefix}.evidence_requirement",
                    )
        facts = contract.get("facts")
        if not isinstance(facts, list):
            raise ControlContractError(f"chapters.{key}.facts must be a list")
        for index, fact in enumerate(facts):
            if not isinstance(fact, dict):
                raise ControlContractError(f"chapters.{key}.facts[{index}] must be an object")
            fact_id = _require_nonempty_string(fact.get("id"), f"chapters.{key}.facts[{index}].id")
            if fact_id in fact_ids:
                raise ControlContractError(f"duplicate fact id {fact_id!r}")
            fact_ids.add(fact_id)
    return document


def validate_scene_ledger(document, expected_story_id=None, expected_source_sha256=None):
    _validate_metadata(document, expected_story_id, expected_source_sha256)
    chapters = _require_mapping(document.get("chapters"), "chapters")
    seen_scene_ids = set()
    scene_assertion_ids = set()
    fact_slot_ids = set()
    reuse_values = {}
    for key, scenes in chapters.items():
        _chapter_key(key, "scene-ledger chapter key")
        if not isinstance(scenes, list) or not scenes:
            raise ControlContractError(f"chapters.{key} must be a non-empty scene list")
        for index, scene in enumerate(scenes):
            prefix = f"chapters.{key}[{index}]"
            _require_mapping(scene, prefix)
            scene_id = _require_nonempty_string(scene.get("scene_id"), f"{prefix}.scene_id")
            if scene_id in seen_scene_ids:
                raise ControlContractError(f"duplicate scene_id {scene_id!r}")
            seen_scene_ids.add(scene_id)
            for field in ("conflict", "choice", "result", "irreversible_change"):
                _require_nonempty_string(scene.get(field), f"{prefix}.{field}")
            completion = _require_mapping(scene.get("completion_assertion"), f"{prefix}.completion_assertion")
            completion_id = _require_nonempty_string(completion.get("id"), f"{prefix}.completion_assertion.id")
            if completion_id in scene_assertion_ids:
                raise ControlContractError(f"duplicate scene completion assertion id {completion_id!r}")
            scene_assertion_ids.add(completion_id)
            _require_nonempty_string(completion.get("claim"), f"{prefix}.completion_assertion.claim")
            fact_slots = scene.get("fact_slots")
            if not isinstance(fact_slots, list):
                raise ControlContractError(f"{prefix}.fact_slots must be a list")
            for fact_index, fact in enumerate(fact_slots):
                fact_prefix = f"{prefix}.fact_slots[{fact_index}]"
                _require_mapping(fact, fact_prefix)
                fact_id = _require_nonempty_string(fact.get("id"), f"{fact_prefix}.id")
                if fact_id in fact_slot_ids:
                    raise ControlContractError(f"duplicate scene fact slot id {fact_id!r}")
                fact_slot_ids.add(fact_id)
                _require_nonempty_string(fact.get("claim"), f"{fact_prefix}.claim")
                _require_discrete_fact_value(fact.get("value"), f"{fact_prefix}.value")
            participants = scene.get("participants")
            required_information = scene.get("required_information")
            _require_string_list(participants, f"{prefix}.participants")
            _require_string_list(required_information, f"{prefix}.required_information")
            reuse_key = scene.get("allowed_reuse_key", "")
            if reuse_key not in (None, ""):
                reuse_key = _require_nonempty_string(reuse_key, f"{prefix}.allowed_reuse_key")
                outcome = (scene["result"].strip(), scene["irreversible_change"].strip())
                if reuse_key in reuse_values and outcome == reuse_values[reuse_key]:
                    raise ControlContractError(
                        f"allowed_reuse_key {reuse_key!r} repeats the same result and irreversible_change"
                    )
                reuse_values[reuse_key] = outcome
    return document


def validate_opening_contract(document, expected_story_id=None, expected_source_sha256=None):
    _validate_metadata(document, expected_story_id, expected_source_sha256)
    for field in ("visible_loss", "forcing_event", "first_core_evidence", "background_serves_choice"):
        _require_nonempty_string(document.get(field), field)
    limit = document.get("max_setup_paragraphs_without_change")
    if not isinstance(limit, int) or limit < 1:
        raise ControlContractError("max_setup_paragraphs_without_change must be a positive integer")
    return document


def validate_character_voice_cards(document, expected_story_id=None, expected_source_sha256=None):
    _validate_metadata(document, expected_story_id, expected_source_sha256)
    cards = document.get("characters")
    if not isinstance(cards, list) or not cards:
        raise ControlContractError("characters must be a non-empty list")
    names = set()
    for index, card in enumerate(cards):
        prefix = f"characters[{index}]"
        _require_mapping(card, prefix)
        name = _require_nonempty_string(card.get("name"), f"{prefix}.name")
        if name.casefold() in names:
            raise ControlContractError(f"duplicate character voice card {name!r}")
        names.add(name.casefold())
        for field in ("default_speech", "exclusive_action_or_information"):
            _require_nonempty_string(card.get(field), f"{prefix}.{field}")
        for field in ("pressure_response_order", "failure_triggers", "forbidden_generic_moves"):
            _require_string_list(card.get(field), f"{prefix}.{field}")
    return document


def validate_prose_budget(document, expected_story_id=None, expected_source_sha256=None):
    _validate_metadata(document, expected_story_id, expected_source_sha256)
    rules = document.get("rules")
    if not isinstance(rules, list):
        raise ControlContractError("rules must be a list")
    rule_ids = set()
    for index, rule in enumerate(rules):
        prefix = f"rules[{index}]"
        _require_mapping(rule, prefix)
        rule_id = _require_nonempty_string(rule.get("id"), f"{prefix}.id")
        if rule_id in rule_ids:
            raise ControlContractError(f"duplicate prose budget rule {rule_id!r}")
        rule_ids.add(rule_id)
        _require_string_list(rule.get("evidence_ids"), f"{prefix}.evidence_ids")
        pattern = _require_nonempty_string(rule.get("pattern"), f"{prefix}.pattern")
        try:
            re.compile(pattern, re.I)
        except re.error as exc:
            raise ControlContractError(f"{prefix}.pattern is invalid: {exc}") from exc
    for field in ("per_chapter_limits", "book_limits"):
        limits = _require_mapping(document.get(field), field)
        for rule_id, limit in limits.items():
            if rule_id not in rule_ids:
                raise ControlContractError(f"{field} references unknown rule {rule_id!r}")
            if not isinstance(limit, int) or limit < 0:
                raise ControlContractError(f"{field}.{rule_id} must be a non-negative integer")
    return document


VALIDATORS = {
    "chapter_contracts": validate_chapter_contracts,
    "scene_ledger": validate_scene_ledger,
    "opening_contract": validate_opening_contract,
    "character_voice_cards": validate_character_voice_cards,
    "prose_budget": validate_prose_budget,
}


def validate_cross_artifact_references(artifacts):
    """Reject bundles whose scene participants cannot resolve to a voice card."""
    try:
        cards = artifacts["character_voice_cards"]["characters"]
        chapters = artifacts["scene_ledger"]["chapters"]
    except (KeyError, TypeError) as exc:
        raise ControlContractError("missing artifacts for cross-reference validation") from exc
    names = {card["name"].casefold() for card in cards}
    missing = sorted({
        participant for scenes in chapters.values() for scene in scenes
        for participant in scene["participants"] if participant.casefold() not in names
    }, key=str.casefold)
    if missing:
        raise ControlContractError(
            "scene participants must exactly match character voice-card names: " + ", ".join(missing)
        )
    return artifacts


def acceptance_requirements_for_chapter(bundle, chapter_no):
    """Expose the current control contract's required outcomes to chapter acceptance."""
    artifacts = bundle.get("artifacts", bundle) if isinstance(bundle, dict) else None
    key = f"{int(chapter_no):04d}"
    try:
        assertions = artifacts["chapter_contracts"]["chapters"][key]["end_assertions"]
    except (KeyError, TypeError) as exc:
        raise ControlContractError(f"missing control end assertions for chapter {key}") from exc
    if not isinstance(assertions, list) or not assertions:
        raise ControlContractError(f"control end assertions for chapter {key} must not be empty")
    requirements, seen = [], set()
    for index, assertion in enumerate(assertions, start=1):
        if not isinstance(assertion, dict):
            raise ControlContractError(f"control end assertion {key}.{index} must be an object")
        assertion_id = _require_nonempty_string(assertion.get("id"), f"control end assertion {key}.{index}.id")
        claim = _require_nonempty_string(assertion.get("claim"), f"control end assertion {key}.{index}.claim")
        if assertion.get("required") is not True or assertion_id in seen:
            raise ControlContractError(f"control end assertions for chapter {key} must be required and unique")
        seen.add(assertion_id)
        requirements.append({"id": assertion_id, "requirement": claim})
    return requirements


def load_control_file(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise ControlContractError(f"cannot read {path}: {exc}") from exc


def load_control_bundle(control_dir, story_id, source_sha256=None, allow_missing=True):
    """Load and validate an opt-in control bundle without touching story prose.

    Missing artifacts are reported rather than silently replaced.  The caller
    can reject the bundle by passing ``allow_missing=False``.
    """
    root = Path(control_dir)
    loaded, missing = {}, []
    for name, filename in CONTROL_FILENAMES.items():
        path = root / filename
        if not path.exists():
            missing.append(name)
            continue
        document = load_control_file(path)
        loaded[name] = VALIDATORS[name](document, story_id, source_sha256)
    if missing and not allow_missing:
        raise ControlContractError("missing control artifacts: " + ", ".join(missing))
    if not missing:
        validate_cross_artifact_references(loaded)
    return {
        "available": not missing,
        "missing": missing,
        "artifacts": loaded,
    }
