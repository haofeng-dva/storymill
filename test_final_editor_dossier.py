import copy
import unittest

from final_editor_contract import build_contract
from final_editor_dossier import (
    FinalEditorDossierError,
    copy_verified_dossier,
    dossier_is_usable,
    quote_anchor,
    verify_dossier,
)


CHAPTERS = {
    "ch0001.md": "Mara crosses the ward after the green light scans her palm. Rafe watches from the gate.",
    "ch0002.md": "Mara records the collar number. Soren gives her the records-room key.",
}


def contract():
    return build_contract("trial", "run1", CHAPTERS, created_at="2026-09-18T00:00:00Z")


def proposal():
    return {
        "story_id": "trial",
        "book_promise": "A forensic outsider investigates a dangerous inherited bond.",
        "voice_profile": "Close third-person prose uses concrete observation before emotional inference.",
        "chapter_roles": [
            {"chapter": 1, "function": "ward admission", "evidence_quotes": [
                {"file": "ch0001.md", "text": "Mara crosses the ward after the green light scans her palm."},
            ]},
            {"chapter": 2, "function": "evidence access", "evidence_quotes": [
                {"file": "ch0002.md", "text": "Soren gives her the records-room key."},
            ]},
        ],
        "findings": [{
            "id": "fe-001",
            "root_cause": "The records access is granted without a later investigative consequence.",
            "impact": "The story promise of evidence-led investigation stalls after the handoff.",
            "chapters": [2],
            "evidence_quotes": [{"file": "ch0002.md", "text": "Soren gives her the records-room key."}],
            "repair_scope": "scene",
            "requires_human_decision": False,
        }],
    }


class FinalEditorDossierTests(unittest.TestCase):
    def test_verifies_full_book_dossier_with_exact_anchors(self):
        dossier = verify_dossier(proposal(), CHAPTERS, contract())
        self.assertEqual(dossier["status"], "verified")
        self.assertTrue(dossier_is_usable(dossier))
        self.assertEqual(dossier["chapter_roles"][0]["anchors"][0]["line_start"], 1)
        self.assertEqual(dossier["findings"][0]["anchors"][0]["file"], "ch0002.md")
        self.assertEqual(copy_verified_dossier(dossier), dossier)

    def test_rejects_ambiguous_or_wrong_chapter_evidence_into_audit(self):
        texts = dict(CHAPTERS)
        texts["ch0002.md"] = "Echo phrase appears here. Echo phrase appears here. Soren gives her the records-room key."
        bad = proposal()
        bad["chapter_roles"][1]["evidence_quotes"] = [{"file": "ch0002.md", "text": "Echo phrase appears here."}]
        bad["findings"][0]["evidence_quotes"] = [{"file": "ch0001.md", "text": "Soren gives her the records-room key."}]
        dossier = verify_dossier(bad, texts, build_contract("trial", "run1", texts, created_at="now"))
        self.assertEqual(dossier["status"], "incomplete")
        self.assertEqual(dossier["findings"], [])
        self.assertGreaterEqual(len(dossier["rejected_records"]), 2)
        self.assertFalse(dossier_is_usable(dossier))

    def test_rejects_source_change_and_story_mismatch(self):
        changed = dict(CHAPTERS)
        changed["ch0002.md"] += " Another sentence."
        with self.assertRaisesRegex(FinalEditorDossierError, "source manifest changed"):
            verify_dossier(proposal(), changed, contract())

        wrong_story = proposal()
        wrong_story["story_id"] = "other"
        with self.assertRaisesRegex(FinalEditorDossierError, "story_id"):
            verify_dossier(wrong_story, CHAPTERS, contract())

    def test_rejects_invalid_scope_and_duplicate_chapter_role(self):
        bad = proposal()
        duplicate = copy.deepcopy(bad["chapter_roles"][0])
        bad["chapter_roles"].append(duplicate)
        bad["findings"][0]["repair_scope"] = "multi_chapter"
        dossier = verify_dossier(bad, CHAPTERS, contract())
        self.assertEqual(dossier["status"], "incomplete")
        self.assertEqual(len(dossier["chapter_roles"]), 2)
        self.assertEqual(dossier["findings"], [])

    def test_quote_anchor_requires_exact_unique_source_text(self):
        anchor, reason = quote_anchor("One sentence. One sentence.", "One sentence.")
        self.assertIsNone(anchor)
        self.assertIn("not unique", reason)
        anchor, reason = quote_anchor("One sentence.", "sentence")
        self.assertIsNone(anchor)
        self.assertIn("too short", reason)


if __name__ == "__main__":
    unittest.main()
