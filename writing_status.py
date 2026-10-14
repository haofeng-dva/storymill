"""Shared run-level writing status contract.

The chapter database remains the detailed audit log. This small file is the
cross-process handoff consumed by orchestration and packaging.
"""
import json
import os


def status_path(story_dir):
    return os.path.join(story_dir, "writing_status.json")


def write_status(story_dir, *, run_id, story_id, expected_chapters,
                 completed_chapters, missing_chapters):
    missing = sorted(int(n) for n in missing_chapters)
    completed = sorted(int(n) for n in completed_chapters)
    payload = {
        "run_id": run_id,
        "story_id": story_id,
        "status": "completed" if not missing else "partial",
        "expected_chapters": int(expected_chapters),
        "completed_chapters": completed,
        "missing_chapters": missing,
    }
    path = status_path(story_dir)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    return path, payload


def load_status(story_dir):
    path = status_path(story_dir)
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("writing status must be a JSON object")
    return payload


def is_deliverable(payload):
    return (
        payload.get("status") == "completed"
        and not payload.get("missing_chapters")
        and int(payload.get("expected_chapters", 0)) > 0
        and len(payload.get("completed_chapters", [])) == int(payload["expected_chapters"])
    )
