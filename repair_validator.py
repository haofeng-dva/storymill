# -*- coding: utf-8 -*-
"""Validators for low-risk, memory-aware chapter patches."""
import hashlib
import json
import re

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


def apply_scoped_patches(before, patches, allowed_anchors):
    """Apply only non-overlapping replacements anchored to verified source text.

    The model never chooses a free-form edit range: every ``find`` must be one
    of the exact reviewer quotations and occur exactly once in the source.
    Invalid proposals return the untouched source so callers can fail closed.
    """
    audit = {
        "before_sha256": hashlib.sha256((before or "").encode("utf-8")).hexdigest(),
        "allowed_anchors": list(allowed_anchors or []),
        "patches": [],
    }
    if not isinstance(patches, list) or not patches:
        return {"accepted": False, "text": before, "audit": audit, "errors": ["repair returned no patches"]}
    anchors = {anchor for anchor in (allowed_anchors or []) if isinstance(anchor, str) and anchor}
    if not anchors:
        return {"accepted": False, "text": before, "audit": audit, "errors": ["repair has no verified anchors"]}

    spans = []
    errors = []
    for index, patch in enumerate(patches, start=1):
        if not isinstance(patch, dict) or set(patch) != {"find", "replace"}:
            errors.append(f"patch {index} has an invalid shape")
            continue
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
        spans.append((start, start + len(find), find, replace, index))

    if errors:
        return {"accepted": False, "text": before, "audit": audit, "errors": errors}
    spans.sort()
    for previous, current in zip(spans, spans[1:]):
        if current[0] < previous[1]:
            return {
                "accepted": False, "text": before, "audit": audit,
                "errors": [f"patch {current[4]} overlaps patch {previous[4]}"],
            }

    after = before
    for start, end, find, replace, index in reversed(spans):
        after = after[:start] + replace + after[end:]
        audit["patches"].append({"index": index, "find": find, "replace": replace})
    audit["patches"].reverse()
    audit["after_sha256"] = hashlib.sha256(after.encode("utf-8")).hexdigest()
    validation_errors = local_validate(before, after)
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
