# -*- coding: utf-8 -*-
"""Cheap local checks that run after generation without another model call."""
import re


STRICT_PATTERNS = {
    "pupil-swallowing anatomy": r"\bpupils?\s+(?:had\s+)?(?:swallowed|swallowing)\b.{0,30}\b(?:iris|irises)\b",
    "wolf-beneath-skin shorthand": r"\bwolf\s+(?:pressed|moved|stirred|rose)\b.{0,35}\bbeneath\s+(?:his|her)\s+skin\b",
    "room-or-forest narrowing shorthand": r"\b(?:room|forest|silence)\s+(?:seemed\s+to\s+)?narrow(?:ed|ing)?\b",
    "environment-closing-in shorthand": r"\b(?:room|forest|silence|night|darkness)\s+(?:seemed\s+to\s+)?close(?:d)?\s+in\b",
    "abstract reaction landed": r"\b(?:answer|recognition|fear|silence)\s+(?:landed|moved|settled)\b",
    "breath-caught shorthand": r"\b(?:his|her|mara'?s|rafe'?s)\s+breath\s+caught\b",
    "trailer fragment triad": r"\bNot\s+[^.!?]{1,45}[.!?]\s*Not\s+[^.!?]{1,45}[.!?]\s*[^.!?]{1,70}[.!?]",
    "unsupported sequence-encodes-protein claim": r"\b(?:sequence|pattern)\s+(?:could\s+)?encode\s+(?:proteins?|binding\s+sites?)\b",
}

# These are deliberately finite, evidence-based defects found in production
# review.  Do not add broad taste rules here: a local checker must be able to
# state exactly what it found and why it is unsafe.
TARGETED_RULES = (
    ("missing-alpha-article", "mechanical", "important", r"\bwhat\s+alpha\s+tells\s+you\b", "Use 'the alpha' unless a dialect rule explicitly permits the omission."),
    ("sterile-light-device", "language_clarity", "important", r"\bsterile\s+light\b", "'Sterile light' is not a named medical device; name the actual light."),
    ("readings-overlaid", "language_clarity", "important", r"\breadings\s+overlaid\b", "Measurements may align or overlap; 'readings overlaid' is non-idiomatic."),
    ("enough-passed-idiom", "language_clarity", "important", r"\benough\s+passed\s+a\s+long\s+time\s+ago\b", "This construction is non-idiomatic; recast the sentence."),
    ("unsupported-protein-encoding", "science_medical", "blocking", STRICT_PATTERNS["unsupported sequence-encodes-protein claim"], "A visible ward pattern cannot be said to encode proteins without an established code."),
    ("ambiguous-speaker-clicked-off", "language_clarity", "important", r"\bThe\s+speaker\s+clicked\s+off\b", "Name the audio device or describe it falling silent."),
    ("missing-cold-article", "mechanical", "important", r"\bdespite\s+cold\s+beyond\b", "Use 'despite the cold beyond ...'."),
    ("wolf-form-glove-action", "continuity", "blocking", r"\bpatrol\s+wolves?\s+stripped\s+off\s+a\s+thick\s+glove\b", "A shifted patrol wolf cannot remove a glove; establish a human actor or a shift first."),
    ("gold-pupil-anatomy", "language_clarity", "important", r"\bpupils?\s+had\s+narrowed\s+until\s+(?:his|her)\s+eyes\s+looked\s+almost\s+wholly\s+gold\b", "Pupils remain black; describe the irises as gold instead."),
    ("joined-dialogue-and-narration", "text_integrity", "important", r"(?<=[a-z.!?])[”\"](?=[A-Z])", "Dialogue closes directly into a new sentence; restore the missing space or paragraph break."),
    ("missing-space-after-terminal-punctuation", "text_integrity", "blocking", r"(?<=[a-z”\"'])[.!?](?=[A-Z“\"])", "Terminal punctuation runs directly into a new sentence; restore the missing space or paragraph break."),
    ("future-spoiler-narration", "narrative_spoiler", "important", r"\bdid not yet know that\b.{0,180}\bwould\b", "Future-tense narration reveals a later outcome before the chapter earns it."),
)

def count_words(text):
    return len(re.findall(r"[A-Za-z']+", text))

def style_lint(text, max_as_if=2):
    warnings = []
    as_if = len(re.findall(r"\bas if\b", text, re.I))
    if as_if > max_as_if:
        warnings.append(f"as if x{as_if} (limit {max_as_if})")
    # Strict, repeatable anti-template rules. Production can treat these as gates.
    for label, pattern in STRICT_PATTERNS.items():
        count = len(re.findall(pattern, text or "", re.I))
        if count:
            warnings.append(f"strict rule: {label} x{count}")
    return warnings


def strict_prose_violations(text):
    """Return zero-tolerance patterns that require a rewrite, not a warning."""
    violations = [warning for warning in style_lint(text) if warning.startswith("strict rule:")]
    for finding in structured_quality_findings(text):
        if finding.get("severity") == "blocking":
            violations.append(f"{finding['rule_id']}: {finding['message']}")
    return list(dict.fromkeys(violations))


def repair_deterministic_integrity(text):
    """Apply the one text-integrity repair whose correction is unambiguous.

    This deliberately handles only a terminal mark immediately followed by a
    new sentence. Adding that one missing space cannot alter story content,
    unlike a prose rewrite, and it lets the bounded sentence repair handle the
    remaining style-only violations.
    """
    repaired, count = re.subn(
        r"(?<=[a-z\u201d\"'])[.!?](?=[A-Z\u201c\"])",
        lambda match: match.group(0) + " ",
        text or "",
    )
    return repaired, count


def strict_repair_anchors(text):
    """Return unique whole-sentence anchors for strict local violations.

    A phrase match is too small to edit grammatically. A full sentence is still
    bounded enough to prohibit plot or continuity rewrites.
    """
    anchors_by_excerpt = {}
    for rule_id, pattern in STRICT_PATTERNS.items():
        for match in re.finditer(pattern, text or "", re.I):
            if rule_id == "trailer fragment triad":
                # This rule is itself a three-sentence construction. Editing
                # just its first fragment leaves the prohibited cadence intact.
                excerpt = match.group(0).strip()
            else:
                start = max(text.rfind(mark, 0, match.start()) for mark in ".!?") + 1
                end_marks = [text.find(mark, match.end()) for mark in ".!?"]
                end_marks = [offset for offset in end_marks if offset >= 0]
                end = min(end_marks) + 1 if end_marks else len(text)
                excerpt = text[start:end].strip()
            if not excerpt or text.count(excerpt) != 1:
                continue
            anchor = anchors_by_excerpt.setdefault(excerpt, {
                "rule_id": rule_id, "rule_ids": [], "excerpt": excerpt,
            })
            if rule_id not in anchor["rule_ids"]:
                anchor["rule_ids"].append(rule_id)
    return list(anchors_by_excerpt.values())


def _finding(rule_id, category, severity, message, text, match):
    """Return a source-stable issue card for a local rule match."""
    start, end = match.span()
    lines = text.splitlines()
    line_start = text.count("\n", 0, start) + 1
    line_end = text.count("\n", 0, end) + 1
    return {
        "source": "local_lint",
        "rule_id": rule_id,
        "root_cause_key": rule_id,
        "category": category,
        "severity": severity,
        "message": message,
        "line_start": line_start,
        "line_end": line_end,
        "excerpt": text[start:end],
        "context_before": lines[max(0, line_start - 3):line_start - 1],
        "context_after": lines[line_end:min(len(lines), line_end + 2)],
    }


def structured_quality_findings(text):
    """Locate only deterministic quality defects, with exact repair anchors.

    These results are suitable for an automated repair queue. They intentionally
    exclude broad style preferences because an automated repair needs a stable anchor.
    """
    findings = []
    for rule_id, category, severity, pattern, message in TARGETED_RULES:
        for match in re.finditer(pattern, text or "", re.I):
            findings.append(_finding(rule_id, category, severity, message, text, match))
    for rule_id, pattern in STRICT_PATTERNS.items():
        # The protein claim already has the more useful science-specific card.
        if rule_id == "unsupported sequence-encodes-protein claim":
            continue
        for match in re.finditer(pattern, text or "", re.I):
            findings.append(_finding(f"strict-{rule_id}", "style", "important", f"Strict prose rule: {rule_id}.", text, match))
    # Dialogue can legally continue across paragraphs, so balance quote styles
    # across the full chapter rather than requiring every paragraph to close.
    content = text or ""
    def straight_quote_is_open():
        open_quote = False
        for paragraph in re.split(r"\n\s*\n", content):
            positions = [match.start() for match in re.finditer(r'"', paragraph)]
            if open_quote and positions and not paragraph[:positions[0]].strip():
                positions = positions[1:]
            for _ in positions:
                open_quote = not open_quote
        return open_quote

    # Curly dialogue needs the same stateful treatment as straight dialogue.
    # A leading opening mark is a continuation marker only when the preceding
    # paragraph left the dialogue open; otherwise it begins an ordinary new
    # paragraph of speech.
    curly_quote_is_open = False
    for paragraph in re.split(r"\n\s*\n", content):
        marks = re.findall(r"[“”]", paragraph)
        if curly_quote_is_open and paragraph.lstrip().startswith('“') and marks:
            marks = marks[1:]
        for mark in marks:
            if mark == '“':
                curly_quote_is_open = True
            else:
                curly_quote_is_open = False
    if straight_quote_is_open() or curly_quote_is_open:
        match = re.search(r"[\"“”]", content)
        if match:
            findings.append(_finding(
                "unmatched-dialogue-quote", "text_integrity", "blocking",
                "The chapter has an unmatched dialogue quote.", content, match,
            ))
    return findings


def unregistered_entity_findings(text, canonical_names):
    """Flag a new surname attached to an already-canonical character.

    This is intentionally narrow. It does not guess whether arbitrary title
    case words are people; it only catches ``KnownFirst NewSurname`` when the
    exact full name is absent from the existing character cards.
    """
    allowed = {str(name).strip().casefold() for name in canonical_names if isinstance(name, str) and name.strip()}
    first_names = {
        name.split()[0] for name in allowed
        if name.split()[0] not in {"a", "an", "the"}
    }
    findings = []
    for match in re.finditer(r"\b([A-Z][a-z]{1,})\s+([A-Z][a-z]{1,})\b", text or ""):
        candidate = match.group(0)
        if match.group(1).casefold() not in first_names or candidate.casefold() in allowed:
            continue
        findings.append(_finding(
            "unregistered-entity", "continuity", "blocking",
            f"{candidate!r} is not in the canonical character roster.", text, match,
        ))
    return findings

def local_checks(text, target=None, lo=None, hi=None):
    warnings = style_lint(text)
    words = count_words(text)
    if lo is not None and words < lo:
        warnings.append(f"too short: {words}<{lo}")
    if hi is not None and words > hi:
        warnings.append(f"too long: {words}>{hi}")
    return warnings

def remove_exact_repeats(text):
    """Remove accidental adjacent duplicate paragraphs/sentences only.

    This is deliberately conservative: non-adjacent repetition is left for
    editorial review because it may be intentional characterization or motif.
    """
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]
    kept = []
    removed = 0
    for p in paragraphs:
        if kept and p.casefold() == kept[-1].casefold():
            removed += 1
            continue
        kept.append(p)
    out = "\n\n".join(kept)
    # Catch the common duplicated quoted line left by a model paste.
    out = re.sub(r"(?P<line>[^\n]{20,})\n(?P=line)", r"\g<line>", out, flags=re.I)
    return out, removed

def repetition_check(text, max_sentence_repeats=1):
    """Flag repeated non-trivial sentences in a chapter."""
    sentences = [s.strip().casefold() for s in re.split(r"(?<=[.!?])\s+", text or "")]
    counts = {}
    for s in sentences:
        if len(re.findall(r"[a-z']+", s)) >= 8:
            counts[s] = counts.get(s, 0) + 1
    return [f"duplicate sentence x{n}: {s[:100]}" for s, n in counts.items() if n > max_sentence_repeats]


def redundancy_check(text):
    """Flag repeatable AI-style constructions without changing the prose.

    These patterns are deliberately narrow: a single atmospheric beat or
    fragment is valid; the warning appears only when the same construction is
    reused in one chapter.
    """
    normalized = re.sub(r"\s+", " ", text or " ").strip()
    warnings = []

    fragment_echoes = re.findall(
        r"\bnot\s+([a-z']{3,})[.!?]\s*\1\s+(?:was|is|had)\b",
        normalized,
        re.I,
    )
    for word in sorted({word.lower() for word in fragment_echoes}):
        if sum(found.lower() == word for found in fragment_echoes) > 1:
            warnings.append(f"repeated 'Not {word}. {word} was/is...' construction")

    stock_patterns = {
        "thunder rolled": r"\bthunder\s+rolled\b",
        "room-tightening beat": r"\b(?:room|infirmary|silence)\b.{0,40}\b(?:tighten(?:ed|ing)?|narrow(?:ed|ing)?|contract(?:ed|ing)?)\b",
        "wolf-beneath-skin beat": r"\bwolf\s+(?:pressed|moved|stirred).{0,30}\bbeneath\s+(?:his|her)\s+skin\b",
    }
    for label, pattern in stock_patterns.items():
        count = len(re.findall(pattern, normalized, re.I))
        if count > 1:
            warnings.append(f"repeated {label} x{count}")
    return warnings

def continuity_check(previous, current, tail_chars=700, head_chars=700):
    if not previous or not current:
        return ["missing adjacent chapter text"]
    tail = previous[-tail_chars:].lower()
    head = current[:head_chars].lower()
    warnings = []
    if re.search(r"\b(pending|to be resolved|unfinished)\b", tail) and not re.search(
        r"\b(then|after|next|inside|morning|evening|minutes|hours)\b", head
    ):
        warnings.append("possible skipped transition")
    tail_terms = set(re.findall(r"\b[a-z]{5,}\b", tail))
    head_terms = set(re.findall(r"\b[a-z]{5,}\b", head))
    if len(tail_terms & head_terms) < 2:
        warnings.append("low boundary vocabulary overlap")
    return warnings
