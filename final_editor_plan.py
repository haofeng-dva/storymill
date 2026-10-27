# -*- coding: utf-8 -*-
"""Validate book-specific final-editor repair plans without model calls."""
import copy
import re

from final_editor_contract import FinalEditorContractError, validate_contract
from final_editor_dossier import dossier_is_usable


SCHEMA_VERSION = "final_editor_plan_v1"
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
DECISION_CONFIDENCE = {"high", "medium", "low"}


class FinalEditorPlanError(ValueError):
    """Raised when a repair plan cannot safely edit its candidate copy."""


def _require_text(value, label, minimum=1):
    if not isinstance(value, str) or len(value.strip()) < minimum:
        raise FinalEditorPlanError(f"{label} must be a non-empty string")
    return value


def _require_id(value, label):
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise FinalEditorPlanError(f"{label} must be a safe non-empty id")
    return value


def _valid_finding_map(dossier):
    if not dossier_is_usable(dossier):
        raise FinalEditorPlanError("repair planning requires a complete verified dossier")
    return {item["id"]: item for item in dossier.get("findings", [])}


def _validate_finding_ids(value, finding_map, seen):
    if not isinstance(value, list) or not value or value != sorted(set(value)):
        raise FinalEditorPlanError("finding_ids must be a sorted non-empty unique list")
    for finding_id in value:
        if finding_id not in finding_map:
            raise FinalEditorPlanError(f"unknown finding id {finding_id!r}")
        if finding_id in seen:
            raise FinalEditorPlanError(f"finding id {finding_id!r} has more than one disposition")
        seen.add(finding_id)
    return value


def _validate_chapter_targets(value, findings):
    expected = sorted({chapter for finding in findings for chapter in finding["chapters"]})
    if value != expected:
        raise FinalEditorPlanError("chapter_targets must exactly match the linked finding chapters")
    return value


def _validate_word_deltas(value, targets, per_limit):
    if not isinstance(value, dict) or set(value) != {str(chapter) for chapter in targets}:
        raise FinalEditorPlanError("allowed_word_deltas must contain exactly the targeted chapter keys")
    normalized = {}
    for key, delta in value.items():
        if not isinstance(delta, (int, float)) or isinstance(delta, bool) or not -per_limit <= delta <= per_limit:
            raise FinalEditorPlanError("allowed_word_deltas must stay within the contract limit")
        normalized[int(key)] = float(delta)
    return normalized


def verify_plan(proposal, dossier, contract):
    """Verify that every dossier finding has one bounded repair disposition."""
    try:
        validate_contract(contract)
    except FinalEditorContractError as exc:
        raise FinalEditorPlanError(str(exc)) from exc
    if not isinstance(proposal, dict) or proposal.get("story_id") != contract["story_id"]:
        raise FinalEditorPlanError("plan story_id does not match contract")
    finding_map = _valid_finding_map(dossier)
    if proposal.get("dossier_contract_sha256") != dossier.get("contract_sha256"):
        raise FinalEditorPlanError("plan dossier_contract_sha256 does not match dossier")
    per_limit = contract["word_budget"]["per_chapter_max_delta"]
    seen_findings, seen_ids, items, deferred, chapter_owners = set(), set(), [], [], {}

    for index, item in enumerate(proposal.get("items", []), 1):
        if not isinstance(item, dict):
            raise FinalEditorPlanError(f"plan item {index} must be an object")
        item_id = _require_id(item.get("id"), "plan item id")
        if item_id in seen_ids:
            raise FinalEditorPlanError(f"duplicate plan item id {item_id!r}")
        seen_ids.add(item_id)
        finding_ids = _validate_finding_ids(item.get("finding_ids"), finding_map, seen_findings)
        findings = [finding_map[finding_id] for finding_id in finding_ids]
        targets = _validate_chapter_targets(item.get("chapter_targets"), findings)
        for chapter in targets:
            if chapter in chapter_owners:
                raise FinalEditorPlanError(
                    f"chapter {chapter} belongs to both {chapter_owners[chapter]!r} and {item_id!r}; merge related findings"
                )
            chapter_owners[chapter] = item_id
        deltas = _validate_word_deltas(item.get("allowed_word_deltas"), targets, per_limit)
        _require_text(item.get("editorial_goal"), "editorial_goal", minimum=12)
        _require_text(item.get("expected_function_after"), "expected_function_after", minimum=8)
        _require_text(item.get("editor_instructions"), "editor_instructions", minimum=12)
        _require_text(item.get("editorial_decision"), "editorial_decision", minimum=12)
        _require_text(item.get("decision_basis"), "decision_basis", minimum=12)
        if item.get("decision_confidence") not in DECISION_CONFIDENCE:
            raise FinalEditorPlanError("decision_confidence must be high, medium, or low")
        checks = item.get("validation_checks")
        if not isinstance(checks, list) or set(checks) != set(contract["required_verifications"]):
            raise FinalEditorPlanError("validation_checks must contain every contract verification")
        items.append({
            "id": item_id,
            "finding_ids": finding_ids,
            "chapter_targets": targets,
            "allowed_word_deltas": deltas,
            "editorial_goal": item["editorial_goal"],
            "expected_function_after": item["expected_function_after"],
            "editor_instructions": item["editor_instructions"],
            "editorial_decision": item["editorial_decision"],
            "decision_basis": item["decision_basis"],
            "decision_confidence": item["decision_confidence"],
            "validation_checks": checks,
            "status": "editorial_decision_approved_for_candidate_only",
        })

    if proposal.get("deferred") not in ([], None):
        raise FinalEditorPlanError("final editor must decide every verified finding; deferred must be empty")

    unplanned = sorted(set(finding_map) - seen_findings)
    if unplanned:
        raise FinalEditorPlanError(f"every verified finding needs a disposition: {unplanned}")
    aggregate = {}
    for item in items:
        for chapter, delta in item["allowed_word_deltas"].items():
            aggregate[chapter] = aggregate.get(chapter, 0.0) + delta
    if any(abs(delta) > per_limit for delta in aggregate.values()):
        raise FinalEditorPlanError("combined allowed_word_deltas exceed the contract limit")
    return {
        "schema_version": SCHEMA_VERSION,
        "story_id": contract["story_id"],
        "dossier_contract_sha256": dossier["contract_sha256"],
        "items": items,
        "deferred": deferred,
        "aggregate_allowed_word_deltas": aggregate,
        "status": "verified",
    }


def plan_is_usable(plan):
    return isinstance(plan, dict) and plan.get("schema_version") == SCHEMA_VERSION and plan.get("status") == "verified"


def copy_verified_plan(plan):
    if not plan_is_usable(plan):
        raise FinalEditorPlanError("only a verified plan can enter candidate editing")
    return copy.deepcopy(plan)
