# -*- coding: utf-8 -*-
"""Build one deduplicated, coordinate-first repair backlog from all checkers."""
import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

from quality_checks import structured_quality_findings
from repair_pipeline import build_cards
from story_fact_contract import check_story


ROOT = Path(__file__).resolve().parent


REVIEW_ROOT_CAUSES = (
    ("ward_palm_scan", ("palm scan", "scanned my palm", "green light"), "continuity", "blocking"),
    ("contract_authorization", ("bound rafe", "authorized the contract", "contract he had not approved"), "continuity", "blocking"),
    ("rogue_bodies_and_collar", ("corpse reference", "first rogue", "collar"), "continuity", "blocking"),
    ("archive_locations", ("archive location", "records building", "below the old hall"), "continuity", "blocking"),
    ("timers", ("countdown", "station lockdown", "station purge", "timer"), "continuity", "blocking"),
    ("station_identity", ("weather station", "field biology"), "continuity", "blocking"),
    ("mara_samples", ("intake sleeve", "blood sample", "age-six", "age six"), "continuity", "blocking"),
    ("missing-alpha-article", ("what the alpha tells", "missing article"), "mechanical", "important"),
    ("sterile-light-device", ("sterile light",), "language_clarity", "important"),
    ("readings-overlaid", ("readings overlaid",), "language_clarity", "important"),
    ("enough-passed-idiom", ("enough passed",), "language_clarity", "important"),
    ("unsupported-protein-encoding", ("encode proteins",), "science_medical", "blocking"),
    ("ambiguous-speaker-clicked-off", ("speaker clicked off",), "language_clarity", "important"),
    ("missing-cold-article", ("despite the cold", "missing article before"), "mechanical", "important"),
    ("wolf-form-glove-action", ("stripped off a thick glove", "wolf form"), "continuity", "blocking"),
    ("gold-pupil-anatomy", ("pupils", "iris", "anatomy error"), "language_clarity", "important"),
)


def _review_classification(issue):
    lower = issue.lower()
    for root_cause_key, signals, category, severity in REVIEW_ROOT_CAUSES:
        if any(signal in lower for signal in signals):
            return root_cause_key, category, severity
    if any(word in lower for word in ("continuity", "corpse", "location", "timer", "evidence chain")):
        return None, "continuity", "blocking"
    if any(word in lower for word in ("science", "protein", "genetic", "medical")):
        return None, "science_medical", "important"
    if any(word in lower for word in ("grammar", "word choice", "non-idiomatic", "antecedent")):
        return None, "language_clarity", "important"
    if any(word in lower for word in ("generic", "stock", "cliche", "ai-", "repetitive")):
        return None, "style", "polish"
    return None, "needs_triage", "important"


def _review_card(card):
    key, category, severity = _review_classification(card["review_issue"])
    return {
        "source": "reviewer",
        "id": card["id"],
        "root_cause_key": key or card["id"],
        "chapter": card["chapter"],
        "file": card["file"],
        "category": category,
        "severity": severity,
        "message": card["review_issue"],
        "anchors": card.get("anchors", []),
    }


def build_queue(story_id, report):
    story_dir = ROOT / "shorts" / story_id
    review = [_review_card(card) for card in build_cards(report, story_dir)["cards"]]
    local, facts = [], []
    for chapter_path in sorted(story_dir.glob("ch*.md")):
        chapter = int(chapter_path.stem[2:])
        for finding in structured_quality_findings(chapter_path.read_text(encoding="utf-8")):
            finding.update({"chapter": chapter, "file": chapter_path.name, "anchors": [{key: finding[key] for key in ("line_start", "line_end", "excerpt", "context_before", "context_after")} ]})
            local.append(finding)
    facts = check_story(story_dir, story_dir / "story_facts.json")
    all_cards = review + local + facts
    grouped = defaultdict(list)
    for card in all_cards:
        key = (card["chapter"], card["root_cause_key"])
        grouped[key].append(card)
    groups = []
    for (chapter, key), cards in sorted(grouped.items()):
        categories = Counter(card["category"] for card in cards)
        severities = {card["severity"] for card in cards}
        source_types = sorted({card["source"] for card in cards})
        groups.append({
            "id": f"ch{chapter:04d}-{key}",
            "chapter": chapter,
            "root_cause_key": key,
            "category": categories.most_common(1)[0][0],
            "severity": "blocking" if "blocking" in severities else "important" if "important" in severities else "polish",
            "source_types": source_types,
            "cards": cards,
        })
    by_category = Counter(group["category"] for group in groups)
    by_severity = Counter(group["severity"] for group in groups)
    acceptance = {
        "blocking_fact": sum("fact_contract" in group["source_types"] for group in groups),
        "deterministic_local": sum("local_lint" in group["source_types"] for group in groups),
        "semantic_or_continuity_review": sum(
            "reviewer" in group["source_types"] and group["category"] in {"continuity", "science_medical"}
            and "fact_contract" not in group["source_types"] and "local_lint" not in group["source_types"]
            for group in groups
        ),
        "style_backlog": sum(group["category"] == "style" for group in groups),
        "untriaged_reviewer": sum(group["category"] == "needs_triage" for group in groups),
    }
    return {
        "schema": "repair_queue_v1",
        "story_id": story_id,
        "source_report": str(Path(report).resolve()),
        "acceptance": acceptance,
        "counts": {"groups": len(groups), "by_category": dict(by_category), "by_severity": dict(by_severity)},
        "groups": groups,
    }


def main():
    parser = argparse.ArgumentParser(description="Generate a merged, coordinate-first fiction repair backlog")
    parser.add_argument("--story-id", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    report = Path(args.report)
    if not report.is_absolute():
        report = ROOT / report
    queue = build_queue(args.story_id, report)
    out = Path(args.out) if args.out else ROOT / "out" / args.story_id / "repair_queue.json"
    if not out.is_absolute():
        out = ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(queue, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"groups": queue["counts"]["groups"], **queue["acceptance"], "output": str(out)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
