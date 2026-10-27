# -*- coding: utf-8 -*-
"""Validate explicit story decisions without asking a model to remember them."""
import argparse
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def _anchor(text, start, end):
    lines = text.splitlines()
    first = text.count("\n", 0, start) + 1
    last = text.count("\n", 0, end) + 1
    return {
        "line_start": first,
        "line_end": last,
        "excerpt": text[start:end],
        "context_before": lines[max(0, first - 3):first - 1],
        "context_after": lines[last:min(len(lines), last + 2)],
    }


def _card(fact, check, text, match):
    card = {
        "source": "fact_contract",
        "fact_id": fact["id"],
        "root_cause_key": fact["id"],
        "category": "continuity",
        "severity": "blocking",
        "message": check["message"],
        "canonical_fact": fact["canonical_fact"],
    }
    card.update(_anchor(text, match.start(), match.end()))
    return card


def check_text(facts, chapter, text):
    """Return only violated, source-anchored contract checks for one chapter."""
    results = []
    for fact in facts.get("facts", []):
        if chapter not in fact.get("chapters", []):
            continue
        for check in fact.get("checks", []):
            kind = check.get("kind")
            if kind == "prior_evidence":
                for trigger in re.finditer(check["trigger"], text, re.I):
                    if not re.search(check["evidence"], text[:trigger.start()], re.I | re.S):
                        results.append(_card(fact, check, text, trigger))
            elif kind == "nearby_evidence":
                for trigger in re.finditer(re.escape(check["trigger"]), text, re.I):
                    window = int(check.get("window", 400))
                    nearby = text[max(0, trigger.start() - window):trigger.end() + window]
                    if not re.search(check["evidence"], nearby, re.I | re.S):
                        results.append(_card(fact, check, text, trigger))
            elif kind == "forbid_before":
                boundary = re.search(check["before"], text, re.I)
                boundary_at = boundary.start() if boundary else len(text)
                for forbidden in re.finditer(check["pattern"], text[:boundary_at], re.I):
                    results.append(_card(fact, check, text, forbidden))
    return results


def load_contract(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def check_story(story_dir, contract_path):
    contract = load_contract(contract_path)
    findings = []
    for path in sorted(Path(story_dir).glob("ch*.md")):
        match = re.fullmatch(r"ch(\d+)\.md", path.name)
        if not match:
            continue
        chapter = int(match.group(1))
        for finding in check_text(contract, chapter, path.read_text(encoding="utf-8")):
            finding.update({"chapter": chapter, "file": path.name})
            findings.append(finding)
    return findings


def main():
    parser = argparse.ArgumentParser(description="Check source-anchored fiction fact contracts")
    parser.add_argument("--story-id", required=True)
    parser.add_argument("--contract", default="")
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    story_dir = ROOT / "shorts" / args.story_id
    contract = Path(args.contract) if args.contract else story_dir / "story_facts.json"
    findings = check_story(story_dir, contract)
    output = {"schema": "story_fact_findings_v1", "story_id": args.story_id, "contract": str(contract.resolve()), "findings": findings}
    if args.out:
        out = Path(args.out)
        if not out.is_absolute():
            out = ROOT / out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"findings": len(findings), "blocking": len(findings)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
