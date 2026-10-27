# -*- coding: utf-8 -*-
"""Independent acceptance checks for final-editor candidates."""
from final_editor_candidate import candidate_diff_summary
from final_editor_contract import FinalEditorContractError, validate_contract, word_budget_report
from final_editor_dossier import _contract_hash, dossier_is_usable
from final_editor_plan import plan_is_usable


class FinalEditorVerificationError(ValueError):
    """Raised when verification inputs cannot be tied to one candidate run."""


def _expected_finding_ids(plan):
    return sorted({finding_id for item in plan["items"] for finding_id in item["finding_ids"]})


def _expected_protected_fact_ids(dossier):
    return sorted(item["id"] for item in dossier.get("protected_facts", []))


def _validate_candidate_audit(audit, plan, candidate_texts):
    if not isinstance(audit, dict) or audit.get("schema_version") != "final_editor_candidate_v1":
        raise FinalEditorVerificationError("candidate audit schema is invalid")
    expected_items = [item["id"] for item in plan["items"]]
    waves = audit.get("waves")
    if not isinstance(waves, list) or [wave.get("plan_id") for wave in waves] != expected_items:
        raise FinalEditorVerificationError("candidate audit does not cover approved plan items in order")
    candidate_hashes = audit.get("candidate_chapter_sha256")
    if not isinstance(candidate_hashes, dict):
        raise FinalEditorVerificationError("candidate audit is missing candidate hashes")
    from final_editor_contract import sha256_text
    actual = {filename: sha256_text(text) for filename, text in candidate_texts.items()}
    if candidate_hashes != actual:
        raise FinalEditorVerificationError("candidate audit hashes do not match candidate text")
    if any(wave.get("status") != "applied" for wave in waves):
        raise FinalEditorVerificationError("candidate audit contains a non-applied repair wave")
    remediation_waves = audit.get("remediation_waves", [])
    if not isinstance(remediation_waves, list) or any(
            not isinstance(wave, dict) or wave.get("status") != "applied"
            for wave in remediation_waves):
        raise FinalEditorVerificationError("candidate audit contains an invalid remediation wave")


def _validate_review(review, candidate_texts, expected_findings, expected_protected_facts):
    if not isinstance(review, dict):
        raise FinalEditorVerificationError("independent review must be an object")
    reviewer_id = review.get("reviewer_id")
    if not isinstance(reviewer_id, str) or not reviewer_id.strip():
        raise FinalEditorVerificationError("independent review must identify its reviewer")
    from final_editor_contract import sha256_text
    hashes = {filename: sha256_text(text) for filename, text in candidate_texts.items()}
    if review.get("candidate_chapter_sha256") != hashes:
        raise FinalEditorVerificationError("independent review hashes do not match candidate text")
    blockers = review.get("blocking_findings")
    if not isinstance(blockers, list):
        raise FinalEditorVerificationError("independent review blocking_findings must be a list")
    assessments = review.get("finding_assessments")
    if not isinstance(assessments, list):
        raise FinalEditorVerificationError("independent review finding_assessments must be a list")
    found = {}
    for item in assessments:
        if not isinstance(item, dict) or item.get("finding_id") not in expected_findings:
            raise FinalEditorVerificationError("independent review has an unknown finding assessment")
        status = item.get("status")
        if status not in {"resolved", "unresolved"}:
            raise FinalEditorVerificationError("independent review finding assessment status is invalid")
        if item["finding_id"] in found:
            raise FinalEditorVerificationError("independent review repeats a finding assessment")
        found[item["finding_id"]] = status
    if set(found) != set(expected_findings):
        raise FinalEditorVerificationError("independent review must assess every planned finding")
    fact_assessments = review.get("protected_fact_assessments")
    if not isinstance(fact_assessments, list):
        raise FinalEditorVerificationError("independent review protected_fact_assessments must be a list")
    protected = {}
    for item in fact_assessments:
        if not isinstance(item, dict) or item.get("fact_id") not in expected_protected_facts:
            raise FinalEditorVerificationError("independent review has an unknown protected fact assessment")
        status = item.get("status")
        if status not in {"preserved", "changed"}:
            raise FinalEditorVerificationError("independent review protected fact assessment status is invalid")
        if item["fact_id"] in protected:
            raise FinalEditorVerificationError("independent review repeats a protected fact assessment")
        protected[item["fact_id"]] = status
    if set(protected) != set(expected_protected_facts):
        raise FinalEditorVerificationError("independent review must assess every protected fact")
    return {
        "reviewer_id": reviewer_id,
        "blocking_findings": blockers,
        "assessments": found,
        "protected_fact_assessments": protected,
    }


def verify_candidate(source_texts, candidate_texts, contract, dossier, plan, candidate_audit,
                     fact_findings, independent_review):
    """Return an evidence-based accept/reject decision without mutating files."""
    try:
        validate_contract(contract)
    except FinalEditorContractError as exc:
        raise FinalEditorVerificationError(str(exc)) from exc
    if not dossier_is_usable(dossier) or not plan_is_usable(plan):
        raise FinalEditorVerificationError("verification requires a usable dossier and repair plan")
    if dossier.get("contract_sha256") != _contract_hash(contract):
        raise FinalEditorVerificationError("dossier is not linked to the supplied contract")
    _validate_candidate_audit(candidate_audit, plan, candidate_texts)
    budget = word_budget_report(contract, candidate_texts)
    expected = _expected_finding_ids(plan)
    expected_facts = _expected_protected_fact_ids(dossier)
    review = _validate_review(independent_review, candidate_texts, expected, expected_facts)
    if not isinstance(fact_findings, dict) or not isinstance(fact_findings.get("findings"), list):
        raise FinalEditorVerificationError("fact_findings must use the fact-contract findings shape")
    fact_blockers = fact_findings["findings"]
    fact_status = fact_findings.get("status")
    unresolved = sorted(finding_id for finding_id, status in review["assessments"].items() if status == "unresolved")
    changed_facts = sorted(fact_id for fact_id, status in review["protected_fact_assessments"].items() if status == "changed")
    blockers = []
    if not budget["accepted"]:
        blockers.append("word_budget_failed")
    if fact_blockers:
        blockers.append("fact_contract_failed")
    if fact_status in {"not_configured", "missing_fact_contract"}:
        blockers.append("fact_contract_not_configured")
    if review["blocking_findings"]:
        blockers.append("independent_review_blocking_findings")
    if unresolved:
        blockers.append("planned_findings_unresolved")
    if changed_facts:
        blockers.append("protected_fact_changed")
    decision = "accepted" if not blockers else "rejected"
    return {
        "schema_version": "final_editor_verification_v1",
        "story_id": contract["story_id"],
        "decision": decision,
        "blockers": blockers,
        "needs_human_decision": {"finding_ids": [], "protected_fact_ids": [], "deferred_plan_items": []},
        "word_budget": budget,
        "fact_findings": fact_blockers,
        "independent_review": review,
        "change_inventory": candidate_diff_summary(source_texts, candidate_texts),
        "planned_finding_ids": expected,
    }
