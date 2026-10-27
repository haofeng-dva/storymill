# -*- coding: utf-8 -*-
"""Deterministic post-write checks for the opt-in story bible.

The chapter plan already decides which canon facts a chapter actively uses.
This module only checks those facts.  It never requires every known fact to be
mentioned, and it never guesses at unstructured continuity from prose.
"""
import re

from story_bible import is_runtime_state_fact


def _has_literal(text, value):
    return re.search(r"(?<!\w)" + re.escape(value) + r"(?!\w)", text or "", re.IGNORECASE) is not None


def planned_fact_slots(document, chapter_no):
    """Expose chapter-used canon as semantic state-review slots.

    Canon values are structured summaries, not mandatory prose quotations.
    The state reviewer judges whether a verbatim chapter quote preserves or
    contradicts each value; local validation then checks its IDs, values and
    evidence.
    """
    chapter_key = f"{int(chapter_no):04d}"
    plan = document.get("chapter_plans", {}).get(chapter_key)
    if not isinstance(plan, dict):
        raise ValueError(f"story bible has no committed chapter plan for {chapter_key}")
    facts = {
        item.get("id"): item for item in document.get("facts", [])
        if isinstance(item, dict) and item.get("id")
    }
    used_ids = []
    for beat in plan.get("beats", []):
        for fact_id in beat.get("fact_ids", []):
            if fact_id not in used_ids:
                used_ids.append(fact_id)
    slots = []
    for fact_id in used_ids:
        fact = facts.get(fact_id)
        if fact is None:
            raise ValueError(f"chapter plan references unknown canon fact {fact_id!r}")
        if is_runtime_state_fact(fact):
            # A chapter can change an office or command. The actual prior
            # state, not the foundation's historical role label, is the
            # authority for that moving value.
            continue
        value = str(fact.get("value", "")).strip()
        if not value:
            raise ValueError(f"canon fact {fact_id!r} has no canonical value")
        slots.append({
            "id": f"story-bible--{fact_id}",
            "claim": f"Canon {fact.get('field', 'fact')} for {fact.get('subject_id', 'story')}: {value}",
            "value": value,
            "story_bible_fact_id": fact_id,
        })
    return slots


def audit_chapter_facts(document, chapter_no, text):
    """Return an inspectable audit for canon facts used by this chapter's beats.

    The audit declares the canon facts handed to state review. It must not
    demand a prose sentence duplicate a database summary word-for-word:
    dialogue or action may establish the same fact. Facts merely available in
    the bible are intentionally not checked here.
    """
    chapter_key = f"{int(chapter_no):04d}"
    plan = document.get("chapter_plans", {}).get(chapter_key)
    if not isinstance(plan, dict):
        raise ValueError(f"story bible has no committed chapter plan for {chapter_key}")
    slots = planned_fact_slots(document, chapter_no)
    used_ids = [slot["story_bible_fact_id"] for slot in slots]
    return {
        "schema": "story_bible_chapter_audit_v1",
        "chapter": int(chapter_no),
        "checked_fact_ids": used_ids,
        "findings": [],
        "state_review_fact_slots": slots,
        "decision": "deferred_to_state_delta" if used_ids else "not_applicable",
    }
