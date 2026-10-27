# -*- coding: utf-8 -*-
"""Build the bounded, chapter-local prompt block for controlled generation."""
import json
import re
from pathlib import Path

from control_checks import validate_state_delta
from control_contracts import ControlContractError, MAX_CONTEXT_BYTES, validate_context_budget


# Two verified facts preserve the immediately actionable discrete state while
# bounding the writer's prior-state handoff.
MAX_PRIOR_FACTS_PER_CATEGORY = 2
MAX_PRIOR_FACT_CHARS = 180
MAX_PRIOR_HOOKS = 1
MAX_PRIOR_HOOK_CHARS = 80


def _chapter_key(chapter_no):
    if not isinstance(chapter_no, int) or chapter_no < 1:
        raise ControlContractError("chapter_no must be a positive integer")
    return f"{chapter_no:04d}"


def load_state_delta(control_dir, chapter_no):
    """Load an actual prior state delta or report its absence without inference."""
    path = Path(control_dir) / "state" / f"state_delta_ch{_chapter_key(chapter_no)}.json"
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ControlContractError(f"cannot read state delta {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ControlContractError(f"state delta {path} must be an object")
    if value.get("validation_status") == "failed":
        raise ControlContractError(f"prior state delta failed validation: {path}")
    try:
        validate_state_delta(value, chapter_no)
    except ControlContractError as exc:
        raise ControlContractError(f"prior state delta is invalid: {path}: {exc}") from exc
    return value


def _voice_cards_for_scene(cards, participants):
    wanted = {name.casefold() for name in participants}
    selected = [card for card in cards if card["name"].casefold() in wanted]
    missing = wanted - {card["name"].casefold() for card in selected}
    if missing:
        raise ControlContractError("scene participants lack voice cards: " + ", ".join(sorted(missing)))
    return selected


def _writer_scene(scene):
    """Keep only fields the writer needs; the full card stays on disk for checks."""
    return [
        scene["scene_id"], scene["conflict"], scene["choice"], scene["result"],
        scene["irreversible_change"], scene["required_information"],
        scene["completion_assertion"]["id"],
        [[item["id"], item["value"]] for item in scene["fact_slots"]],
    ]


def _writer_voice(card):
    # The full card remains auditable on disk. Keep only the immediate cue.
    return [card["name"], card["default_speech"], card["pressure_response_order"][:2], card["forbidden_generic_moves"][:1]]


def _previous_execution_view(delta):
    """Carry a bounded actual-state handoff; full history remains on disk."""
    def concise(values):
        kept = []
        for value in values or []:
            normalized = " ".join(value.split()) if isinstance(value, str) else ""
            if normalized and len(normalized) <= MAX_PRIOR_FACT_CHARS:
                kept.append(normalized)
            if len(kept) >= MAX_PRIOR_FACTS_PER_CATEGORY:
                break
        return kept

    return {
        "outcomes": [
            {"id": item["assertion_id"], "status": item["status"]}
            for item in delta.get("assertion_statuses", [])
        ],
        "retained": [
            {"id": item["assertion_id"], "status": item["status"]}
            for item in delta.get("non_regression_statuses", [])
        ],
        "new": concise(delta.get("new_information")),
        # Values are capped for the same reason new_information is: the control
        # block must have a provable worst case.  state_delta validation only
        # requires a non-empty string, so an uncapped value makes the byte
        # budget unbounded and lets a bundle pass preflight then fail at runtime.
        "facts": [
            {"id": item["fact_id"], "value": item["value"][:MAX_PRIOR_FACT_CHARS]}
            for item in delta.get("fact_statuses", [])
            if item.get("status") == "consistent"
            and isinstance(item.get("fact_id"), str)
            and isinstance(item.get("value"), str)
        ][:MAX_PRIOR_FACTS_PER_CATEGORY],
        "hooks": concise(delta.get("open_hooks"))[:MAX_PRIOR_HOOKS],
    }


def _strings_in(value):
    """Yield strings from a control value without interpreting their meaning."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings_in(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings_in(item)


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


def _normalized_matches(pattern, values):
    matches = set()
    for value in values:
        for match in pattern.findall(value):
            matches.add(" ".join(match.casefold().split()))
    return sorted(matches)


def build_repair_context(bundle, chapter_no, previous_state_delta=None):
    """Return the full, chapter-local evidence packet for bounded repairs.

    Writer context is intentionally compact. Repair needs the opposite tradeoff:
    it must be able to check a proposed localized change against every frozen
    current-chapter requirement, without importing future chapters or inventing
    planning facts.
    """
    key = _chapter_key(chapter_no)
    artifacts = bundle.get("artifacts", bundle)
    try:
        contract = artifacts["chapter_contracts"]["chapters"][key]
        scenes = artifacts["scene_ledger"]["chapters"][key]
    except (KeyError, TypeError) as exc:
        raise ControlContractError(f"missing current chapter repair data for {key}") from exc

    prior = None
    if chapter_no > 1 and previous_state_delta is not None:
        if not isinstance(previous_state_delta, dict):
            raise ControlContractError("previous_state_delta must be an object")
        prior = _previous_execution_view(previous_state_delta)

    scene_view = [{
        "scene_id": scene["scene_id"],
        "result": scene["result"],
        "irreversible_change": scene["irreversible_change"],
        "required_information": scene["required_information"],
        "completion_assertion": scene["completion_assertion"],
        "fact_slots": scene["fact_slots"],
    } for scene in scenes]
    frozen_values = [
        fact["value"]
        for scene in scenes
        for fact in scene["fact_slots"]
        if isinstance(fact.get("value"), str) and fact["value"]
    ]
    if prior:
        frozen_values.extend(item["value"] for item in prior["facts"])

    source_values = list(_strings_in({
        "pre_state": contract["pre_state"],
        "must_change": contract["must_change"],
        "end_assertions": contract["end_assertions"],
        "must_not_regress": contract["must_not_regress"],
        "facts": contract["facts"],
        "scenes": scene_view,
        "opening_contract": artifacts["opening_contract"] if chapter_no == 1 else None,
        "prior_state": prior,
    }))
    approved_clock_times = _normalized_matches(_CLOCK_TIME_RE, source_values)
    approved_durations = _normalized_matches(_DURATION_RE, source_values)
    return {
        "chapter": chapter_no,
        "contract": {
            "pre_state": contract["pre_state"],
            "must_change": contract["must_change"],
            "end_assertions": contract["end_assertions"],
            "must_not_regress": contract["must_not_regress"],
            "facts": contract["facts"],
        },
        "scenes": scene_view,
        "opening_contract": artifacts["opening_contract"] if chapter_no == 1 else None,
        "prior_state": prior,
        "frozen_literal_values": sorted(set(frozen_values)),
        "temporal_policy": {
            "approved_clock_times": approved_clock_times,
            "approved_duration_phrases": approved_durations,
            "no_unapproved_clock_times": True,
            "no_unapproved_more_precise_durations": True,
        },
    }


def build_control_context(bundle, chapter_no, previous_state_delta=None, max_bytes=MAX_CONTEXT_BYTES):
    """Return a bounded context block using only the current chapter's artifacts."""
    key = _chapter_key(chapter_no)
    artifacts = bundle.get("artifacts", bundle)
    try:
        contract = artifacts["chapter_contracts"]["chapters"][key]
        scenes = artifacts["scene_ledger"]["chapters"][key]
        cards = artifacts["character_voice_cards"]["characters"]
    except (KeyError, TypeError) as exc:
        raise ControlContractError(f"missing current chapter control data for {key}") from exc
    participants = []
    for scene in scenes:
        participants.extend(scene["participants"])
    selected_cards = _voice_cards_for_scene(cards, participants)
    # Full pre-state is retained in the JSON contract for planning only.
    # The writer receives the minimal execution view rather than duplicate data.
    context = {
        "h": chapter_no,
        "e": [[item["id"], item["claim"]] for item in contract["end_assertions"]],
        "k": [[item["id"], item["claim"], item["evidence_requirement"]] for item in contract["must_not_regress"]],
        "sc": [_writer_scene(scene) for scene in scenes],
        "vo": [_writer_voice(card) for card in selected_cards],
    }
    if chapter_no == 1:
        opening = artifacts["opening_contract"]
        context["o"] = [opening["visible_loss"], opening["forcing_event"], opening["first_core_evidence"], opening["background_serves_choice"], opening["max_setup_paragraphs_without_change"]]
    diagnostics = {
        "chapter": chapter_no,
        "state_delta_status": "not_required" if chapter_no == 1 else "missing",
        "selected_voice_cards": [card["name"] for card in selected_cards],
        "scene_count": len(scenes),
    }
    if chapter_no > 1 and previous_state_delta is not None:
        if not isinstance(previous_state_delta, dict):
            raise ControlContractError("previous_state_delta must be an object")
        context["pr"] = _previous_execution_view(previous_state_delta)
        diagnostics["state_delta_status"] = "available"
    text = (
        "e=[i,c];k=[i,c,p];sc=[i,c,o,r,g,n,d,[[i,v]]];vo=[n,s,p,a];o=[l,f,e,c,m];pr=prior with facts and hooks. Preserve facts; advance the open hook; realize each scene once.\n"
        + json.dumps(context, ensure_ascii=False, separators=(",", ":"))
    )
    diagnostics["context_bytes"] = validate_context_budget(text, max_bytes=max_bytes)
    return {"text": text, "diagnostics": diagnostics}
