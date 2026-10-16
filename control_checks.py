# -*- coding: utf-8 -*-
"""Local validation and findings for controlled-generation state deltas."""
import json
import re
from pathlib import Path

from control_contracts import ControlContractError, sha256_text


STATE_DELTA_REQUIRED = (
    "chapter", "source_sha256", "assertion_statuses", "non_regression_statuses", "new_information",
    "relationship_changes", "open_hooks", "conflicts_with_contract", "scene_statuses", "fact_statuses",
)


def _nonempty_string(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ControlContractError(f"{label} must be a non-empty string")


def _string_list(value, label, allow_empty=False):
    if (not isinstance(value, list)
            or (not allow_empty and not value)
            or any(not isinstance(item, str) or not item.strip() for item in value)):
        raise ControlContractError(f"{label} must be a list of non-empty strings")


def parse_state_delta_output(content, chapter, source_sha256, contract=None, chapter_text=None):
    text = (content or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3 and lines[-1].strip().startswith("```"):
            text = "\n".join(lines[1:-1]).strip()
    try:
        delta = json.loads(text)
    except json.JSONDecodeError as exc:
        # The reviewer occasionally prefixes an otherwise complete object
        # with a label or leaves a closing markdown fence off. Accept that
        # transport decoration only when exactly one JSON object remains.
        start = text.find("{")
        if start <= 0:
            raise ControlContractError(f"state-delta output is not valid JSON: {exc}") from exc
        try:
            delta, end = json.JSONDecoder().raw_decode(text[start:])
        except json.JSONDecodeError as nested:
            raise ControlContractError(f"state-delta output is not valid JSON: {nested}") from nested
        suffix = text[start + end:].strip()
        if suffix not in ("", "```"):
            raise ControlContractError("state-delta output has content after its JSON object") from exc
    validate_state_delta(delta, chapter, source_sha256)
    if contract is not None:
        findings = check_state_delta(contract, delta, chapter_text)
        if findings:
            messages = "; ".join(item["message"] for item in findings)
            raise ControlContractError(f"state-delta contract validation failed: {messages}")
    return delta


def validate_state_delta(delta, expected_chapter=None, expected_source_sha256=None):
    if not isinstance(delta, dict):
        raise ControlContractError("state delta must be an object")
    for field in STATE_DELTA_REQUIRED:
        if field not in delta:
            raise ControlContractError(f"state delta missing {field}")
    if expected_chapter is not None and delta["chapter"] != expected_chapter:
        raise ControlContractError("state delta chapter does not match written chapter")
    if not isinstance(delta["chapter"], int) or delta["chapter"] < 1:
        raise ControlContractError("state delta chapter must be a positive integer")
    _nonempty_string(delta["source_sha256"], "state delta source_sha256")
    if not re.fullmatch(r"[0-9a-f]{64}", delta["source_sha256"]):
        raise ControlContractError("state delta source_sha256 must be a lowercase SHA-256 hex digest")
    if expected_source_sha256 and delta["source_sha256"] != expected_source_sha256:
        raise ControlContractError("state delta source_sha256 does not match written chapter")
    for field in ("new_information", "relationship_changes", "open_hooks", "conflicts_with_contract"):
        # These are observations rather than required outcomes. An empty list
        # truthfully means the chapter made no change in that category.
        _string_list(delta[field], field, allow_empty=True)
    _validate_assertion_statuses(delta["assertion_statuses"], "assertion_statuses", ("met", "violated", "unclear"))
    _validate_assertion_statuses(delta["non_regression_statuses"], "non_regression_statuses", ("preserved", "violated", "unclear"))
    _validate_scene_statuses(delta["scene_statuses"])
    _validate_fact_statuses(delta["fact_statuses"])
    if "validation_status" in delta and delta["validation_status"] not in ("passed", "failed"):
        raise ControlContractError("validation_status is invalid")
    return delta


def _validate_assertion_statuses(statuses, label, allowed_statuses):
    if not isinstance(statuses, list):
        raise ControlContractError(f"{label} must be a list")
    seen = set()
    for index, status in enumerate(statuses):
        prefix = f"{label}[{index}]"
        if not isinstance(status, dict):
            raise ControlContractError(f"{prefix} must be an object")
        assertion_id = status.get("assertion_id")
        _nonempty_string(assertion_id, f"{prefix}.assertion_id")
        if assertion_id in seen:
            raise ControlContractError(f"duplicate {label} assertion_id {assertion_id!r}")
        seen.add(assertion_id)
        if status.get("status") not in allowed_statuses:
            raise ControlContractError(f"{prefix}.status is invalid")
        _nonempty_string(status.get("evidence"), f"{prefix}.evidence")


def _validate_scene_statuses(statuses):
    if not isinstance(statuses, list):
        raise ControlContractError("scene_statuses must be a list")
    seen = set()
    for index, item in enumerate(statuses):
        prefix = f"scene_statuses[{index}]"
        if not isinstance(item, dict):
            raise ControlContractError(f"{prefix} must be an object")
        scene_id = item.get("scene_id")
        _nonempty_string(scene_id, f"{prefix}.scene_id")
        if scene_id in seen:
            raise ControlContractError(f"duplicate scene_statuses scene_id {scene_id!r}")
        seen.add(scene_id)
        if item.get("status") not in ("completed", "repeated", "missing", "unclear"):
            raise ControlContractError(f"{prefix}.status is invalid")
        if not isinstance(item.get("occurrence_count"), int) or item["occurrence_count"] < 0:
            raise ControlContractError(f"{prefix}.occurrence_count must be a non-negative integer")
        _nonempty_string(item.get("evidence"), f"{prefix}.evidence")


def _validate_fact_statuses(statuses):
    if not isinstance(statuses, list):
        raise ControlContractError("fact_statuses must be a list")
    seen = set()
    for index, item in enumerate(statuses):
        prefix = f"fact_statuses[{index}]"
        if not isinstance(item, dict):
            raise ControlContractError(f"{prefix} must be an object")
        fact_id = item.get("fact_id")
        _nonempty_string(fact_id, f"{prefix}.fact_id")
        if fact_id in seen:
            raise ControlContractError(f"duplicate fact_statuses fact_id {fact_id!r}")
        seen.add(fact_id)
        if item.get("status") not in ("consistent", "contradicted", "unclear"):
            raise ControlContractError(f"{prefix}.status is invalid")
        _nonempty_string(item.get("value"), f"{prefix}.value")
        _nonempty_string(item.get("evidence"), f"{prefix}.evidence")


def _finding(chapter, rule_id, message, severity="blocking", **extra):
    finding = {
        "source": "control_state",
        "chapter": chapter,
        "category": "state",
        "severity": severity,
        "rule_id": rule_id,
        "root_cause_key": rule_id,
        "message": message,
    }
    finding.update(extra)
    return finding


def _assertion_findings(chapter, expected, observed, accepted_status, label, chapter_text):
    findings = []
    expected_by_id = {item["id"]: item for item in expected}
    observed_by_id = {item["assertion_id"]: item for item in observed}
    for assertion_id, assertion in expected_by_id.items():
        status = observed_by_id.get(assertion_id)
        if status is None:
            findings.append(_finding(chapter, f"missing-{label}-status", f"No status for assertion {assertion_id}: {assertion['claim']}"))
            continue
        # A delta reports an observed result, including failed/unclear results.
        # Its cited evidence is therefore always required to be a verbatim quote,
        # rather than only being checked for assertions the model calls successful.
        if chapter_text is not None and status["evidence"] not in chapter_text:
            findings.append(_finding(chapter, f"{label}-evidence-not-found", f"Evidence for assertion {assertion_id} is not a verbatim chapter quotation."))
        if status["status"] != accepted_status:
            findings.append(_finding(chapter, f"{label}-not-{accepted_status}", f"Assertion {assertion_id} is {status['status']}: {status['evidence']}"))
    for assertion_id in observed_by_id.keys() - expected_by_id.keys():
        findings.append(_finding(chapter, f"unexpected-{label}-status", f"Unknown assertion id reported: {assertion_id}"))
    return findings


def _scene_findings(chapter, scenes, statuses, chapter_text):
    findings, expected = [], {scene["scene_id"]: scene for scene in scenes}
    observed = {item["scene_id"]: item for item in statuses}
    for scene_id, scene in expected.items():
        item = observed.get(scene_id)
        if item is None:
            findings.append(_finding(chapter, "missing-scene-status", f"No status for scene {scene_id}.", scene_id=scene_id))
            continue
        if chapter_text is not None and item["evidence"] not in chapter_text:
            findings.append(_finding(chapter, "scene-evidence-not-found", f"Evidence for scene {scene_id} is not a verbatim chapter quotation.", scene_id=scene_id))
        if item["status"] != "completed" or item["occurrence_count"] != 1:
            findings.append(_finding(
                chapter,
                "scene-not-once",
                f"Scene {scene_id} is {item['status']} with occurrence_count={item['occurrence_count']}.",
                scene_id=scene_id,
                evidence=item["evidence"],
            ))
    for scene_id in observed.keys() - expected.keys():
        findings.append(_finding(chapter, "unexpected-scene-status", f"Unknown scene id reported: {scene_id}", scene_id=scene_id))
    return findings


def _fact_findings(chapter, scenes, statuses, chapter_text):
    findings = []
    expected = {fact["id"]: fact for scene in scenes for fact in scene["fact_slots"]}
    observed = {item["fact_id"]: item for item in statuses}
    for fact_id, fact in expected.items():
        item = observed.get(fact_id)
        if item is None:
            findings.append(_finding(chapter, "missing-fact-status", f"No status for fact slot {fact_id}: {fact['claim']}", fact_id=fact_id))
            continue
        if chapter_text is not None and item["evidence"] not in chapter_text:
            findings.append(_finding(chapter, "fact-evidence-not-found", f"Evidence for fact slot {fact_id} is not a verbatim chapter quotation.", fact_id=fact_id))
        # A discrete fact is only proven when the reported value appears in
        # the exact quotation that allegedly establishes it. This is local and
        # prevents a state extractor from labelling a conflicting quote as
        # "consistent" merely by copying the contract value into its JSON.
        if item["status"] in ("consistent", "contradicted") and item["value"] not in item["evidence"]:
            findings.append(_finding(
                chapter,
                "fact-evidence-value-not-found",
                f"Evidence for fact slot {fact_id} does not contain its reported value {item['value']!r}.",
                fact_id=fact_id,
                expected_value=fact["value"],
                observed_value=item["value"],
                evidence=item["evidence"],
            ))
        if item["status"] != "consistent" or item["value"] != fact["value"]:
            findings.append(_finding(
                chapter,
                "fact-value-conflict",
                f"Fact slot {fact_id} is {item['status']} with value {item['value']!r}; expected {fact['value']!r}.",
                fact_id=fact_id,
                expected_value=fact["value"],
                observed_value=item["value"],
                evidence=item["evidence"],
            ))
    for fact_id in observed.keys() - expected.keys():
        findings.append(_finding(chapter, "unexpected-fact-status", f"Unknown fact slot id reported: {fact_id}", fact_id=fact_id))
    return findings


def check_state_delta(contract, delta, chapter_text=None, scenes=None):
    """Validate atomic contract assertions without comparing free-form state prose."""
    findings = []
    chapter = delta["chapter"]
    findings.extend(_assertion_findings(chapter, contract.get("end_assertions", []), delta["assertion_statuses"], "met", "end-assertion", chapter_text))
    findings.extend(_assertion_findings(chapter, contract.get("must_not_regress", []), delta["non_regression_statuses"], "preserved", "non-regression", chapter_text))
    if scenes is not None:
        findings.extend(_scene_findings(chapter, scenes, delta["scene_statuses"], chapter_text))
        findings.extend(_fact_findings(chapter, scenes, delta["fact_statuses"], chapter_text))
    for conflict in delta["conflicts_with_contract"]:
        # This field is a model observation without an ID/value/quote contract.
        # It remains visible for review but cannot override the atomic checks.
        findings.append(_finding(
            chapter, "reported-contract-conflict", conflict,
            severity="manual_review_required",
        ))
    return findings


def evaluate_state_delta_output(content, chapter, source_sha256, contract, chapter_text, scenes=None):
    """Parse and validate one state-delta response through the only runtime path."""
    delta = parse_state_delta_output(content, chapter, source_sha256)
    return delta, check_state_delta(contract, delta, chapter_text, scenes)


def write_state_delta(control_dir, delta, findings):
    """Persist observed state plus a validation status; never rewrite chapter text."""
    control_root = Path(control_dir)
    path = control_root / "state" / f"state_delta_ch{delta['chapter']:04d}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    stored = dict(delta)
    stored["validation_status"] = "failed" if any(
        item.get("severity") == "blocking" for item in findings
    ) else "passed"
    path.write_text(json.dumps(stored, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def append_control_findings(output_path, findings):
    """Append de-duplicated control findings to an inspectable JSON report."""
    path = Path(output_path)
    existing = {"schema": "control_findings_v1", "findings": []}
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ControlContractError(f"cannot read existing findings {path}: {exc}") from exc
    known = {
        (item.get("chapter"), item.get("rule_id"), item.get("message")): index
        for index, item in enumerate(existing.get("findings", []))
    }
    for finding in findings:
        if not finding.get("card_id"):
            digest = sha256_text(f"{finding.get('chapter')}\0{finding.get('rule_id')}\0{finding.get('message')}")[:12]
            finding = {**finding, "card_id": f"ch{finding.get('chapter', 0):04d}-{finding.get('rule_id', 'finding')}-{digest}"}
        key = (finding.get("chapter"), finding.get("rule_id"), finding.get("message"))
        if key not in known:
            existing.setdefault("findings", []).append(finding)
            known[key] = len(existing["findings"]) - 1
        else:
            # A revalidation can legitimately refine a card from blocking to
            # manual review. Keep its stable id while replacing stale fields.
            prior = existing["findings"][known[key]]
            existing["findings"][known[key]] = {**prior, **finding}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(existing, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def check_prose_budget(text, budget, chapter, prior_book_counts=None):
    """Check only evidence-bound, local prose patterns and return updated counts."""
    prior_book_counts = dict(prior_book_counts or {})
    counts, findings = {}, []
    for rule in budget.get("rules", []):
        rule_id = rule["id"]
        count = len(re.findall(rule["pattern"], text or "", re.I))
        counts[rule_id] = count
        chapter_limit = budget.get("per_chapter_limits", {}).get(rule_id)
        if chapter_limit is not None and count > chapter_limit:
            findings.append({
                "source": "control_prose_budget", "chapter": chapter, "category": "prose_budget",
                "severity": "blocking", "rule_id": rule_id, "root_cause_key": rule_id,
                "message": f"{rule_id} count {count} exceeds chapter limit {chapter_limit}",
                "evidence_ids": rule["evidence_ids"],
            })
        total = prior_book_counts.get(rule_id, 0) + count
        book_limit = budget.get("book_limits", {}).get(rule_id)
        if book_limit is not None and total > book_limit:
            findings.append({
                "source": "control_prose_budget", "chapter": chapter, "category": "prose_budget",
                "severity": "blocking", "rule_id": f"book-{rule_id}", "root_cause_key": rule_id,
                "message": f"{rule_id} book count {total} exceeds book limit {book_limit}",
                "evidence_ids": rule["evidence_ids"],
            })
        prior_book_counts[rule_id] = total
    return findings, counts, prior_book_counts


def manual_review_findings(chapter, scenes, voice_cards, opening_contract=None):
    """Create non-passing cards where prose semantics cannot be proved locally."""
    findings = []
    if opening_contract is not None:
        findings.append({
            "source": "control_manual", "chapter": chapter, "category": "opening",
            "severity": "manual_review_required", "rule_id": "opening-contract-review",
            "root_cause_key": "opening-contract-review",
            "message": "Review visible loss, forcing event, first core evidence, and background-to-choice linkage.",
        })
    for scene in scenes:
        findings.append({
            "source": "control_manual", "chapter": chapter, "category": "scene",
            "severity": "manual_review_required", "rule_id": f"scene-{scene['scene_id']}",
            "root_cause_key": f"scene-{scene['scene_id']}",
            "message": f"Review whether scene {scene['scene_id']} realizes its choice, result, and irreversible change.",
        })
    for card in voice_cards:
        findings.append({
            "source": "control_manual", "chapter": chapter, "category": "voice",
            "severity": "manual_review_required", "rule_id": f"voice-{card['name'].casefold()}",
            "root_cause_key": f"voice-{card['name'].casefold()}",
            "message": f"Review whether {card['name']} follows the pressure response and avoids generic moves.",
        })
    return findings
