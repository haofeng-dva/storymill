# -*- coding: utf-8 -*-
"""Compile opt-in controlled-generation artifacts from planning inputs."""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from control_contracts import (
    CONTROL_FILENAMES,
    ControlContractError,
    SCHEMA_VERSION,
    VALIDATORS,
    load_control_bundle,
    source_hash,
    validate_cross_artifact_references,
)
from control_context import build_control_context


ROOT = Path(__file__).resolve().parent


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
  \"facts\": [{{\"id\": \"unique-short-id\"}}]
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
For every chapter, its final writer control context must fit in 1700 UTF-8 bytes.
Do not include prose paragraphs, explanations, or duplicate facts.

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
                build_control_context(bundle, chapter_no, max_bytes=1700)
            else:
                prior = contracts[f"{chapter_no - 1:04d}"]
                build_control_context(
                    bundle, chapter_no, _max_execution_view(prior), max_bytes=2048
                )
        except ControlContractError as exc:
            raise ControlContractError(
                f"compiler output exceeds current-chapter context budget for {key}: {exc}"
            ) from exc
    return bundle


def _max_execution_view(contract):
    """Bounded worst-case state view used only to reserve runtime context space."""
    return {
        "assertion_statuses": [
            {"assertion_id": item["id"], "status": "met"}
            for item in contract["end_assertions"]
        ],
        "non_regression_statuses": [
            {"assertion_id": item["id"], "status": "preserved"}
            for item in contract["must_not_regress"]
        ],
        "new_information": ["x" * 180],
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
    return build_bundle(payload, story_id, source_hash(direction, outline, world_bible))


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


def _compile_with_engine(prompt):
    # Delayed import keeps all schema/dry-run paths local-only.
    from engine import WritingEngine, load_env
    load_env()
    engine = WritingEngine()
    engine.llm.set_call_context(purpose="control_contract_compilation")
    return engine.llm.chat("outline", [
        {"role": "system", "content": "You are a fiction continuity planner. Return strict JSON only."},
        {"role": "user", "content": prompt},
    ], max_tokens=5000, temperature=0.2, json_mode=True)


def main():
    parser = argparse.ArgumentParser(description="Compile opt-in controlled-generation contracts")
    parser.add_argument("--story-id", required=True)
    parser.add_argument("--direction-file", required=True)
    parser.add_argument("--outline", required=True)
    parser.add_argument("--world-bible", required=True)
    parser.add_argument("--chapters", type=int, required=True)
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

    bundle = compile_bundle(direction, outline, world_bible, args.story_id, args.chapters, _compile_with_engine)
    target = control_dir / "draft" if args.bootstrap_control else control_dir
    if args.bootstrap_control:
        for document in bundle.values():
            document["review_status"] = "needs_human_confirmation"
    write_bundle(bundle, target, replace=args.replace_control)
    print(json.dumps({
        "status": "draft_written" if args.bootstrap_control else "compiled",
        "control_dir": str(target), "source_sha256": digest,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
