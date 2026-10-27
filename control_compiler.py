# -*- coding: utf-8 -*-
"""Compile opt-in controlled-generation artifacts from planning inputs."""
import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from control_contracts import (
    CONTROL_FILENAMES,
    ControlContractError,
    MAX_CONTEXT_BYTES,
    SCHEMA_VERSION,
    VALIDATORS,
    load_control_bundle,
    source_hash,
    validate_cross_artifact_references,
)
from control_context import (
    MAX_PRIOR_FACT_CHARS,
    MAX_PRIOR_FACTS_PER_CATEGORY,
    MAX_PRIOR_HOOK_CHARS,
    MAX_PRIOR_HOOKS,
    build_control_context,
)


ROOT = Path(__file__).resolve().parent


class ControlTransportError(ControlContractError):
    """A model request failed before it produced candidate control JSON."""


def _utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def build_compile_prompt(direction, outline, world_bible, chapter_count):
    return f"""Create a compact controlled-generation plan for an English serialized novel.
Return ONE valid JSON object only. Do not use Markdown or commentary.

The object must contain exactly these top-level keys:
chapter_contracts, scene_ledger, opening_contract, character_voice_cards, prose_budget.

chapter_contracts must have {{\"chapters\": {{\"0001\": {{
  \"chapter\": 1,
  \"pre_state\": {{\"time\": [], \"location\": [], \"characters\": {{}}, \"objects\": {{}}, \"known_information\": [], \"relationship_commitments\": []}},
  \"must_change\": [\"short concrete change\"],
  \"end_assertions\": [{{\"id\": \"unique-short-id\", \"claim\": \"short required outcome\", \"required\": true}}],
  \"must_not_regress\": [{{\"id\": \"unique-short-id\", \"claim\": \"short retained fact\", \"evidence_requirement\": \"short observable proof to write\"}}],
  \"facts\": [{{\"id\": \"unique-short-id\", \"claim\": \"short literal fact\", \"value\": \"one canonical value\"}}]
}}}}. Include chapters 0001 through {chapter_count:04d}.

scene_ledger must have {{\"chapters\": {{\"0001\": [{{
  \"scene_id\": \"unique-id\", \"conflict\": \"...\", \"choice\": \"...\",
  \"result\": \"...\", \"irreversible_change\": \"...\",
  \"completion_assertion\": {{\"id\": \"unique-id\", \"claim\": \"observable completed scene outcome\"}},
  \"fact_slots\": [{{\"id\": \"unique-id\", \"claim\": \"discrete fact to keep consistent\", \"value\": \"canonical value\"}}],
  \"allowed_reuse_key\": \"\", \"participants\": [\"name\"],
  \"required_information\": [\"new information\"]
}}]}}}}. Every scene needs a real irreversible change and one completion_assertion. Reused danger or information must use the same allowed_reuse_key only when its result or irreversible change differs. fact_slots may be empty, but use them only for each discrete value that would make a repeated scene contradictory, such as a sample count, named record set, granted permission, or time. A fact_slots.value must be a literal value a writer can copy exactly into one short quotation: a number, a name, a clock/date, a permission/access label, or a concrete state such as "sealed". Never use a summary, relation, quantity word, or adjective phrase such as "several former alphas", "shared by every pack member", or "tightly worded". When no literal finite value is established, use []. completion_assertion must restate only the scene's result or irreversible_change. Do not add incidental locations, gestures, named objects, timing, or actors unless that exact detail appears in the supplied outline or world bible and is necessary to the result. fact_slots must likewise contain only a discrete value explicitly established by the supplied planning inputs and necessary to the scene result; use [] rather than inventing a detail.

opening_contract must have visible_loss, forcing_event, first_core_evidence,
background_serves_choice, and max_setup_paragraphs_without_change.

character_voice_cards must have {{\"characters\": [{{
  \"name\": \"...\", \"default_speech\": \"...\",
  \"pressure_response_order\": [\"...\"], \"failure_triggers\": [\"...\"],
  \"forbidden_generic_moves\": [\"...\"],
  \"exclusive_action_or_information\": \"...\"
}}]}}.
Every scene participant must exactly equal one character_voice_cards character name, including titles or surnames. Do not use aliases.

prose_budget must have {{\"rules\": [], \"per_chapter_limits\": {{}}, \"book_limits\": {{}}}}.
Do not put scene counts, structural limits, or any invented rule in prose_budget.

Keep all strings short, factual, and usable as constraints. Each string must be 12 words or fewer.
Every end assertion must be a concrete outcome that can be quoted from the written chapter. Every retained assertion needs an evidence_requirement: a positive, observable on-page proof of preservation, not merely an absence of change. Assertion IDs must be unique across the book.
For every chapter, its final writer control context must fit in {MAX_CONTEXT_BYTES} UTF-8 bytes.
Do not include prose paragraphs, explanations, duplicate facts, or id-only facts. Every declared fact must carry id, claim, and literal value, and must appear identically in one scene fact_slots entry.

Story direction:
{direction}

Outline:
{outline}

World bible:
{world_bible}
"""


def parse_compiler_output(content):
    text = (content or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3 and lines[-1].strip().startswith("```"):
            text = "\n".join(lines[1:-1]).strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ControlContractError(f"compiler output is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ControlContractError("compiler output must be a JSON object")
    if set(payload) != set(CONTROL_FILENAMES):
        raise ControlContractError(
            "compiler output must contain exactly: " + ", ".join(CONTROL_FILENAMES)
        )
    # Evidence-bound prose budgets are deliberately owned by local review data,
    # not by the planning model.  Never adopt invented model rules as blockers.
    payload["prose_budget"] = {
        "rules": [], "per_chapter_limits": {}, "book_limits": {},
    }
    return payload


def build_bundle(payload, story_id, source_sha256, created_at=None, review_status="confirmed"):
    """Attach provenance metadata and validate all compiler-produced artifacts."""
    created_at = created_at or _utc_now()
    bundle = {}
    for name in CONTROL_FILENAMES:
        document = payload[name]
        if not isinstance(document, dict):
            raise ControlContractError(f"compiler field {name!r} must be an object")
        prepared = dict(document)
        prepared.update({
            "schema_version": SCHEMA_VERSION,
            "story_id": story_id,
            "source_sha256": source_sha256,
            "created_at": created_at,
            "review_status": review_status,
        })
        bundle[name] = VALIDATORS[name](prepared, story_id, source_sha256)
    validate_cross_artifact_references(bundle)
    contracts = bundle["chapter_contracts"]["chapters"]
    for key in contracts:
        chapter_no = int(key)
        try:
            if chapter_no == 1:
                build_control_context(bundle, chapter_no, max_bytes=MAX_CONTEXT_BYTES)
            else:
                prior = contracts[f"{chapter_no - 1:04d}"]
                # Byte reservation only: this view is measured, never validated.
                build_control_context(
                    bundle,
                    chapter_no,
                    _max_execution_view(prior),
                    max_bytes=MAX_CONTEXT_BYTES,
                )
        except ControlContractError as exc:
            raise ControlContractError(
                f"compiler output exceeds current-chapter context budget for {key}: {exc}"
            ) from exc
    return bundle


def _max_execution_view(contract):
    """Return a deliberately conservative *byte upper bound* for prior state.

    This is **not** a legal state delta and must never be validated as one: the
    ``max-fact-*`` identifiers reference no fact contract, and the previous
    chapter may declare no ``fact_slots`` at all.  It is a measuring stick, not
    a payload.

    The runtime projection is built from the model's reported state delta, whose
    facts have no schema length limit and are unrelated to the previous
    chapter's scene ``fact_slots`` (those are frequently empty — 11 of the 12
    chapters in the current bundle declare none).  Reserving the maximum
    ``_previous_execution_view`` can emit keeps compile-time preflight and the
    runtime writer measuring the same bytes, so preflight can never come out
    smaller than what the writer will actually build.

    If any future change starts feeding this value into a validator, it has to
    become a real delta first — see the note in ``build_bundle``.
    """
    return {
        "assertion_statuses": [
            {"assertion_id": item["id"], "status": "met"}
            for item in contract["end_assertions"]
        ],
        "non_regression_statuses": [
            {"assertion_id": item["id"], "status": "preserved"}
            for item in contract["must_not_regress"]
        ],
        "new_information": ["x" * MAX_PRIOR_FACT_CHARS] * MAX_PRIOR_FACTS_PER_CATEGORY,
        "fact_statuses": [
            {"fact_id": f"max-fact-{index}", "status": "consistent", "value": "x" * MAX_PRIOR_FACT_CHARS}
            for index in range(1, MAX_PRIOR_FACTS_PER_CATEGORY + 1)
        ],
        "open_hooks": ["x" * MAX_PRIOR_HOOK_CHARS] * MAX_PRIOR_HOOKS,
    }


def write_bundle(bundle, control_dir, replace=False):
    """Write a validated bundle without silently replacing any existing artifact."""
    root = Path(control_dir)
    paths = [root / filename for filename in CONTROL_FILENAMES.values()]
    existing = [str(path) for path in paths if path.exists()]
    if existing and not replace:
        raise ControlContractError("control artifacts already exist; use --replace-control: " + ", ".join(existing))
    root.mkdir(parents=True, exist_ok=True)
    for name, filename in CONTROL_FILENAMES.items():
        (root / filename).write_text(
            json.dumps(bundle[name], ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return root


def compile_bundle(direction, outline, world_bible, story_id, chapter_count, model_call):
    """Use exactly one injected planning call, then return a validated bundle."""
    prompt = build_compile_prompt(direction, outline, world_bible, chapter_count)
    response = model_call(prompt)
    if isinstance(response, dict):
        if response.get("error"):
            raise ControlContractError("control compiler model error: " + str(response["error"]))
        response = response.get("content", "")
    payload = parse_compiler_output(response)
    _synchronize_generated_facts(payload)
    return build_bundle(payload, story_id, source_hash(direction, outline, world_bible))


_CHAPTER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
}
_CHAPTER_HEADING = re.compile(r"(?im)^chapter\s+(\d+|[a-z]+)\b.*$")


def _chapter_sections(outline, chapter_count):
    """Extract only the named chapter plans for bounded compiler calls."""
    headings = []
    for match in _CHAPTER_HEADING.finditer(outline or ""):
        label = match.group(1).casefold()
        number = int(label) if label.isdigit() else _CHAPTER_WORDS.get(label)
        if number and 1 <= number <= chapter_count:
            headings.append((number, match.start(), match.end()))
    sections = {}
    for index, (number, start, _end) in enumerate(headings):
        next_start = headings[index + 1][1] if index + 1 < len(headings) else len(outline)
        sections[number] = outline[start:next_start].strip()
    missing = [str(number) for number in range(1, chapter_count + 1) if number not in sections]
    if missing:
        raise ControlContractError("outline lacks recognizable chapter sections: " + ", ".join(missing))
    return sections


def _json_object(content, required_keys, label):
    if isinstance(content, dict):
        payload = content
    else:
        text = (content or "").strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if len(lines) >= 3 and lines[-1].strip().startswith("```"):
                text = "\n".join(lines[1:-1]).strip()
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ControlContractError(f"{label} output is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict) or set(payload) != set(required_keys):
        raise ControlContractError(f"{label} output must contain exactly: " + ", ".join(required_keys))
    return payload


def _global_compile_prompt(direction, world_bible, correction=""):
    return f"""Create global controlled-generation data for an English serialized novel.
Return one valid JSON object only with exactly: opening_contract, character_voice_cards, prose_budget.

opening_contract needs visible_loss, forcing_event, first_core_evidence, background_serves_choice, max_setup_paragraphs_without_change.
character_voice_cards must be {{\"characters\":[{{\"name\":\"...\",\"default_speech\":\"...\",\"pressure_response_order\":[\"...\"],\"failure_triggers\":[\"...\"],\"forbidden_generic_moves\":[\"...\"],\"exclusive_action_or_information\":\"...\"}}]}}. Include every named character from the planning inputs, with exact names only.
prose_budget must be {{\"rules\":[],\"per_chapter_limits\":{{}},\"book_limits\":{{}}}}.
Keep strings short and factual. Do not invent facts.

Story direction:\n{direction}\n\nWorld bible:\n{world_bible}{correction}
"""


def _chapter_batch_prompt(direction, world_bible, sections, cards, correction=""):
    chapter_keys = [f"{number:04d}" for number in sorted(sections)]
    plans = "\n\n".join(sections[number] for number in sorted(sections))
    names = [card["name"] for card in cards]
    return f"""Create controlled chapter data for exactly chapters {', '.join(chapter_keys)}.
Return one valid JSON object only with exactly: chapter_contracts, scene_ledger.
chapter_contracts must be {{\"chapters\":{{\"0001\":{{\"chapter\":1,\"pre_state\":{{\"time\":[],\"location\":[],\"characters\":{{}},\"objects\":{{}},\"known_information\":[],\"relationship_commitments\":[]}},\"must_change\":[\"short change\"],\"end_assertions\":[{{\"id\":\"unique-id\",\"claim\":\"observable outcome\",\"required\":true}}],\"must_not_regress\":[{{\"id\":\"unique-id\",\"claim\":\"retained fact\",\"evidence_requirement\":\"observable proof\"}}],\"facts\":[{{\"id\":\"unique-id\",\"claim\":\"discrete fact\",\"value\":\"one literal canonical value\"}}]}}}}.
scene_ledger must be {{\"chapters\":{{\"0001\":[{{\"scene_id\":\"unique-id\",\"conflict\":\"...\",\"choice\":\"...\",\"result\":\"...\",\"irreversible_change\":\"...\",\"completion_assertion\":{{\"id\":\"unique-id\",\"claim\":\"observable result\"}},\"fact_slots\":[],\"allowed_reuse_key\":\"\",\"participants\":[\"exact card name\"],\"required_information\":[\"new information\"]}}]}}}}.
Include only the requested four-digit chapter keys. Every scene must change something irreversibly. Use only these exact participant names: {json.dumps(names)}. Keep all strings 12 words or fewer and do not invent facts or precise times. For every chapter_contracts.facts item, declare the identical id, claim, and literal value in exactly one relevant scene fact_slots item. If the chapter plan contains no literal continuity fact, use [] in both places; never put an id-only fact in chapter_contracts.

This request is deliberately chapter-local. Do not infer or add facts from any
unseen book plan. The chapter plans below are the only plot authority.

Chapter plans:\n{plans}{correction}
"""


def _single_chapter_artifact_prompt(sections, cards, artifact_name, correction=""):
    """Keep one chapter-control artifact within the relay's small-output band."""
    number = next(iter(sections))
    plan = sections[number]
    names = [card["name"] for card in cards]
    if artifact_name == "chapter_contracts":
        schema = (
            '{"chapter_contracts":{"chapters":{"0001":{"chapter":1,'
            '"pre_state":{"time":[],"location":[],"characters":{},"objects":{},'
            '"known_information":[],"relationship_commitments":[]},"must_change":["short change"],'
            '"end_assertions":[{"id":"unique-id","claim":"observable outcome","required":true}],'
            '"must_not_regress":[{"id":"unique-id","claim":"retained fact",'
            '"evidence_requirement":"observable proof"}],"facts":[{"id":"unique-id","claim":"discrete fact","value":"literal value"}]}}}}'
        )
    elif artifact_name == "scene_ledger":
        schema = (
            '{"scene_ledger":{"chapters":{"0001":[{"scene_id":"unique-id",'
            '"conflict":"...","choice":"...","result":"...","irreversible_change":"...",'
            '"completion_assertion":{"id":"unique-id","claim":"observable result"},'
            '"fact_slots":[],"allowed_reuse_key":"","participants":["exact card name"],'
            '"required_information":["new information"]}]}}}'
        )
    else:
        raise ControlContractError(f"unsupported chapter artifact: {artifact_name}")
    if artifact_name == "chapter_contracts":
        compactness = (
            "Use only necessary execution constraints: at most 3 must_change, "
            "3 end_assertions, 2 must_not_regress, and 4 facts."
        )
    else:
        compactness = (
            "Use at most 2 scenes, 2 fact_slots per scene, and 2 required_information "
            "items per scene; keep only values necessary to prevent contradiction."
        )
    return f"""Create only {artifact_name} for chapter {number:04d}.
Return one valid JSON object only matching this compact schema:
{schema}
Use only the chapter plan below. Keep all strings 12 words or fewer. Use only these exact participant names: {json.dumps(names)}. Do not invent facts, times, locations, or characters. A fact is valid only when its id, claim, and literal value can be copied identically into one relevant scene fact_slots item; otherwise use [] rather than an id-only fact. Never emit a partial fact_slots object: if any id, claim, or value would be blank, unknown, or non-literal, remove that object and use fact_slots: [] instead.
The final writer control context has a hard {MAX_CONTEXT_BYTES} UTF-8 byte limit. {compactness}
Preserve every necessary constraint by consolidating redundant wording; never add explanatory prose.

Chapter plan:\n{plan}{correction}
"""


def _cache_payload(cache_dir, source_digest, name, required_keys, label, validator=None):
    if not cache_dir:
        return None
    path = Path(cache_dir) / source_digest[:16] / f"{name}.json"
    if not path.is_file():
        return None
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(saved, dict) or saved.get("source_sha256") != source_digest:
        return None
    try:
        payload = _json_object(saved.get("payload"), required_keys, label)
        if validator:
            validator(payload)
        return payload
    except ControlContractError:
        return None


def _save_cache_payload(cache_dir, source_digest, name, payload):
    if not cache_dir:
        return
    path = Path(cache_dir) / source_digest[:16] / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "source_sha256": source_digest,
        "payload": payload,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _model_payload_once(model_call, prompt, max_tokens, required_keys, label):
    response = model_call(prompt, max_tokens)
    if isinstance(response, dict):
        if response.get("error"):
            error = (
                f"control compiler {label} model error: " + str(response["error"])
            )
            if response.get("failure_type") == "network_error":
                raise ControlTransportError(error)
            raise ControlContractError(error)
        response = response.get("content", "")
    return _json_object(response, required_keys, label)


def _retry_complete_payload(build_candidate, label, attempts):
    """Retry model-format/schema failures without concealing program errors."""
    correction = ""
    for attempt in range(1, attempts + 1):
        try:
            return build_candidate(correction)
        except ControlTransportError:
            # The model returned no candidate to correct. Its HTTP retry budget
            # is handled below the compiler; never turn a gateway page into a
            # schema instruction or spend a schema attempt on it.
            raise
        except ControlContractError as exc:
            if attempt < attempts:
                reason = str(exc)
                print(
                    f"[control] {label} attempt {attempt}/{attempts} rejected: {reason}",
                    flush=True,
                )
                correction = (
                    "\n\nPrevious attempt rejected by the required schema: "
                    f"{reason}\nReturn the same requested JSON shape, correcting this error only."
                )
                time.sleep(5)
                continue
            raise
    raise AssertionError("unreachable")


def _bounded_model_payload(model_call, prompt, max_tokens, required_keys, label, attempts):
    """Compatibility wrapper for callers that need only JSON-shape retries."""
    return _retry_complete_payload(
        lambda _correction: _model_payload_once(
            model_call, prompt, max_tokens, required_keys, label,
        ),
        label,
        attempts,
    )


def _normalize_global_payload(payload):
    """Prose budgets are local review data, never model-authored control rules."""
    payload["prose_budget"] = {
        "rules": [], "per_chapter_limits": {}, "book_limits": {},
    }
    return payload


def _checkpoint_document(payload, name, label):
    document = payload.get(name) if isinstance(payload, dict) else None
    if not isinstance(document, dict):
        raise ControlContractError(f"{label}.{name} must be an object")
    return dict(document)


def _validate_global_checkpoint(payload, story_id, source_digest):
    """Validate global data before it can enter a resumable checkpoint cache."""
    _normalize_global_payload(payload)
    timestamp = _utc_now()
    for name in ("opening_contract", "character_voice_cards", "prose_budget"):
        document = _checkpoint_document(payload, name, "global compiler")
        document.update({
            "schema_version": SCHEMA_VERSION, "story_id": story_id,
            "source_sha256": source_digest, "created_at": timestamp,
        })
        VALIDATORS[name](document, story_id, source_digest)


def _chapter_checkpoint_documents(payload, story_id, source_digest):
    """Attach temporary provenance required by the artifact validators."""
    timestamp = _utc_now()
    contracts = _checkpoint_document(payload, "chapter_contracts", "chapter compiler")
    ledger = _checkpoint_document(payload, "scene_ledger", "chapter compiler")
    contracts.update({
        "schema_version": SCHEMA_VERSION, "story_id": story_id,
        "source_sha256": source_digest, "created_at": timestamp,
    })
    ledger.update({
        "schema_version": SCHEMA_VERSION, "story_id": story_id,
        "source_sha256": source_digest, "created_at": timestamp,
    })
    return contracts, ledger


def _synchronize_generated_facts(payload):
    """Use generated scene slots as the single source for generated chapter facts.

    The compiler asks two bounded model calls for a contract and a ledger.  A
    ledger slot is already a complete, literal fact declaration, whereas the
    contract copy is prone to being omitted.  Copying the exact slot payload
    before validation prevents two independent calls from silently creating
    two incomplete authorities.  This is deliberately compiler-only:
    externally supplied bundles still face the strict bidirectional validator.
    """
    try:
        contracts = payload["chapter_contracts"]["chapters"]
        ledger = payload["scene_ledger"]["chapters"]
    except (KeyError, TypeError) as exc:
        raise ControlContractError("chapter compiler payload lacks contracts or scene ledger") from exc
    if set(contracts) != set(ledger):
        raise ControlContractError("chapter compiler contracts and scene ledger must use identical chapter keys")
    for key, scenes in ledger.items():
        declared = contracts[key].get("facts", [])
        if not isinstance(declared, list):
            raise ControlContractError(f"chapter {key} contract facts must be a list")
        slots = []
        for scene in scenes:
            for slot in scene.get("fact_slots", []):
                if not isinstance(slot, dict):
                    raise ControlContractError(f"chapter {key} scene fact slot must be an object")
                slots.append({field: slot.get(field) for field in ("id", "claim", "value")})
        if declared:
            declared_values = [
                {field: item.get(field) for field in ("id", "claim", "value")}
                for item in declared if isinstance(item, dict)
            ]
            if len(declared_values) != len(declared) or declared_values != slots:
                raise ControlContractError(
                    f"chapter {key} contract facts must match scene fact_slots exactly"
                )
        else:
            contracts[key]["facts"] = slots
    return payload


def _validate_chapter_contract_checkpoint(payload, story_id, source_digest):
    contracts, _ledger = _chapter_checkpoint_documents(payload, story_id, source_digest)
    VALIDATORS["chapter_contracts"](contracts, story_id, source_digest)


def _validate_scene_ledger_checkpoint(payload, story_id, source_digest):
    _contracts, ledger = _chapter_checkpoint_documents(payload, story_id, source_digest)
    VALIDATORS["scene_ledger"](ledger, story_id, source_digest)


def _validate_chapter_cross_references(payload, global_payload, story_id, source_digest):
    contracts, ledger = _chapter_checkpoint_documents(payload, story_id, source_digest)
    validate_cross_artifact_references({
        "chapter_contracts": contracts,
        "scene_ledger": ledger,
        "character_voice_cards": global_payload["character_voice_cards"],
    })


def _validate_chapter_checkpoint(payload, global_payload, story_id, source_digest):
    """Reject malformed generated data before it becomes a resumable checkpoint."""
    _synchronize_generated_facts(payload)
    _validate_chapter_contract_checkpoint(payload, story_id, source_digest)
    _validate_scene_ledger_checkpoint(payload, story_id, source_digest)
    _validate_chapter_cross_references(payload, global_payload, story_id, source_digest)


def _validate_chapter_checkpoint_with_context(payload, global_payload,
                                              accepted_contracts, accepted_ledger,
                                              story_id, source_digest):
    """Reject a checkpoint that cannot fit the writer's real control context."""
    _validate_chapter_checkpoint(payload, global_payload, story_id, source_digest)
    contracts = {
        **accepted_contracts,
        **payload["chapter_contracts"].get("chapters", {}),
    }
    ledger = {
        **accepted_ledger,
        **payload["scene_ledger"].get("chapters", {}),
    }
    build_bundle({
        "chapter_contracts": {"chapters": contracts},
        "scene_ledger": {"chapters": ledger},
        **global_payload,
    }, story_id, source_digest)


def _retry_single_chapter_payload(build_contract, build_ledger, validate_contract,
                                  validate_ledger, validate_cross_references,
                                  label, attempts):
    """Keep correction feedback with the artifact that caused it.

    A batch-size-one chapter uses two independent model prompts. Sending a
    contract-schema error to the ledger prompt (or vice versa) makes the
    otherwise valid prompt produce the wrong JSON shape.
    """
    corrections = {"chapter_contracts": "", "scene_ledger": ""}
    for attempt in range(1, attempts + 1):
        try:
            contracts_only = build_contract(corrections["chapter_contracts"])
        except ControlTransportError:
            raise
        except ControlContractError as exc:
            target, reason = "chapter_contracts", str(exc)
        else:
            try:
                ledger_only = build_ledger(corrections["scene_ledger"])
            except ControlTransportError:
                raise
            except ControlContractError as exc:
                target, reason = "scene_ledger", str(exc)
            else:
                try:
                    candidate = {**contracts_only, **ledger_only}
                    validate_contract(candidate)
                except ControlContractError as exc:
                    target, reason = "chapter_contracts", str(exc)
                else:
                    try:
                        validate_ledger(candidate)
                    except ControlContractError as exc:
                        target, reason = "scene_ledger", str(exc)
                    else:
                        try:
                            validate_cross_references(candidate)
                        except ControlContractError as exc:
                            # Cross-artifact failures need both prompts to see one correction.
                            target, reason = "both", str(exc)
                        else:
                            return candidate
        if attempt >= attempts:
            raise ControlContractError(reason)
        print(
            f"[control] {label} attempt {attempt}/{attempts} rejected ({target}): {reason}",
            flush=True,
        )
        correction = (
            "\n\nPrevious attempt rejected by the required schema: "
            f"{reason}\nReturn the same requested JSON shape, correcting this error only."
        )
        if "fact_slots" in reason and "non-empty string" in reason:
            correction += (
                " Remove the entire incomplete fact_slots object; do not use an "
                "empty placeholder. Use fact_slots: [] when no literal value is available."
            )
        if target == "both":
            corrections = {"chapter_contracts": correction, "scene_ledger": correction}
        else:
            corrections[target] = correction
        time.sleep(5)
    raise AssertionError("unreachable")


def compile_bundle_batched(direction, outline, world_bible, story_id, chapter_count, model_call,
                           batch_size=2, cache_dir=None, attempts=2):
    """Compile a complete bundle in small model calls, then validate it as one unit."""
    batch_size = max(1, min(int(batch_size), 4))
    attempts = max(1, min(int(attempts), 3))
    digest = source_hash(direction, outline, world_bible)
    global_validator = lambda value: _validate_global_checkpoint(value, story_id, digest)
    global_payload = _cache_payload(
        cache_dir, digest, "global",
        ("opening_contract", "character_voice_cards", "prose_budget"),
        "global compiler", global_validator,
    )
    if global_payload is None:
        print("[control] compiling global constraints", flush=True)
        def compile_global(correction):
            payload = _model_payload_once(
                model_call, _global_compile_prompt(direction, world_bible, correction), 1200,
                ("opening_contract", "character_voice_cards", "prose_budget"), "global compiler",
            )
            global_validator(payload)
            return payload

        global_payload = _retry_complete_payload(
            compile_global, "global constraints", attempts,
        )
        _save_cache_payload(cache_dir, digest, "global", global_payload)
    else:
        print("[control] reusing cached global constraints", flush=True)
    _normalize_global_payload(global_payload)
    sections = _chapter_sections(outline, chapter_count)
    contracts, ledger = {}, {}
    for start in range(1, chapter_count + 1, batch_size):
        numbers = range(start, min(chapter_count, start + batch_size - 1) + 1)
        name = f"chapters_{start:04d}_{max(numbers):04d}"
        accepted_contracts = dict(contracts)
        accepted_ledger = dict(ledger)
        checkpoint_validator = lambda value: _validate_chapter_checkpoint_with_context(
            value, global_payload, accepted_contracts, accepted_ledger, story_id, digest,
        )
        payload = _cache_payload(
            cache_dir, digest, name, ("chapter_contracts", "scene_ledger"),
            f"chapter batch {start}", checkpoint_validator,
        )
        if payload is None:
            print(f"[control] compiling chapters {start}-{max(numbers)}", flush=True)
            selected = {number: sections[number] for number in numbers}
            if batch_size == 1:
                payload = _retry_single_chapter_payload(
                    lambda correction: _model_payload_once(
                        model_call,
                        _single_chapter_artifact_prompt(
                            selected, global_payload["character_voice_cards"]["characters"],
                            "chapter_contracts", correction,
                        ),
                        600, ("chapter_contracts",), f"chapter {start} contract",
                    ),
                    lambda correction: _model_payload_once(
                        model_call,
                        _single_chapter_artifact_prompt(
                            selected, global_payload["character_voice_cards"]["characters"],
                            "scene_ledger", correction,
                        ),
                        800, ("scene_ledger",), f"chapter {start} ledger",
                    ),
                    lambda value: _validate_chapter_contract_checkpoint(value, story_id, digest),
                    lambda value: _validate_scene_ledger_checkpoint(value, story_id, digest),
                    checkpoint_validator,
                    f"chapter {start}-{max(numbers)}", attempts,
                )
            else:
                def compile_chapter_batch(correction):
                    candidate = _model_payload_once(
                        model_call,
                        _chapter_batch_prompt(
                            direction, world_bible, selected,
                            global_payload["character_voice_cards"]["characters"], correction,
                        ),
                        1800, ("chapter_contracts", "scene_ledger"),
                        f"chapter batch {start}",
                    )
                    checkpoint_validator(candidate)
                    return candidate
                payload = _retry_complete_payload(
                    compile_chapter_batch, f"chapter {start}-{max(numbers)}", attempts,
                )
            _save_cache_payload(cache_dir, digest, name, payload)
        else:
            print(f"[control] reusing cached chapters {start}-{max(numbers)}", flush=True)
        contracts.update(payload["chapter_contracts"].get("chapters", {}))
        ledger.update(payload["scene_ledger"].get("chapters", {}))
    expected = {f"{number:04d}" for number in range(1, chapter_count + 1)}
    if set(contracts) != expected or set(ledger) != expected:
        raise ControlContractError("batched compiler did not return exactly the requested chapters")
    return build_bundle({
        "chapter_contracts": {"chapters": contracts},
        "scene_ledger": {"chapters": ledger},
        **global_payload,
    }, story_id, digest)


def _read_text(path, label, strip=False):
    try:
        text = Path(path).read_text(encoding="utf-8")
        # engine.py strips direction-file input but hashes outline/world-bible
        # files exactly as written. Normalizing the latter here produced
        # contracts that the writer could never load when a trailing newline
        # was present.
        return text.strip() if strip else text
    except OSError as exc:
        raise ControlContractError(f"cannot read {label} at {path}: {exc}") from exc


def _new_compiler_engine(outline_model=""):
    """Create one model client for one compiler run, retaining role fallback."""
    # Delayed import keeps all schema/dry-run paths local-only.
    from engine import WritingEngine, load_env
    load_env()
    engine = WritingEngine()
    if outline_model:
        engine.llm.models["outline"] = outline_model
    return engine


def _compile_with_engine(prompt, outline_model="", max_tokens=5000, engine=None):
    engine = engine or _new_compiler_engine(outline_model)
    engine.llm.set_call_context(purpose="control_contract_compilation")
    return engine.llm.chat("outline", [
        {"role": "system", "content": "You are a fiction continuity planner. Return strict JSON only."},
        {"role": "user", "content": prompt},
    ], max_tokens=max_tokens, temperature=0.2, json_mode=True,
       relay_max_attempts=3, relay_request_timeout=90)


def _shared_engine_model_call(outline_model=""):
    """Return a compiler callback that shares fallback state across all chapters."""
    engine = _new_compiler_engine(outline_model)
    return lambda prompt, max_tokens: _compile_with_engine(
        prompt, max_tokens=max_tokens, engine=engine,
    )


def _main():
    parser = argparse.ArgumentParser(description="Compile opt-in controlled-generation contracts")
    parser.add_argument("--story-id", required=True)
    parser.add_argument("--direction-file", required=True)
    parser.add_argument("--outline", required=True)
    parser.add_argument("--world-bible", required=True)
    parser.add_argument("--chapters", type=int, required=True)
    parser.add_argument("--outline-model", default="", help="Only override the control compiler model for this run")
    parser.add_argument("--batch-size", type=int, default=0, help="Compile control chapters in bounded batches (1-4)")
    parser.add_argument("--batch-attempts", type=int, default=2, help="Attempts per bounded control batch (1-3)")
    parser.add_argument("--control-dir", default="")
    parser.add_argument("--dry-run", action="store_true", help="Print source/hash context without a model call")
    parser.add_argument("--bootstrap-control", action="store_true", help="Write an unconfirmed draft for an existing story")
    parser.add_argument("--confirm-draft", action="store_true", help="Validate and promote control/draft artifacts")
    parser.add_argument("--replace-control", action="store_true")
    args = parser.parse_args()
    if args.chapters < 1:
        parser.error("--chapters must be positive")
    if args.bootstrap_control and args.confirm_draft:
        parser.error("--bootstrap-control and --confirm-draft cannot be combined")

    story_dir = ROOT / "shorts" / args.story_id
    control_dir = Path(args.control_dir) if args.control_dir else story_dir / "control"
    direction = _read_text(args.direction_file, "direction", strip=True)
    outline = _read_text(args.outline, "outline")
    world_bible = _read_text(args.world_bible, "world bible")
    digest = source_hash(direction, outline, world_bible)

    if args.confirm_draft:
        draft_dir = control_dir / "draft"
        loaded = load_control_bundle(draft_dir, args.story_id, digest, allow_missing=False)
        draft_bundle = loaded["artifacts"]
        for document in draft_bundle.values():
            if document.get("review_status") != "needs_human_confirmation":
                raise ControlContractError("only bootstrap drafts can be confirmed")
            document["review_status"] = "confirmed"
        write_bundle(draft_bundle, control_dir, replace=args.replace_control)
        print(json.dumps({"status": "confirmed", "control_dir": str(control_dir)}, ensure_ascii=False))
        return

    if args.dry_run:
        print(json.dumps({"status": "dry_run", "story_id": args.story_id, "source_sha256": digest}, ensure_ascii=False))
        return

    model_call = _shared_engine_model_call(args.outline_model)
    if args.batch_size:
        bundle = compile_bundle_batched(
            direction, outline, world_bible, args.story_id, args.chapters,
            model_call,
            args.batch_size, cache_dir=control_dir / ".compile_cache", attempts=args.batch_attempts,
        )
    else:
        bundle = compile_bundle(
            direction, outline, world_bible, args.story_id, args.chapters,
            lambda prompt: model_call(prompt, 5000),
        )
    target = control_dir / "draft" if args.bootstrap_control else control_dir
    if args.bootstrap_control:
        for document in bundle.values():
            document["review_status"] = "needs_human_confirmation"
    write_bundle(bundle, target, replace=args.replace_control)
    print(json.dumps({
        "status": "draft_written" if args.bootstrap_control else "compiled",
        "control_dir": str(target), "source_sha256": digest,
    }, ensure_ascii=False))


def main():
    try:
        return _main()
    except ControlContractError as exc:
        print(f"[control] FAILED: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
