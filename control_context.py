# -*- coding: utf-8 -*-
"""Build the bounded, chapter-local prompt block for controlled generation."""
import json
from pathlib import Path

from control_checks import validate_state_delta
from control_contracts import ControlContractError, MAX_CONTEXT_BYTES, validate_context_budget


# Two verified facts preserve the immediately actionable discrete state while
# leaving room for the current chapter's contract under the 2 KB hard limit.
MAX_PRIOR_FACTS_PER_CATEGORY = 2
MAX_PRIOR_FACT_CHARS = 180


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
        "facts": [
            {"id": item["fact_id"], "value": item["value"]}
            for item in delta.get("fact_statuses", [])
            if item.get("status") == "consistent"
            and isinstance(item.get("fact_id"), str)
            and isinstance(item.get("value"), str)
        ][:MAX_PRIOR_FACTS_PER_CATEGORY],
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
        "e=[i,c];k=[i,c,p];sc=[i,c,o,r,g,n,d,[[i,v]]];vo=[n,s,p,a];o=[l,f,e,c,m];pr=prior with facts. Preserve facts; realize each scene once.\n"
        + json.dumps(context, ensure_ascii=False, separators=(",", ":"))
    )
    diagnostics["context_bytes"] = validate_context_budget(text, max_bytes=max_bytes)
    return {"text": text, "diagnostics": diagnostics}
