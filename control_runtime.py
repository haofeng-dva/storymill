# -*- coding: utf-8 -*-
"""Runtime orchestration for opt-in controlled chapter generation."""
import json
from pathlib import Path

from control_checks import (
    append_control_findings,
    check_prose_budget,
    evaluate_state_delta_output,
    manual_review_findings,
    write_state_delta,
)
from control_context import build_control_context, load_state_delta
from control_contracts import ControlContractError, sha256_text


class ControlledGenerationRuntime:
    """Keep controlled-mode file I/O and validation out of the writing engine."""

    def __init__(self, story_id, control_dir, output_dir, bundle):
        self.story_id = story_id
        self.control_dir = Path(control_dir)
        self.output_dir = Path(output_dir)
        self.bundle = bundle
        self.artifacts = bundle["artifacts"]
        self.book_counts = {}

    @property
    def findings_path(self):
        return self.output_dir / "control_findings.json"

    def context_for(self, chapter_no):
        previous = load_state_delta(self.control_dir, chapter_no - 1) if chapter_no > 1 else None
        result = build_control_context(self.bundle, chapter_no, previous)
        if chapter_no > 1 and result["diagnostics"]["state_delta_status"] != "available":
            raise ControlContractError(
                "controlled generation requires the actual previous state delta; D4 is not yet available"
            )
        return result

    def record_state_delta(self, chapter_no, text, response):
        """Persist one validated delta; return False only for a blocking state failure."""
        source_sha256 = sha256_text(text)
        contract = self.artifacts["chapter_contracts"]["chapters"].get(f"{chapter_no:04d}")
        scenes = self.scenes_for(chapter_no)
        if not contract:
            self._append_state_failure(chapter_no, "missing-chapter-contract", "No control contract for chapter.", source_sha256)
            return False
        if "error" in response:
            self._append_state_failure(chapter_no, "state-delta-generation-failed", response["error"], source_sha256)
            return False
        try:
            delta, findings = evaluate_state_delta_output(
                response.get("content", ""), chapter_no, source_sha256, contract, text, scenes
            )
            write_state_delta(self.control_dir, delta, findings)
        except ControlContractError as exc:
            self._append_state_failure(chapter_no, "invalid-state-delta", str(exc), source_sha256)
            return False
        if findings:
            self._append_findings(chapter_no, text, findings)
            return not any(item.get("severity") == "blocking" for item in findings)
        return True

    def scenes_for(self, chapter_no):
        return self.artifacts["scene_ledger"]["chapters"].get(f"{chapter_no:04d}", [])

    def post_write_findings(self, chapter_no, text):
        """Write deterministic and manual-review cards, returning blocking budget findings."""
        key = f"{chapter_no:04d}"
        scenes = self.artifacts["scene_ledger"]["chapters"][key]
        participants = {name for scene in scenes for name in scene["participants"]}
        voice_cards = [
            card for card in self.artifacts["character_voice_cards"]["characters"]
            if card["name"] in participants
        ]
        budget_findings, _, self.book_counts = check_prose_budget(
            text, self.artifacts["prose_budget"], chapter_no, self.book_counts
        )
        manual_findings = manual_review_findings(
            chapter_no, scenes, voice_cards,
            self.artifacts["opening_contract"] if chapter_no == 1 else None,
        )
        self._append_findings(chapter_no, text, manual_findings + budget_findings)
        return budget_findings

    def _append_findings(self, chapter_no, text, findings):
        """Attach immutable source provenance before a card reaches repair routing."""
        source_sha256 = sha256_text(text)
        enriched = []
        for finding in findings:
            item = dict(finding)
            item.update({
                "story_id": self.story_id,
                "chapter": chapter_no,
                "source_file": f"ch{chapter_no:04d}.md",
                "source_sha256": item.get("source_sha256") or source_sha256,
                "repair_scope": "scene" if item.get("scene_id") else "chapter",
            })
            enriched.append(item)
        append_control_findings(self.findings_path, enriched)

    def _append_state_failure(self, chapter_no, rule_id, message, source_sha256):
        finding = {
            "source": "control_state", "chapter": chapter_no, "category": "state",
            "severity": "blocking", "rule_id": rule_id,
            "root_cause_key": rule_id, "message": message,
        }
        # State-generation failures have no trustworthy quote, but they still
        # bind to the exact chapter that must be regenerated or inspected.
        self._append_findings(chapter_no, "", [{**finding, "source_sha256": source_sha256}])

    def write_repair_request(self, card_id):
        """Create a non-mutating, chapter-local repair input from one explicit card."""
        if not self.findings_path.is_file():
            raise ControlContractError(f"control findings not found: {self.findings_path}")
        findings = json.loads(self.findings_path.read_text(encoding="utf-8")).get("findings", [])
        card = next((item for item in findings if item.get("card_id") == card_id), None)
        if card is None:
            raise ControlContractError(f"unknown control repair card: {card_id}")
        if card.get("story_id") != self.story_id:
            raise ControlContractError(f"repair card story_id does not match current story: {card_id}")
        chapter_no = card.get("chapter")
        if not isinstance(chapter_no, int) or chapter_no < 1:
            raise ControlContractError(f"repair card has invalid chapter: {card_id}")
        chapter_path = self.output_dir / f"ch{chapter_no:04d}.md"
        if not chapter_path.is_file():
            raise ControlContractError(f"current chapter source not found for repair card: {chapter_path}")
        chapter_text = chapter_path.read_text(encoding="utf-8")
        if card.get("source_sha256") != sha256_text(chapter_text):
            raise ControlContractError(f"repair card source hash does not match current chapter: {card_id}")
        scenes = self.scenes_for(chapter_no)
        scene = next((item for item in scenes if item["scene_id"] == card.get("scene_id")), None)
        scope = "scene" if scene else "chapter"
        source = {
            "file": chapter_path.name,
            "sha256": card["source_sha256"],
        }
        if scope == "scene":
            evidence = card.get("evidence")
            if not isinstance(evidence, str) or evidence not in chapter_text:
                raise ControlContractError(f"scene repair card lacks a verified current-chapter evidence anchor: {card_id}")
            offset = chapter_text.index(evidence)
            source["anchor"] = evidence
            source["context"] = chapter_text[max(0, offset - 600):min(len(chapter_text), offset + len(evidence) + 600)]
        else:
            source["chapter_text"] = chapter_text
        request = {
            "schema": "control_repair_request_v2",
            "story_id": self.story_id,
            "card": card,
            "chapter": chapter_no,
            "scope": scope,
            "source": source,
            "contract": self.artifacts["chapter_contracts"]["chapters"][f"{chapter_no:04d}"],
            "scene": scene,
            "instructions": [
                "Rewrite only the named chapter or scene; do not change other chapters.",
                "Preserve every contract assertion and fact-slot value.",
                "Do not replay any scene already completed once.",
                "Return a complete replacement for the scoped chapter or scene, not commentary.",
            ],
        }
        path = self.control_dir / "repairs" / f"{card_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(request, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path
