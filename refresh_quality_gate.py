# -*- coding: utf-8 -*-
"""Refresh current-text chapter quality evidence without regenerating prose."""
import argparse
import os
import re
import sys
from pathlib import Path

from engine import load_env
from native_check import QualityJudge, load_env as reviewer_env
import quality_checks
import quality_gate


ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description="Refresh chapter quality-gate evidence")
    parser.add_argument("--story-id", required=True)
    parser.add_argument("--quality-min", type=int, default=4)
    parser.add_argument("--chapters", default="", help="optional comma-separated chapter numbers")
    args = parser.parse_args()
    story_dir = ROOT / "shorts" / args.story_id
    chapters = sorted(path for path in story_dir.glob("ch????.md") if re.fullmatch(r"ch\d{4}\.md", path.name))
    if args.chapters:
        wanted = {int(value) for value in args.chapters.split(",") if value.strip()}
        chapters = [path for path in chapters if int(path.stem[2:]) in wanted]
    if not chapters:
        print("no canonical chapters found")
        return 2
    load_env()
    judge = QualityJudge(reviewer_env())
    failed = []
    for chapter_path in chapters:
        chapter_no = int(chapter_path.stem[2:])
        text = chapter_path.read_text(encoding="utf-8")
        score, reason, provider = judge.blind(text)
        warnings = quality_checks.local_checks(text, target=2500, lo=2250, hi=2750)
        warnings.extend(quality_checks.repetition_check(text))
        warnings.extend(quality_checks.redundancy_check(text))
        report = quality_gate.build_report(
            text,
            chapter_no,
            blind={"score": score, "threshold": args.quality_min, "reason": reason, "provider": provider},
            local_warnings=warnings,
        )
        quality_gate.write_report(story_dir / f"ch{chapter_no:04d}.quality_gate.json", report)
        print(f"ch{chapter_no:04d}: score={score} provider={provider} decision={report['decision']}", flush=True)
        if report["decision"] != "passed":
            failed.append(chapter_no)
    print(f"refreshed={len(chapters)} failed={failed}")
    return 0 if not failed else 2


if __name__ == "__main__":
    sys.exit(main())
