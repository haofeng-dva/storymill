# -*- coding: utf-8 -*-
"""Validators for low-risk, memory-aware chapter patches."""
import hashlib
import json
import re
from collections import Counter


_CLOCK_TIME_RE = re.compile(
    r"\b(?:[01]?\d|2[0-3]):[0-5]\d(?:\s*(?:a\.?m\.?|p\.?m\.?))?(?=$|\W)", re.I,
)
_DURATION_RE = re.compile(
    r"\b(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\s+"
    r"(?:seconds?|minutes?|hours?|days?|weeks?|months?|years?)"
    r"(?:\s*(?:,?\s*and|,)\s*(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\s+"
    r"(?:seconds?|minutes?|hours?|days?|weeks?|months?|years?))?\b",
    re.I,
)


def _normalize(value):
    return " ".join(value.casefold().split())


def _reference_validation_errors(before, after, repair_context):
    """Reject local patches that break frozen facts or invent time precision."""
    if repair_context is None:
        return []
    if not isinstance(repair_context, dict):
        return ["repair reference context must be an object"]
    errors = []
    values = repair_context.get("frozen_literal_values", [])
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        return ["repair reference context has invalid frozen literal values"]
    for value in values:
        if value and value in before and value not in after:
            errors.append(f"repair removed frozen fact literal: {value}")

    policy = repair_context.get("temporal_policy", {})
    if not isinstance(policy, dict):
        return ["repair reference context has invalid temporal policy"]
    approved_times = policy.get("approved_clock_times", [])
    approved_durations = policy.get("approved_duration_phrases", [])
    if not isinstance(approved_times, list) or not isinstance(approved_durations, list):
        return ["repair reference context has invalid temporal approvals"]
    if not all(isinstance(value, str) for value in approved_times + approved_durations):
        return ["repair reference context has invalid temporal approvals"]
    approved_times = {_normalize(value) for value in approved_times}
    approved_durations = {_normalize(value) for value in approved_durations}
    if policy.get("no_unapproved_clock_times", False):
        prior_times = Counter(_normalize(found) for found in _CLOCK_TIME_RE.findall(before))
        for found in _CLOCK_TIME_RE.findall(after):
            normalized = _normalize(found)
            if prior_times[normalized]:
                prior_times[normalized] -= 1
                continue
            if normalized not in approved_times:
                errors.append(f"repair contains unapproved clock time: {found}")
    if policy.get("no_unapproved_more_precise_durations", False):
        prior_durations = Counter(
            _normalize(found) for found in _DURATION_RE.findall(before)
        )
        for found in _DURATION_RE.findall(after):
            normalized = _normalize(found)
            if prior_durations[normalized]:
                prior_durations[normalized] -= 1
                continue
            is_more_precise = " and " in normalized or "," in found
            if is_more_precise and normalized not in approved_durations:
                errors.append(f"repair contains unapproved precise duration: {found}")
    return errors

def local_validate(before, after):
    errors = []
    if not after or not after.strip():
        errors.append("empty chapter")
    if re.search(r"[\u4e00-\u9fff]", after):
        errors.append("non-English character remains")
    before_wc = len(re.findall(r"[A-Za-z']+", before or ""))
    after_wc = len(re.findall(r"[A-Za-z']+", after or ""))
    if before_wc and abs(after_wc - before_wc) > max(120, int(before_wc * 0.08)):
        errors.append(f"word count changed {before_wc}->{after_wc}")
    paras = [p.strip().casefold() for p in re.split(r"\n\s*\n", after) if p.strip()]
    if any(a == b and len(a) > 80 for a, b in zip(paras, paras[1:])):
        errors.append("adjacent duplicate paragraph")
    return errors


def _unique_offset(text, excerpt):
    first = text.find(excerpt)
    if first < 0 or text.find(excerpt, first + 1) >= 0:
        return None
    return first


def _is_complete_paragraph(text, start, end):
    """Require insertion anchors to name one whole source paragraph."""
    before, after = text[:start], text[end:]
    return (
        (not before or re.search(r"\n\s*\n\s*$", before) is not None)
        and (not after or re.match(r"^\s*\n\s*\n", after) is not None)
    )


def apply_scoped_patches(before, patches, allowed_anchors, insertion_anchors=(), repair_context=None):
    """Apply only verified replacements or small inserts at verified paragraphs.

    The model never chooses a free-form edit range: every ``find`` must be one
    of the exact reviewer quotations. An ``insert_after`` must be a unique
    whole paragraph provided by the acceptance reviewer for a missing outcome.
    Invalid proposals return the untouched source so callers can fail closed.
    """
    audit = {
        "before_sha256": hashlib.sha256((before or "").encode("utf-8")).hexdigest(),
        "allowed_anchors": list(allowed_anchors or []),
        "insertion_anchors": list(insertion_anchors or []),
        "patches": [],
    }
    if repair_context is not None:
        audit["repair_reference"] = {
            "chapter": repair_context.get("chapter") if isinstance(repair_context, dict) else None,
            "frozen_literal_values": repair_context.get("frozen_literal_values", []) if isinstance(repair_context, dict) else [],
            "temporal_policy": repair_context.get("temporal_policy", {}) if isinstance(repair_context, dict) else {},
        }
    if not isinstance(patches, list) or not patches:
        return {"accepted": False, "text": before, "audit": audit, "errors": ["repair returned no patches"]}
    anchors = {anchor for anchor in (allowed_anchors or []) if isinstance(anchor, str) and anchor}
    insert_anchors = {anchor for anchor in (insertion_anchors or []) if isinstance(anchor, str) and anchor}
    if not anchors and not insert_anchors:
        return {"accepted": False, "text": before, "audit": audit, "errors": ["repair has no verified anchors"]}

    replacements = []
    insertions = []
    used_insertion_anchors = set()
    errors = []
    for index, patch in enumerate(patches, start=1):
        if not isinstance(patch, dict):
            errors.append(f"patch {index} has an invalid shape")
            continue
        if set(patch) == {"find", "replace"}:
            find, replace = patch["find"], patch["replace"]
            if not isinstance(find, str) or not find or not isinstance(replace, str):
                errors.append(f"patch {index} has invalid text")
                continue
            if find not in anchors:
                errors.append(f"patch {index} is outside verified repair anchors")
                continue
            start = _unique_offset(before, find)
            if start is None:
                errors.append(f"patch {index} anchor is not uniquely locatable")
                continue
            if find == replace:
                errors.append(f"patch {index} makes no change")
                continue
            find_words = len(re.findall(r"[A-Za-z']+", find))
            replace_words = len(re.findall(r"[A-Za-z']+", replace))
            if replace_words > max(120, find_words * 8):
                errors.append(f"patch {index} exceeds its verified repair scope")
                continue
            replacements.append((start, start + len(find), find, replace, index))
            continue
        if set(patch) == {"insert_after", "insert"}:
            anchor, insert = patch["insert_after"], patch["insert"]
            if not isinstance(anchor, str) or not anchor or not isinstance(insert, str) or not insert.strip():
                errors.append(f"patch {index} has invalid insertion text")
                continue
            if anchor not in insert_anchors:
                errors.append(f"patch {index} is outside verified insertion anchors")
                continue
            if anchor in used_insertion_anchors:
                errors.append(f"patch {index} repeats a verified insertion anchor")
                continue
            start = _unique_offset(before, anchor)
            if start is None:
                errors.append(f"patch {index} insertion anchor is not uniquely locatable")
                continue
            if not _is_complete_paragraph(before, start, start + len(anchor)):
                errors.append(f"patch {index} insertion anchor is not a complete paragraph")
                continue
            insert = insert.strip()
            if re.search(r"\n\s*\n", insert) or len(re.findall(r"[A-Za-z']+", insert)) > 120:
                errors.append(f"patch {index} exceeds its verified insertion scope")
                continue
            used_insertion_anchors.add(anchor)
            insertions.append((start + len(anchor), anchor, insert, index))
            continue
        errors.append(f"patch {index} has an invalid shape")

    if errors:
        return {"accepted": False, "text": before, "audit": audit, "errors": errors}
    replacements.sort()
    for previous, current in zip(replacements, replacements[1:]):
        if current[0] < previous[1]:
            return {
                "accepted": False, "text": before, "audit": audit,
                "errors": [f"patch {current[4]} overlaps patch {previous[4]}"],
            }
    for position, _anchor, _insert, index in insertions:
        for start, end, _find, _replace, replacement_index in replacements:
            if start <= position <= end:
                return {
                    "accepted": False, "text": before, "audit": audit,
                    "errors": [f"patch {index} conflicts with patch {replacement_index}"],
                }

    after = before
    operations = [
        (start, "replace", end, find, replace, index)
        for start, end, find, replace, index in replacements
    ] + [
        (position, "insert", None, anchor, insert, index)
        for position, anchor, insert, index in insertions
    ]
    for position, kind, end, anchor, value, index in sorted(operations, reverse=True):
        if kind == "replace":
            after = after[:position] + value + after[end:]
            audit["patches"].append({"index": index, "find": anchor, "replace": value})
        else:
            after = after[:position] + "\n\n" + value + after[position:]
            audit["patches"].append({"index": index, "insert_after": anchor, "insert": value})
    audit["patches"].reverse()
    audit["after_sha256"] = hashlib.sha256(after.encode("utf-8")).hexdigest()
    validation_errors = local_validate(before, after) + _reference_validation_errors(before, after, repair_context)
    if validation_errors:
        return {"accepted": False, "text": before, "audit": audit, "errors": validation_errors}
    return {"accepted": True, "text": after, "audit": audit, "errors": []}

def parse_decision(content):
    raw = re.sub(r"^```(?:json)?|```$", "", (content or "").strip(), flags=re.M).strip()
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {"accept": False, "reason": "invalid decision shape"}
    except Exception:
        return {"accept": False, "reason": "validator returned invalid JSON"}
