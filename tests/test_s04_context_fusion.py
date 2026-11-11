"""S04: context fusion must separate altar, decoration and banquet tables."""

from __future__ import annotations

import unittest

from phuthanhai.analyzer.context_fusion import build_overlay, raw_evidence_digest


def frame(frame_id: str, scene: str, pts: int, description: str, objects: list[str] | None = None) -> dict:
    return {
        "frame_id": frame_id,
        "scene_id": scene,
        "normalized_pts_ms": pts,
        "interpretation_level": "observed",
        "observation": {
            "description": description,
            "objects": objects or [],
            "actions": [],
            "camera": "fixed",
            "lighting": "even",
            "visible_text": [],
            "uncertainty": [],
        },
    }


def evidence(frames: list[dict]) -> dict:
    return {
        "analysis_id": "11111111-1111-4111-8111-111111111111",
        "coverage_status": "sufficient",
        "frames": frames,
    }


ALTAR = evidence([
    frame("F0001", "S0001", 100, "A table with a shrine, incense, offerings, a statue and red backdrop.", ["shrine", "incense", "offering", "statue", "backdrop"]),
])
DECORATION = evidence([
    frame("F0001", "S0001", 100, "A long table decorated with flowers and a centerpiece, plates and chairs.", ["long table", "flowers", "centerpiece", "plates", "chairs"]),
])
BANQUET = evidence([
    frame("F0001", "S0001", 100, "A long table with plates, food, glasses and chairs for guests.", ["long table", "plates", "food", "glasses", "chairs"]),
])
SHRINE_LIKE = evidence([
    frame("F0001", "S0001", 100, "A table with a shrine-like decoration and flowers, no incense.", ["table", "shrine", "flowers"]),
])


def keys(overlay: dict) -> set[str]:
    return {item["label_key"] for item in overlay["candidates"]}


class AltarDistinctionTests(unittest.TestCase):
    def test_altar_with_ritual_evidence_is_a_candidate(self) -> None:
        overlay = build_overlay(asset_id="a", asset_duration_ms=2000, evidence=ALTAR, profile_id="wedding_traditional")
        self.assertIn("ancestor_altar", keys(overlay))
        candidate = next(item for item in overlay["candidates"] if item["label_key"] == "ancestor_altar")
        self.assertEqual(candidate["interpretation_level"], "hypothesis")
        self.assertEqual(candidate["evidence_frames"], ["F0001"])

    def test_flower_table_is_not_an_altar(self) -> None:
        overlay = build_overlay(asset_id="a", asset_duration_ms=2000, evidence=DECORATION, profile_id="wedding_traditional")
        self.assertNotIn("ancestor_altar", keys(overlay))
        self.assertIn("wedding_decoration", keys(overlay))

    def test_banquet_table_is_not_an_altar(self) -> None:
        overlay = build_overlay(asset_id="a", asset_duration_ms=2000, evidence=BANQUET, profile_id="wedding_traditional")
        self.assertNotIn("ancestor_altar", keys(overlay))
        self.assertIn("banquet_table", keys(overlay))

    def test_shrine_like_without_ritual_is_unresolved(self) -> None:
        overlay = build_overlay(asset_id="a", asset_duration_ms=2000, evidence=SHRINE_LIKE, profile_id="wedding_traditional")
        self.assertNotIn("ancestor_altar", keys(overlay))
        self.assertTrue(any(item["label_key"] == "ancestor_altar" for item in overlay["unresolved"]))

    def test_general_profile_never_suggests_wedding(self) -> None:
        overlay = build_overlay(asset_id="a", asset_duration_ms=2000, evidence=ALTAR, profile_id="general")
        wedding = {item["label_key"] for item in overlay["candidates"]} & {
            "ancestor_altar", "wedding_reception", "wedding_gate", "ancestor_ceremony"
        }
        self.assertEqual(wedding, set())


class UserLabelTests(unittest.TestCase):
    def test_user_confirmation_is_provenance_tagged_and_raw_stays_identical(self) -> None:
        before = raw_evidence_digest(ALTAR)
        overlay = build_overlay(
            asset_id="a",
            asset_duration_ms=2000,
            evidence=ALTAR,
            profile_id="wedding_traditional",
            user_labels=[{
                "label_key": "ancestor_altar",
                "label_vi": "Bàn gia tiên",
                "status": "confirmed",
                "annotation_id": "22222222-2222-4222-8222-222222222222",
                "scene_id": "S0001",
                "frame_id": None,
                "revision": 1,
            }],
        )
        self.assertEqual(overlay["raw_evidence_sha256"], before)
        entry = overlay["user_labels"][0]
        self.assertEqual(entry["provenance"], "user_annotation")
        self.assertEqual(entry["interpretation_level"], "user_asserted")
        self.assertEqual(entry["annotation_id"], "22222222-2222-4222-8222-222222222222")

    def test_dismissed_label_removes_candidate(self) -> None:
        overlay = build_overlay(
            asset_id="a",
            asset_duration_ms=2000,
            evidence=ALTAR,
            profile_id="wedding_traditional",
            user_labels=[{
                "label_key": "ancestor_altar",
                "label_vi": "Bàn gia tiên",
                "status": "dismissed",
                "annotation_id": "22222222-2222-4222-8222-222222222222",
                "scene_id": "S0001",
                "frame_id": None,
                "revision": 1,
            }],
        )
        self.assertNotIn("ancestor_altar", keys(overlay))
        self.assertEqual(overlay["dismissed_label_keys"], ["ancestor_altar"])

    def test_scene_cards_reference_real_frames(self) -> None:
        overlay = build_overlay(asset_id="a", asset_duration_ms=2000, evidence=ALTAR, profile_id="wedding_traditional")
        self.assertEqual(overlay["scenes"][0]["scene_id"], "S0001")
        self.assertEqual(overlay["scenes"][0]["end_ms"], 2000)
        self.assertEqual(overlay["scenes"][0]["frame_ids"], ["F0001"])


if __name__ == "__main__":
    unittest.main()


class RecognitionImprovementTests(unittest.TestCase):
    def classify(self, description: str, objects: list[str] | None = None) -> set[str]:
        ev = evidence([frame("F0001", "S0001", 100, description, objects or [])])
        overlay = build_overlay(asset_id="a", asset_duration_ms=3000, evidence=ev, profile_id="wedding_traditional")
        return {item["label_key"] for item in overlay["candidates"]}

    def test_negation_blocks_altar_claim(self) -> None:
        keys = self.classify("A table with flowers and decorations, no incense, not an altar, without offerings.")
        self.assertNotIn("ancestor_altar", keys)

    def test_altar_like_single_signal_stays_unresolved(self) -> None:
        ev = evidence([frame("F0001", "S0001", 100, "An altar-like decoration with flowers and a centerpiece.", ["altar-like", "flowers"])])
        overlay = build_overlay(asset_id="a", asset_duration_ms=3000, evidence=ev, profile_id="wedding_traditional")
        self.assertNotIn("ancestor_altar", {c["label_key"] for c in overlay["candidates"]})
        self.assertTrue(any(item["label_key"] == "ancestor_altar" for item in overlay["unresolved"]))

    def test_vid022_like_description_recalls_altar(self) -> None:
        keys = self.classify(
            "A room with a shrine-like setup with statues, offerings and flowers behind a long table.",
            ["shrine-like", "statue", "offerings", "flowers"],
        )
        self.assertIn("ancestor_altar", keys)

    def test_vid013_like_description_recalls_prep_and_photo(self) -> None:
        keys = self.classify(
            "A person in a red outfit stands behind a table with fruits and flowers. "
            "A person's hands hold a camera, photographing the woman.",
            ["person", "red outfit", "fruit", "camera"],
        )
        self.assertIn("bride_groom_prep", keys)
        self.assertIn("family_photos", keys)

    def test_word_boundary_avoids_substring_false_positive(self) -> None:
        keys = self.classify("Delegates at the entrance with a welcome board and flowers.", ["delegates", "entrance", "flowers"])
        self.assertNotIn("wedding_gate", keys)

    def test_vietnamese_negation_is_respected(self) -> None:
        keys = self.classify("Bàn trang trí hoa, không có hương, không phải bàn thờ.", ["bàn", "hoa"])
        self.assertNotIn("ancestor_altar", keys)
