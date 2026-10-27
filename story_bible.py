# -*- coding: utf-8 -*-
"""Validated, append-only canon for chapter-local fiction planning.

The library is intentionally literal.  It does not infer story meaning: a
chapter plan may only reference existing ids, and a new value is refused when
it conflicts with an already committed (subject, field) value.
"""
import copy
import hashlib
import json
import re
from pathlib import Path


SCHEMA_VERSION = "story_bible_v1"
PLAN_SCHEMA_VERSION = "story_bible_chapter_plan_v1"
_ID = re.compile(r"^[a-z][a-z0-9-]{1,63}$")
_TOKEN = re.compile(r"\{\{(entity|fact):([a-z][a-z0-9-]{1,63})\}\}")
# A chapter source block may use either a standalone heading or the compact
# ``Chapter 1: beat ...`` form emitted by the segmented outline writer. The
# line-start anchor keeps inline ``Serial arc: Chapter Two`` references out.
_CHAPTER_HEADING = re.compile(
    r"(?im)^chapter\s+(\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)(?=\s*(?::|\r?$))"
)
_SERIAL_ARC_HEADING = re.compile(r"(?im)^serial\s+arc\s*:")
_CHAPTER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}
_TRANSIENT_FACT_FIELD_TOKENS = frozenset({
    "beat", "choice", "conflict", "emotional", "emotion", "hook", "pressure",
    "reaction", "result", "scene", "status", "turn",
})
_RUNTIME_STATE_FACT_FIELD_TOKENS = frozenset({
    "role", "rank", "office", "command", "status", "title",
})


class StoryBibleError(ValueError):
    """Raised when a canon record or chapter plan cannot safely be used."""


def sha256_text(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def planning_source(direction, outline, world_bible):
    return "\n\n".join((direction or "", outline or "", world_bible or ""))


def planning_hash(direction, outline, world_bible):
    return sha256_text(planning_source(direction, outline, world_bible))


def chapter_outline_section(outline, chapter_no):
    """Return one macro chapter section without sending the whole outline onward."""
    headings = []
    for match in _CHAPTER_HEADING.finditer(outline or ""):
        label = match.group(1).casefold()
        number = int(label) if label.isdigit() else _CHAPTER_WORDS[label]
        headings.append((number, match.start()))
    headings.sort()
    for index, (number, start) in enumerate(headings):
        if number == chapter_no:
            end = headings[index + 1][1] if index + 1 < len(headings) else len(outline)
            return outline[start:end].strip()
    raise StoryBibleError(f"outline lacks a recognizable Chapter {chapter_no} section")


def story_bible_foundation(outline):
    """Return only the pre-chapter setup used to seed immutable canon.

    Later chapter details must enter through their own approved chapter plan;
    otherwise the first chapter receives future plot facts and the library is
    no longer append-only in practice.
    """
    matches = [match.start() for match in (_CHAPTER_HEADING.search(outline or ""), _SERIAL_ARC_HEADING.search(outline or "")) if match]
    cutoff = min(matches) if matches else len(outline or "")
    foundation = (outline or "")[:cutoff]
    foundation = foundation.strip()
    if not foundation:
        raise StoryBibleError("outline needs fixed setup before Chapter One for story-bible mode")
    return foundation


def _string(value, label):
    if not isinstance(value, str) or not value.strip():
        raise StoryBibleError(f"{label} must be a non-empty string")
    return value.strip()


def _identifier(value, label):
    value = _string(value, label)
    if not _ID.fullmatch(value):
        raise StoryBibleError(f"{label} must use lowercase hyphenated id")
    return value


def _list(value, label):
    if not isinstance(value, list):
        raise StoryBibleError(f"{label} must be a list")
    return value


def _anchored(value, quote, source, label):
    quote = _string(quote, f"{label}.source_quote")
    if quote not in source:
        raise StoryBibleError(f"{label}.source_quote is not an exact planning-input quote")
    if value.casefold() not in quote.casefold():
        raise StoryBibleError(f"{label} value must appear in its source_quote")
    return quote


def _entity_map(document):
    return {item["id"]: item for item in document["entities"]}


def _fact_map(document):
    return {item["id"]: item for item in document["facts"]}


def _is_transient_chapter_fact_field(field):
    """Reject chapter-beat labels that cannot serve as lasting canon."""
    return bool(set((field or "").split("-")) & _TRANSIENT_FACT_FIELD_TOKENS)


def is_runtime_state_fact(item):
    """Whether a fact describes a state that later chapters may change.

    The bible remains authoritative for stable setup.  Office, command, rank,
    and similar values are carried by the validated chapter-state chain so a
    later plot beat can formally change them without contradicting foundation.
    """
    field = item.get("field", "") if isinstance(item, dict) else ""
    return bool(set(field.split("-")) & _RUNTIME_STATE_FACT_FIELD_TOKENS)


def _canon_values(document):
    return {(item["subject_id"], item["field"]): item for item in document["facts"]}


def _has_literal(text, value):
    """Match a value as a standalone literal, without interpreting prose."""
    return re.search(r"(?<!\w)" + re.escape(value) + r"(?!\w)", text, re.I) is not None


def validate_story_bible(document, story_id=None, source_sha256=None, source_text=None):
    if not isinstance(document, dict):
        raise StoryBibleError("story bible must be an object")
    if document.get("schema_version") != SCHEMA_VERSION:
        raise StoryBibleError(f"unsupported story bible schema {document.get('schema_version')!r}")
    if story_id is not None and document.get("story_id") != story_id:
        raise StoryBibleError("story bible story_id mismatch")
    _string(document.get("story_id"), "story_id")
    digest = _string(document.get("source_sha256"), "source_sha256")
    if source_sha256 is not None and digest != source_sha256:
        raise StoryBibleError("story bible source_sha256 mismatch; rebuild from current planning inputs")
    entities = _list(document.get("entities"), "entities")
    facts = _list(document.get("facts"), "facts")
    if not entities:
        raise StoryBibleError("story bible must contain at least one entity")
    entity_ids, names = set(), set()
    for index, item in enumerate(entities):
        label = f"entities[{index}]"
        if not isinstance(item, dict):
            raise StoryBibleError(f"{label} must be an object")
        entity_id = _identifier(item.get("id"), f"{label}.id")
        name = _string(item.get("name"), f"{label}.name")
        if entity_id in entity_ids or name.casefold() in names:
            raise StoryBibleError(f"duplicate story-bible entity {entity_id!r} or {name!r}")
        entity_ids.add(entity_id)
        names.add(name.casefold())
        aliases = _list(item.get("aliases", []), f"{label}.aliases")
        if any(not isinstance(alias, str) or not alias.strip() for alias in aliases):
            raise StoryBibleError(f"{label}.aliases must contain non-empty strings")
        if source_text is not None:
            _anchored(name, item.get("source_quote"), source_text, label)
    fact_ids, canonical_keys = set(), set()
    for index, item in enumerate(facts):
        label = f"facts[{index}]"
        if not isinstance(item, dict):
            raise StoryBibleError(f"{label} must be an object")
        fact_id = _identifier(item.get("id"), f"{label}.id")
        subject_id = _identifier(item.get("subject_id"), f"{label}.subject_id")
        field = _identifier(item.get("field"), f"{label}.field")
        value = _string(item.get("value"), f"{label}.value")
        if subject_id not in entity_ids:
            raise StoryBibleError(f"{label}.subject_id is not a known entity")
        key = (subject_id, field)
        if fact_id in fact_ids or key in canonical_keys:
            raise StoryBibleError(f"duplicate canonical fact {fact_id!r} or {subject_id}.{field}")
        fact_ids.add(fact_id)
        canonical_keys.add(key)
        if source_text is not None:
            _anchored(value, item.get("source_quote"), source_text, label)
    plans = document.get("chapter_plans", {})
    if not isinstance(plans, dict):
        raise StoryBibleError("chapter_plans must be an object")
    return document


def make_story_bible(story_id, direction, outline, world_bible, payload, canon_source=None):
    if not isinstance(payload, dict):
        raise StoryBibleError("story bible compiler output must be an object")
    document = {
        "schema_version": SCHEMA_VERSION,
        "story_id": story_id,
        "source_sha256": planning_hash(direction, outline, world_bible),
        "entities": payload.get("entities"),
        "facts": payload.get("facts"),
        "chapter_plans": {},
    }
    source = canon_source if canon_source is not None else planning_source(direction, outline, world_bible)
    return validate_story_bible(document, story_id, document["source_sha256"], source)


def load_story_bible(path, story_id=None, source_sha256=None):
    target = Path(path)
    try:
        document = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StoryBibleError(f"cannot read story bible: {exc}") from exc
    return validate_story_bible(document, story_id, source_sha256)


def save_story_bible(path, document):
    document = validate_story_bible(document)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)


def canon_context(document):
    """Return compact immutable entries for the chapter planner and writer."""
    validate_story_bible(document)
    entities = [{"id": item["id"], "name": item["name"]} for item in document["entities"]]
    facts = [
        {"id": item["id"], "subject_id": item["subject_id"], "field": item["field"], "value": item["value"]}
        for item in document["facts"]
    ]
    return {"entities": entities, "facts": facts}


def normalize_plan_canon_references(plan, document, chapter_source=None):
    """Replace literal canon mentions in a model plan with verified references.

    This is mechanical normalization, not story interpretation: every exact
    registered name or value is replaced with its corresponding token before
    the plan is validated.  It prevents a planner's formatting lapse from
    defeating a valid chapter while preserving the fail-closed validator for
    unknown or contradictory values.
    """
    if not isinstance(plan, dict):
        return plan
    normalized = copy.deepcopy(plan)
    entities = _entity_map(document)
    facts = _fact_map(document)
    stable_facts = {fact_id: item for fact_id, item in facts.items() if not is_runtime_state_fact(item)}
    replacements = []
    for entity_id, item in entities.items():
        replacements.append((item["name"], f"{{{{entity:{entity_id}}}}}", "entity", entity_id))
    for fact_id, item in stable_facts.items():
        replacements.append((item["value"], f"{{{{fact:{fact_id}}}}}", "fact", fact_id))
    # Longest first prevents a short literal from consuming part of a longer one.
    replacements.sort(key=lambda item: len(item[0]), reverse=True)
    token_pattern = re.compile(r"\{\{(?:entity|fact):[^{}]+\}\}")

    def replace_literals_outside_tokens(text, literal, token):
        """Keep existing references opaque while normalizing plain prose."""
        parts = token_pattern.split(text)
        tokens = token_pattern.findall(text)
        pattern = re.compile(r"(?<!\w)" + re.escape(literal) + r"(?!\w)", re.I)
        rewritten = []
        for index, part in enumerate(parts):
            rewritten.append(pattern.sub(token, part))
            if index < len(tokens):
                rewritten.append(tokens[index])
        return "".join(rewritten)

    for beat in normalized.get("beats", []):
        if not isinstance(beat, dict):
            continue
        participant_ids = list(beat.get("participant_ids") or [])
        fact_ids = list(beat.get("fact_ids") or [])
        for field in ("conflict", "choice", "result"):
            text = beat.get(field)
            if not isinstance(text, str):
                continue
            for literal, token, kind, reference in replacements:
                plain_text = token_pattern.sub("", text)
                if _has_literal(plain_text, literal):
                    text = replace_literals_outside_tokens(text, literal, token)
                    if kind == "entity" and reference not in participant_ids:
                        participant_ids.append(reference)
                    if kind == "fact" and reference not in fact_ids:
                        fact_ids.append(reference)
            beat[field] = text
        beat["participant_ids"] = participant_ids
        beat["fact_ids"] = fact_ids
    known_entities = list(normalized.get("known_entity_ids") or [])
    known_facts = list(normalized.get("known_fact_ids") or [])
    for beat in normalized.get("beats", []):
        if isinstance(beat, dict):
            for entity_id in beat.get("participant_ids") or []:
                if entity_id in entities and entity_id not in known_entities:
                    known_entities.append(entity_id)
            for fact_id in beat.get("fact_ids") or []:
                if fact_id in facts and fact_id not in known_facts:
                    known_facts.append(fact_id)
    # The macro outline itself is also a literal use of canon.  Declare those
    # ids even when the model forgot the bookkeeping list; validation below
    # still rejects an unknown or conflicting value.
    if chapter_source is not None:
        for entity_id, item in entities.items():
            if _has_literal(chapter_source, item["name"]) and entity_id not in known_entities:
                known_entities.append(entity_id)
        for fact_id, item in stable_facts.items():
            if _has_literal(chapter_source, item["value"]) and fact_id not in known_facts:
                known_facts.append(fact_id)
    normalized["known_entity_ids"] = known_entities
    normalized["known_fact_ids"] = known_facts
    return normalized


def normalize_new_entry_source_quotes(plan, chapter_source):
    """Repair only an omitted local evidence quote for a literal new value.

    A new item remains invalid unless its own name or value occurs verbatim in
    this chapter's macro outline.  This replaces a bad quote with the exact
    containing outline line; it never invents support or relaxes anchoring.
    """
    if not isinstance(plan, dict):
        return plan
    normalized = copy.deepcopy(plan)
    lines = [line.strip() for line in (chapter_source or "").splitlines() if line.strip()]

    def quote_for(value):
        if not isinstance(value, str) or not value.strip():
            return None
        return next((line for line in lines if _has_literal(line, value)), None)

    for item in normalized.get("new_entities", []):
        if isinstance(item, dict):
            quote = quote_for(item.get("name"))
            if quote is not None:
                item["source_quote"] = quote
    for item in normalized.get("new_facts", []):
        if isinstance(item, dict):
            quote = quote_for(item.get("value"))
            if quote is not None:
                item["source_quote"] = quote
    return normalized


def validate_chapter_plan(plan, document, chapter_no, chapter_source):
    """Validate a chapter plan without attempting semantic interpretation.

    Existing canon may appear only through ids.  New entries are allowed only
    when their source quote appears in this chapter's macro outline and their
    (subject, field) key is not already assigned a different value.
    """
    validate_story_bible(document)
    if not isinstance(plan, dict):
        raise StoryBibleError("chapter plan must be an object")
    if plan.get("schema_version") != PLAN_SCHEMA_VERSION:
        raise StoryBibleError("unsupported chapter plan schema")
    if plan.get("chapter") != chapter_no:
        raise StoryBibleError("chapter plan chapter does not match requested chapter")
    if plan.get("story_id") != document["story_id"]:
        raise StoryBibleError("chapter plan story_id mismatch")
    if plan.get("source_sha256") != document["source_sha256"]:
        raise StoryBibleError("chapter plan was built from another story bible")
    entities = _entity_map(document)
    facts = _fact_map(document)
    stable_facts = {fact_id: item for fact_id, item in facts.items() if not is_runtime_state_fact(item)}
    known_entity_ids = _list(plan.get("known_entity_ids"), "known_entity_ids")
    known_fact_ids = _list(plan.get("known_fact_ids"), "known_fact_ids")
    if len(set(known_entity_ids)) != len(known_entity_ids) or any(item not in entities for item in known_entity_ids):
        raise StoryBibleError("chapter plan references an unknown or duplicate entity id")
    if len(set(known_fact_ids)) != len(known_fact_ids) or any(item not in facts for item in known_fact_ids):
        raise StoryBibleError("chapter plan references an unknown or duplicate fact id")
    # A planner cannot evade canon by simply omitting a fact already stated in
    # this chapter's macro outline.  Exact-literal matching is deliberate:
    # this layer validates declared values, never guesses semantic equivalents.
    required_entities = {
        entity_id for entity_id, item in entities.items()
        if _has_literal(chapter_source, item["name"])
    }
    required_facts = {
        fact_id for fact_id, item in stable_facts.items()
        if _has_literal(chapter_source, item["value"])
    }
    missing_entities = sorted(required_entities - set(known_entity_ids))
    missing_facts = sorted(required_facts - set(known_fact_ids))
    if missing_entities:
        raise StoryBibleError("chapter plan omitted canonical entity ids: " + ", ".join(missing_entities))
    if missing_facts:
        raise StoryBibleError("chapter plan omitted canonical fact ids: " + ", ".join(missing_facts))
    new_entities = _list(plan.get("new_entities", []), "new_entities")
    new_facts = _list(plan.get("new_facts", []), "new_facts")
    working_entities = dict(entities)
    known_names = {item["name"].casefold() for item in entities.values()}
    for index, item in enumerate(new_entities):
        label = f"new_entities[{index}]"
        if not isinstance(item, dict):
            raise StoryBibleError(f"{label} must be an object")
        entity_id = _identifier(item.get("id"), f"{label}.id")
        name = _string(item.get("name"), f"{label}.name")
        _string(item.get("role"), f"{label}.role")
        if entity_id in working_entities or name.casefold() in known_names:
            raise StoryBibleError(f"{label} duplicates an existing entity")
        _anchored(name, item.get("source_quote"), chapter_source, label)
        working_entities[entity_id] = item
        known_names.add(name.casefold())
    working_facts = _canon_values(document)
    fact_ids = set(facts)
    for index, item in enumerate(new_facts):
        label = f"new_facts[{index}]"
        if not isinstance(item, dict):
            raise StoryBibleError(f"{label} must be an object")
        fact_id = _identifier(item.get("id"), f"{label}.id")
        subject_id = _identifier(item.get("subject_id"), f"{label}.subject_id")
        field = _identifier(item.get("field"), f"{label}.field")
        value = _string(item.get("value"), f"{label}.value")
        if fact_id in fact_ids or subject_id not in working_entities:
            raise StoryBibleError(f"{label} has duplicate id or unknown subject")
        if _is_transient_chapter_fact_field(field):
            raise StoryBibleError(
                f"{label}.field describes a chapter beat, not a lasting canon fact: {field!r}"
            )
        _anchored(value, item.get("source_quote"), chapter_source, label)
        key = (subject_id, field)
        existing = working_facts.get(key)
        if existing is not None:
            if existing["value"] != value:
                raise StoryBibleError(
                    f"{label} conflicts with canon {subject_id}.{field}: {existing['value']!r} != {value!r}"
                )
            raise StoryBibleError(f"{label} duplicates existing canon {subject_id}.{field}")
        working_facts[key] = item
        fact_ids.add(fact_id)
    beats = _list(plan.get("beats"), "beats")
    if not beats:
        raise StoryBibleError("chapter plan needs at least one beat")
    allowed_entities = set(known_entity_ids) | {item["id"] for item in new_entities}
    allowed_facts = set(known_fact_ids) | {item["id"] for item in new_facts}
    for index, beat in enumerate(beats):
        label = f"beats[{index}]"
        if not isinstance(beat, dict):
            raise StoryBibleError(f"{label} must be an object")
        for field in ("conflict", "choice", "result"):
            text = _string(beat.get(field), f"{label}.{field}")
            for kind, reference in _TOKEN.findall(text):
                allowed = allowed_entities if kind == "entity" else allowed_facts
                if reference not in allowed:
                    raise StoryBibleError(f"{label}.{field} references unavailable {kind} {reference!r}")
            plain = _TOKEN.sub("", text)
            for entity in working_entities.values():
                if _has_literal(plain, entity["name"]):
                    raise StoryBibleError(f"{label}.{field} must reference entity {entity['id']!r} by token")
            for fact in working_facts.values():
                if is_runtime_state_fact(fact):
                    continue
                if _has_literal(plain, fact["value"]):
                    raise StoryBibleError(f"{label}.{field} must reference fact {fact['id']!r} by token")
        participants = _list(beat.get("participant_ids"), f"{label}.participant_ids")
        if not participants or any(item not in allowed_entities for item in participants):
            raise StoryBibleError(f"{label}.participant_ids must use declared entity ids")
        referenced = _list(beat.get("fact_ids", []), f"{label}.fact_ids")
        if any(item not in allowed_facts for item in referenced):
            raise StoryBibleError(f"{label}.fact_ids must use declared fact ids")
    return plan


def commit_chapter_plan(document, plan, chapter_source):
    """Append validated new entries and the plan; never rewrite old canon."""
    plan = validate_chapter_plan(plan, document, int(plan.get("chapter", 0)), chapter_source)
    updated = copy.deepcopy(document)
    chapter_key = f"{plan['chapter']:04d}"
    if chapter_key in updated["chapter_plans"]:
        existing = updated["chapter_plans"][chapter_key]
        if existing != plan:
            raise StoryBibleError(f"chapter {chapter_key} already has a different committed plan")
        return updated
    updated["entities"].extend(copy.deepcopy(plan.get("new_entities", [])))
    updated["facts"].extend(copy.deepcopy(plan.get("new_facts", [])))
    updated["chapter_plans"][chapter_key] = copy.deepcopy(plan)
    return validate_story_bible(updated)


def rollback_chapter_plan(document, chapter_no):
    """Discard a failed chapter's unmaterialized plan and its new canon."""
    updated = copy.deepcopy(document)
    chapter_key = f"{int(chapter_no):04d}"
    plan = updated.get("chapter_plans", {}).get(chapter_key)
    if not isinstance(plan, dict):
        raise StoryBibleError(f"no committed chapter plan for {chapter_key}")
    new_entity_ids = {item["id"] for item in plan.get("new_entities", [])}
    new_fact_ids = {item["id"] for item in plan.get("new_facts", [])}
    for key, later in updated["chapter_plans"].items():
        if key <= chapter_key or not isinstance(later, dict):
            continue
        entities = set(later.get("known_entity_ids", [])) | {
            item for beat in later.get("beats", []) for item in beat.get("participant_ids", [])
        }
        facts = set(later.get("known_fact_ids", [])) | {
            item for beat in later.get("beats", []) for item in beat.get("fact_ids", [])
        }
        if new_entity_ids & entities or new_fact_ids & facts:
            raise StoryBibleError(f"cannot roll back {chapter_key}; a later plan references its canon")
    updated["entities"] = [item for item in updated["entities"] if item["id"] not in new_entity_ids]
    updated["facts"] = [item for item in updated["facts"] if item["id"] not in new_fact_ids]
    del updated["chapter_plans"][chapter_key]
    return validate_story_bible(updated)


def render_chapter_packet(document, chapter_no):
    """Render a plan with canonical values after validation has locked them."""
    validate_story_bible(document)
    plan = document["chapter_plans"].get(f"{chapter_no:04d}")
    if not plan:
        raise StoryBibleError(f"no committed chapter plan for {chapter_no:04d}")
    entities = _entity_map(document)
    facts = _fact_map(document)
    token_values = {
        **{("entity", item_id): item["name"] for item_id, item in entities.items()},
        **{("fact", item_id): item["value"] for item_id, item in facts.items()},
    }

    def render(text):
        return _TOKEN.sub(lambda match: token_values[(match.group(1), match.group(2))], text)

    lines = [f"Chapter {chapter_no} approved plan.", "Use only these existing canon entries exactly:"]
    entity_ids = list(plan["known_entity_ids"])
    fact_ids = list(plan["known_fact_ids"])
    for beat in plan["beats"]:
        entity_ids.extend(item for item in beat["participant_ids"] if item not in entity_ids)
        fact_ids.extend(item for item in beat["fact_ids"] if item not in fact_ids)
    for entity_id in entity_ids:
        lines.append(f"- entity {entity_id}: {entities[entity_id]['name']}")
    for fact_id in fact_ids:
        fact = facts[fact_id]
        lines.append(f"- fact {fact_id}: {fact['subject_id']}.{fact['field']} = {fact['value']}")
    lines.append("Required beats:")
    for index, beat in enumerate(plan["beats"], 1):
        participants = ", ".join(entities[item]["name"] for item in beat["participant_ids"])
        lines.append(f"{index}. Participants: {participants}. Conflict: {render(beat['conflict'])} Choice: {render(beat['choice'])} Result: {render(beat['result'])}")
    return "\n".join(lines)
