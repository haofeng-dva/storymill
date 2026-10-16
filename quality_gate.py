# -*- coding: utf-8 -*-
"""Evidence-driven quality gate helpers.

The gate keeps blind review as one signal and combines it with deterministic
findings.  It does not turn a score into a claim that a chapter is issue-free.
"""
import hashlib
import json
import re
from pathlib import Path

import quality_checks
from repair_validator import local_validate


SCHEMA = "quality_gate_report_v1"
REVISION_SCHEMA = "quality_gate_revision_v1"

MARKETING_REVIEW_RULES = {
    "supernatural_reaction": {
        "category": "character_reaction",
        "severity": "blocking",
        "message": "A clear supernatural event may lack a credible immediate character reaction.",
        "required_action": "rewrite_chapter",
    },
    "relationship_basis": {
        "category": "relationship_basis",
        "severity": "important",
        "message": "Relationship escalation may rely on fated-body reactions without a concrete choice or shared consequence.",
        "required_action": "review_editorial",
    },
}


def _issue_id(chapter_no, index, finding):
    key = f"{chapter_no}:{index}:{finding.get('rule_id') or finding.get('category')}:{finding.get('excerpt', '')}"
    return "qg-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def _line_anchor(text, excerpt):
    if not excerpt or text.count(excerpt) != 1:
        return {}
    start = text.index(excerpt)
    return {
        "line_start": text.count("\n", 0, start) + 1,
        "line_end": text.count("\n", 0, start + len(excerpt)) + 1,
        "excerpt": excerpt,
    }


def _reviewer_excerpt(value):
    value = (value or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"\"", "'", "“", "”"}:
        value = value[1:-1].strip()
    return "" if value.casefold() == "none" else value


def blind_marketing_findings(text, blind, chapter_no):
    """Turn the existing blind-review response into evidence-bound marketing cards."""
    findings = []
    reason = (blind or {}).get("reason", "")
    for field, rule in MARKETING_REVIEW_RULES.items():
        status = re.search(rf"^{field}\s*:\s*(pass|review|not_applicable)\s*$", reason, re.I | re.M)
        if not status or status.group(1).casefold() != "review":
            continue
        evidence = re.search(rf"^{field}_evidence\s*:\s*(.+)$", reason, re.I | re.M)
        excerpt = _reviewer_excerpt(evidence.group(1) if evidence else "")
        finding = {
            "source": "blind_review",
            "rule_id": f"marketing-{field}",
            "root_cause_key": f"marketing-{field}",
            "category": rule["category"],
            "severity": rule["severity"],
            "message": rule["message"],
            "required_action": rule["required_action"],
            "status": "detected",
            "reviewer_evidence": excerpt,
        }
        anchor = _line_anchor(text, excerpt)
        if anchor:
            finding.update(anchor)
        else:
            finding["locator_status"] = "needs_human_location"
        finding["issue_id"] = _issue_id(chapter_no, len(findings) + 1, finding)
        findings.append(finding)
    return findings


def _state_summary(state):
    """Keep state evidence compact while retaining the fields used by checks."""
    if state is None:
        return None
    if not isinstance(state, dict):
        return {"status": "invalid_state_object"}
    return {
        "chapter": state.get("chapter"),
        "source_sha256": state.get("source_sha256"),
        "validation_status": state.get("validation_status"),
        "assertions": [
            {"id": item.get("assertion_id"), "status": item.get("status")}
            for item in state.get("assertion_statuses", [])
        ],
        "non_regressions": [
            {"id": item.get("assertion_id"), "status": item.get("status")}
            for item in state.get("non_regression_statuses", [])
        ],
        "scenes": [
            {"id": item.get("scene_id"), "status": item.get("status"), "count": item.get("occurrence_count")}
            for item in state.get("scene_statuses", [])
        ],
        "facts": [
            {"id": item.get("fact_id"), "status": item.get("status"), "value": item.get("value")}
            for item in state.get("fact_statuses", [])
        ],
        "relationship_changes": list(state.get("relationship_changes", [])),
        "open_hooks": list(state.get("open_hooks", [])),
    }


def _state_evidence(previous_state, current_state, text_hash):
    previous = _state_summary(previous_state)
    current = _state_summary(current_state)
    if current is None:
        return {
            "status": "not_provided",
            "previous": previous,
            "current": None,
            "changed": [],
        }
    changed = []
    if current.get("source_sha256") != text_hash:
        changed.append("current_state_source_hash_mismatch")
    if current.get("validation_status") != "passed":
        changed.append("current_state_not_validated")
    if previous is not None and previous.get("source_sha256") != current.get("source_sha256"):
        changed.append("chapter_state_advanced")
    return {
        "status": "verified" if not changed else "invalid",
        "previous": previous,
        "current": current,
        "changed": changed,
    }


def load_state_evidence(path):
    """Read a persisted state record for reporting, including failed records."""
    target = Path(path)
    if not target.is_file():
        return None
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def chapter_acceptance_decision(review, source_text=None, local_blockers=None, issue_ledger=None):
    """Make one fail-closed chapter decision from a source-hashed acceptance audit."""
    local_blockers = list(local_blockers or [])
    if not isinstance(review, dict) or review.get("error"):
        return {
            "decision": "verification_failed",
            "blockers": [review.get("error", "missing chapter acceptance review")] if isinstance(review, dict) else ["missing chapter acceptance review"],
        }
    if not isinstance(review.get("outcomes"), list) or not isinstance(review.get("findings"), list):
        return {"decision": "verification_failed", "blockers": ["acceptance review has no validated outcomes or findings"]}
    if source_text is not None and review.get("source_sha256") != hashlib.sha256(source_text.encode("utf-8")).hexdigest():
        return {"decision": "verification_failed", "blockers": ["acceptance review source hash mismatch"]}
    blockers = list(local_blockers)
    for outcome in review.get("outcomes", []):
        if outcome.get("status") != "met":
            blockers.append(f"outline outcome {outcome.get('id', '?')} is {outcome.get('status', 'missing')}")
    if issue_ledger is not None:
        ledger_items = issue_ledger.get("items") if isinstance(issue_ledger, dict) else None
        assessments = review.get("ledger_assessment")
        if not isinstance(ledger_items, list) or not isinstance(assessments, list):
            return {"decision": "verification_failed", "blockers": ["acceptance review has no validated initial issue ledger assessment"]}
        ledger_by_id = {item.get("id"): item for item in ledger_items if isinstance(item, dict) and item.get("id")}
        assessment_by_id = {item.get("id"): item for item in assessments if isinstance(item, dict) and item.get("id")}
        if (
            len(ledger_by_id) != len(ledger_items)
            or len(assessment_by_id) != len(assessments)
            or set(assessment_by_id) != set(ledger_by_id)
        ):
            return {"decision": "verification_failed", "blockers": ["acceptance ledger assessment does not cover the frozen initial issue ledger"]}
        repair_findings = []
        for item_id, original in ledger_by_id.items():
            assessment = assessment_by_id[item_id]
            if assessment.get("status") not in {"resolved", "unresolved"}:
                return {"decision": "verification_failed", "blockers": [f"acceptance ledger item {item_id} has an invalid status"]}
            if assessment["status"] == "unresolved":
                finding = {
                    "id": item_id,
                    "category": original.get("category"),
                    "repair_scope": original.get("repair_scope"),
                    "reason": assessment.get("reason"),
                    "evidence_quotes": assessment.get("evidence_quotes"),
                }
                if not isinstance(finding["reason"], str) or not finding["reason"].strip() or not isinstance(finding["evidence_quotes"], list) or not finding["evidence_quotes"]:
                    return {"decision": "verification_failed", "blockers": [f"unresolved acceptance ledger item {item_id} has no usable repair evidence"]}
                blockers.append(f"initial issue {item_id} is unresolved: {finding['reason']}")
                repair_findings.append(finding)
        # A recheck is not clean while it identifies a new, quote-verified
        # defect. Carry it into the remaining bounded repair budget instead
        # of recording it as an advisory observation and writing the chapter.
        for finding in review.get("findings", []):
            blockers.append(f"{finding.get('category', 'editorial finding')}: {finding.get('reason', 'repair required')}")
            repair_findings.append(finding)
        return {
            "decision": "passed" if not blockers else "repair_required",
            "blockers": blockers,
            "repair_findings": repair_findings,
            "observations": [],
        }
    for finding in review.get("findings", []):
        blockers.append(f"{finding.get('category', 'editorial finding')}: {finding.get('reason', 'repair required')}")
    return {
        "decision": "passed" if not blockers else "repair_required",
        "blockers": blockers,
        "repair_findings": list(review.get("findings", [])),
        "observations": [],
    }


def build_report(text, chapter_no, blind=None, previous_state=None, current_state=None, local_warnings=None,
                 state_failure=None, acceptance=None):
    """Build a stable report from blind output and local evidence."""
    blind = blind or {}
    findings = quality_checks.structured_quality_findings(text or "")
    issues = []
    for index, finding in enumerate(findings, 1):
        card = dict(finding)
        card["issue_id"] = _issue_id(chapter_no, index, finding)
        card["status"] = "detected"
        card["required_action"] = "review_local" if finding.get("severity") != "blocking" else "repair_required"
        issues.append(card)
    issues.extend(blind_marketing_findings(text or "", blind, chapter_no))
    score = blind.get("score")
    threshold = blind.get("threshold")
    if score is not None and threshold is not None and score < threshold:
        issues.append({
            "issue_id": f"qg-blind-ch{int(chapter_no):04d}",
            "source": "blind_review",
            "category": "naturalness",
            "severity": "blocking",
            "message": f"naturalness {score} is below gate {threshold}",
            "excerpt": "",
            "status": "detected",
            "required_action": "rewrite_chapter",
        })
    text_hash = hashlib.sha256((text or "").encode("utf-8")).hexdigest()
    fact_diff = _state_evidence(previous_state, current_state, text_hash)
    if state_failure or (current_state is not None and fact_diff["status"] != "verified"):
        issues.append({
            "issue_id": f"qg-state-ch{int(chapter_no):04d}",
            "source": "control_state",
            "category": "state",
            "severity": "blocking",
            "message": state_failure or "state delta does not match a validated current chapter",
            "excerpt": "",
            "status": "detected",
            "required_action": "regenerate_or_revalidate_chapter",
        })
    acceptance_gate = None
    if acceptance is not None:
        acceptance_gate = chapter_acceptance_decision(acceptance, text)
        for index, blocker in enumerate(acceptance_gate["blockers"], 1):
            issues.append({
                "issue_id": f"qg-acceptance-ch{int(chapter_no):04d}-{index:02d}",
                "source": "chapter_acceptance",
                "category": "chapter_acceptance",
                "severity": "blocking",
                "message": blocker,
                "excerpt": "",
                "status": "detected",
                "required_action": "repair_and_recheck_chapter",
            })
    blocking = sum(1 for item in issues if item.get("severity") == "blocking")
    decision = "repair_required" if blocking else ("review_required" if issues or local_warnings else "passed")
    return {
        "schema": SCHEMA,
        "chapter": int(chapter_no),
        "source_sha256": text_hash,
        "blind_review": blind,
        "issues": issues,
        "local_warnings": list(local_warnings or []),
        "fact_diff": fact_diff,
        "decision": decision,
        "evidence_complete": current_state is not None,
        "chapter_acceptance": acceptance_gate,
    }


def write_report(path, report):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def build_revision_manifest(chapter_no, before_text, after_text, before_report=None, after_report=None,
                            before_state=None, after_state=None):
    """Decide whether a repaired revision has enough evidence to replace its source.

    A controlled chapter cannot inherit a state delta from a different text
    hash. It must be re-extracted and revalidated before application.
    """
    before_hash = hashlib.sha256((before_text or "").encode("utf-8")).hexdigest()
    after_hash = hashlib.sha256((after_text or "").encode("utf-8")).hexdigest()
    validation_errors = local_validate(before_text, after_text)
    before_ids = {item.get("issue_id") for item in (before_report or {}).get("issues", [])}
    after_ids = {item.get("issue_id") for item in (after_report or {}).get("issues", [])}
    state_required = before_state is not None
    after_state_hash = (after_state or {}).get("source_sha256") if isinstance(after_state, dict) else None
    state_valid = not state_required or (
        after_state_hash == after_hash and (after_state or {}).get("validation_status") == "passed"
    )
    blockers = list(validation_errors)
    if state_required and not state_valid:
        blockers.append("controlled state must be regenerated and validated for the repaired text")
    return {
        "schema": REVISION_SCHEMA,
        "chapter": int(chapter_no),
        "before_sha256": before_hash,
        "after_sha256": after_hash,
        "resolved_issue_ids": sorted(before_ids - after_ids),
        "remaining_issue_ids": sorted(before_ids & after_ids),
        "introduced_issue_ids": sorted(after_ids - before_ids),
        "local_validation_errors": validation_errors,
        "state_revalidation": {
            "required": state_required,
            "passed": state_valid,
            "after_state_source_sha256": after_state_hash,
        },
        "decision": "accepted" if not blockers else "rollback_required",
        "blockers": blockers,
    }


def summarize_story(story_dir, chapter_files=None):
    """Summarize chapter gate reports without treating missing evidence as a pass."""
    root = Path(story_dir)
    files = chapter_files or sorted(path.name for path in root.glob("ch*.md"))
    chapters, blocking, review, missing, stale = [], [], [], [], []
    for filename in files:
        stem = Path(filename).stem
        text_path = root / filename
        report_path = root / f"{stem}.quality_gate.json"
        if not text_path.is_file() or not report_path.is_file():
            missing.append(filename)
            continue
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            missing.append(filename)
            continue
        source_hash = hashlib.sha256(text_path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
        item = {
            "file": filename,
            "decision": report.get("decision"),
            "source_sha256": report.get("source_sha256"),
            "state_status": (report.get("fact_diff") or {}).get("status"),
        }
        chapters.append(item)
        if item["source_sha256"] != source_hash or (
            report.get("evidence_complete") and item["state_status"] != "verified"
        ):
            stale.append(filename)
        if item["decision"] == "repair_required":
            blocking.append(filename)
        elif item["decision"] != "passed":
            review.append(filename)
    return {
        "schema": "quality_gate_story_summary_v1",
        "chapter_count": len(files),
        "reports_found": len(chapters),
        "chapters": chapters,
        "missing_reports": missing,
        "stale_reports": stale,
        "repair_required": blocking,
        "review_required": review,
        "release_ready": bool(files) and not (missing or stale or blocking or review),
    }
