# -*- coding: utf-8 -*-
"""Second-pass, evidence-bound chapter repair for one reviewed story."""
import argparse
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

from engine import LLM, load_env, load_json
from repair_validator import local_validate


ROOT = Path(__file__).resolve().parent
FACT_DECISIONS = """
Use these fixed decisions when they are relevant. Do not introduce alternatives.
1. Elowen used an old Calder Trust authorization to issue Mara's contract without
   Rafe's current approval. Rafe's signature is a pre-authorized Trust mark, not
   his present consent. Mara signs once; her later signature is a receipt/terms
   acknowledgment, not a second contract acceptance.
2. The first injured rogue has the collar and survives after it is removed. The
   second rogue attacks Tavi and dies at the ward stone without a collar. Keep
   those bodies and objects distinct.
3. Rafe's shift is always painful and visibly violent. In the attack it is only
   fast because imminent danger forces him through it; retain a physical cost.
4. In chapter 12, 06 DAYS, 11 HOURS is the time until the full moon. The shorter
   00:04:17:36 and 00:17:42 displays are station lockdown/purge timers, not lunar
   timers. The second neonatal item must add new evidence, not repeat the first.
5. In chapter 9, the freestanding building is the outer records room. The sealed
   archive is below the old hall. Say this explicitly when needed.
6. In chapters 11-12, the abandoned weather station is the public cover name for
   the former Calder Regional Field Biology station.
7. In chapter 12, both early files are from Mara's age-six pediatric follow-up;
   the first is an intake sleeve and the later vial is the preserved blood sample.
""".strip()

SYSTEM = (
    "You are a rigorous fiction line editor. Return JSON only. Repair every concrete "
    "review issue that can be solved by a local replacement in the supplied chapter. "
    "Never rewrite a chapter, add a scene, alter names, or invent facts beyond the fixed "
    "decisions. Prefer deleting repeated wording or making a precise substitution."
)


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_ops(raw):
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", (raw or "").strip(), flags=re.I)
    data = json.loads(raw)
    return data.get("repairs", []) if isinstance(data, dict) and isinstance(data.get("repairs"), list) else []


def apply_ops(text, ops):
    original = text
    audit = []
    for op in ops:
        find = str(op.get("find", ""))
        replace = str(op.get("replace", ""))
        if not find or find == replace or len(find) > 1100 or len(replace) > 1400:
            audit.append({"status": "rejected", "reason": "invalid or no-op replacement"})
            continue
        if text.count(find) != 1:
            audit.append({"status": "rejected", "find": find[:160], "reason": "find is not unique"})
            continue
        text = text.replace(find, replace, 1)
        audit.append({"status": "applied", "issue": op.get("issue", ""), "find": find, "replace": replace, "reason": op.get("reason", "")})
    errors = local_validate(original, text)
    if errors:
        return original, [{"status": "rejected", "reason": "; ".join(errors)}]
    return text, audit


def main():
    parser = argparse.ArgumentParser(description="Apply constrained second-pass chapter repairs")
    parser.add_argument("--story-id", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--chapters", default="", help="comma-separated chapter numbers; default all")
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    report_path = Path(args.report)
    if not report_path.is_absolute():
        report_path = ROOT / report_path
    report = json.loads(report_path.read_text(encoding="utf-8"))
    wanted = {int(x) for x in args.chapters.split(",") if x.strip()} if args.chapters else set()
    story = ROOT / "shorts" / args.story_id
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = Path(args.out) if args.out else ROOT / "out" / args.story_id / f"round2_repair_{stamp}.json"
    if not out_path.is_absolute():
        out_path = ROOT / out_path
    backups = ROOT / "out" / args.story_id / "repair_backups" / f"round2_{stamp}"
    load_env()
    llm = LLM(load_json(ROOT / "config.json"))
    result = {"schema": "round2_repair_v1", "fact_decisions": FACT_DECISIONS, "files": []}
    for row in report:
        filename = row.get("file", "")
        number_match = re.fullmatch(r"ch(\d+)\.md", filename)
        if not number_match:
            continue
        number = int(number_match.group(1))
        if wanted and number not in wanted:
            continue
        path = story / filename
        before = path.read_text(encoding="utf-8")
        prompt = {
            "task": "Return exactly {repairs:[{issue,find,replace,reason}]}. Use only exact contiguous find text from chapter.",
            "fixed_story_decisions": FACT_DECISIONS,
            "review_issues": row.get("content", ""),
            "constraints": [
                "Address all concrete review issues where a local change can resolve them.",
                "Do not add a repair for a purely subjective preference unless replacing a repeated or vague phrase improves clarity.",
                "Each replacement must be one or two sentences at most.",
                "For a deletion, replace with the smallest grammatical text, not an empty scene.",
                "Do not change word count by more than 100 words in total.",
            ],
            "chapter": before,
        }
        response = llm.chat("writer", [{"role": "system", "content": SYSTEM}, {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)}], max_tokens=5000, temperature=0.12)
        if "error" in response:
            result["files"].append({"file": filename, "status": "error", "error": response["error"]})
        else:
            try:
                ops = parse_ops(response.get("content", ""))
                after, audit = apply_ops(before, ops)
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                result["files"].append({"file": filename, "status": "error", "error": f"invalid JSON: {exc}"})
            else:
                applied = [item for item in audit if item["status"] == "applied"]
                if applied:
                    backups.mkdir(parents=True, exist_ok=True)
                    (backups / filename).write_text(before, encoding="utf-8")
                    path.write_text(after, encoding="utf-8")
                    result["files"].append({"file": filename, "status": "applied", "before_sha256": sha(before), "after_sha256": sha(after), "repairs": audit})
                else:
                    result["files"].append({"file": filename, "status": "no_change", "repairs": audit})
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"files": len(result["files"]), "applied": sum(row["status"] == "applied" for row in result["files"]), "out": str(out_path)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
