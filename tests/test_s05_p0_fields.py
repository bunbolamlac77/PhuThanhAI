"""S05 P0: manifest scene boundaries and scoped label dismissal."""

from __future__ import annotations

import unittest
import uuid

from phuthanhai.analyzer.context_fusion import build_overlay
from phuthanhai.analyzer.report import assemble_documents, validate_documents
from pathlib import Path

CONTRACTS = Path(__file__).resolve().parents[1] / "contracts"


def frame(frame_id: str, scene_id: str, pts: int, description: str = "Shrine with incense and offerings") -> dict:
    return {
        "frame_id": frame_id, "scene_id": scene_id, "normalized_pts_ms": pts,
        "interpretation_level": "observed",
        "observation": {
            "description": description, "objects": ["shrine", "incense", "offering"],
            "actions": [], "camera": "fixed", "lighting": "even", "visible_text": [], "uncertainty": [],
        },
    }


def evidence(frames: list[dict]) -> dict:
    return {
        "analysis_id": str(uuid.uuid4()), "coverage_status": "sufficient",
        "source_sha256": "a" * 64, "model_identity": {"name": "visual_analysis_primary"},
        "warnings": [], "frames": frames,
    }


WINDOWS = [
    {"scene_id": "S0001", "start_ms": 0, "end_ms": 3000},
    {"scene_id": "S0002", "start_ms": 3000, "end_ms": 6000},
]


class SceneBoundaryTests(unittest.TestCase):
    def test_scene_uses_manifest_start_not_first_frame(self) -> None:
        ev = evidence([frame("F0001", "S0001", 2200)])
        overlay = build_overlay(
            asset_id="a", asset_duration_ms=6000, evidence=ev, profile_id="wedding_traditional",
            scene_windows=WINDOWS,
        )
        scene = next(item for item in overlay["scenes"] if item["scene_id"] == "S0001")
        self.assertEqual(scene["start_ms"], 0)
        self.assertEqual(scene["end_ms"], 3000)
        self.assertEqual(overlay["scene_timecode_source"], "manifest")
        self.assertEqual(overlay["scene_timecode_status"], "verified")

    def test_all_frames_inside_their_manifest_scene(self) -> None:
        ev = evidence([frame("F0001", "S0001", 1200), frame("F0002", "S0001", 2400), frame("F0003", "S0002", 4200)])
        overlay = build_overlay(asset_id="a", asset_duration_ms=6000, evidence=ev, profile_id="wedding_traditional", scene_windows=WINDOWS)
        for scene in overlay["scenes"]:
            for frame_id in scene["frame_ids"]:
                pts = next(f["normalized_pts_ms"] for f in ev["frames"] if f["frame_id"] == frame_id)
                self.assertTrue(scene["start_ms"] <= pts < scene["end_ms"])

    def test_frame_outside_window_is_rejected(self) -> None:
        ev = evidence([frame("F0001", "S0001", 4200)])
        overlay = build_overlay(asset_id="a", asset_duration_ms=6000, evidence=ev, profile_id="wedding_traditional", scene_windows=WINDOWS)
        self.assertEqual(overlay["scene_timecode_status"], "invalid")
        self.assertTrue(overlay["scene_boundary_errors"])

    def test_missing_manifest_is_unverified_not_invented(self) -> None:
        ev = evidence([frame("F0001", "S0001", 2200)])
        overlay = build_overlay(asset_id="a", asset_duration_ms=6000, evidence=ev, profile_id="wedding_traditional")
        self.assertEqual(overlay["scene_timecode_status"], "unverified")
        self.assertEqual(overlay["scene_timecode_source"], "frame_derived")


class ScopedDismissTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ev = evidence([
            frame("F0001", "S0001", 1200, "Shrine with incense and offerings"),
            frame("F0002", "S0002", 4200, "Shrine with incense and offerings"),
        ])

    def dismiss(self, scene_id: str | None) -> dict:
        return {
            "label_key": "ancestor_altar", "label_vi": "Bàn gia tiên", "status": "dismissed",
            "annotation_id": str(uuid.uuid4()), "scene_id": scene_id, "frame_id": None, "revision": 1,
        }

    def test_scene_dismissal_does_not_remove_other_scene(self) -> None:
        overlay = build_overlay(
            asset_id="a", asset_duration_ms=6000, evidence=self.ev, profile_id="wedding_traditional",
            user_labels=[self.dismiss("S0001")], scene_windows=WINDOWS,
        )
        altar = next(item for item in overlay["candidates"] if item["label_key"] == "ancestor_altar")
        self.assertEqual(altar["evidence_frames"], ["F0002"])
        self.assertNotIn("S0001", altar["evidence_scenes"])
        self.assertEqual(overlay["dismissed_labels"][0]["scope"], "scene")

    def test_clip_dismissal_removes_all_frames(self) -> None:
        overlay = build_overlay(
            asset_id="a", asset_duration_ms=6000, evidence=self.ev, profile_id="wedding_traditional",
            user_labels=[self.dismiss(None)], scene_windows=WINDOWS,
        )
        self.assertFalse(any(item["label_key"] == "ancestor_altar" for item in overlay["candidates"]))
        self.assertEqual(overlay["dismissed_labels"][0]["scope"], "clip")


class ReportBoundaryTests(unittest.TestCase):
    def build(self, segments):
        ev = evidence([frame("F0001", "S0001", 2200), frame("F0002", "S0002", 4200)])
        project_id, asset_id = str(uuid.uuid4()), str(uuid.uuid4())
        project = {"project_id": project_id, "name": "p", "analysis_profile": "wedding_traditional",
                   "profile_revision": 1, "revision": 1, "created_at": "2026-01-01T00:00:00+00:00"}
        asset = {"asset_id": asset_id, "filename": "clip.mp4", "duration_ms": 6000, "width": 360,
                 "height": 640, "fps_nominal": "25/1", "has_audio": False}
        manifest = {"timeline": {"duration_ms": 6000, "width": 360, "height": 640, "fps_nominal": "25/1",
                                 "vfr_detected": False, "has_audio": False, "first_video_pts_ms": 0,
                                 "filename": "clip.mp4"},
                    "profile": "quick", "expected_max_gap_ms": 12000, "observed_max_gap_ms": 2000}
        if segments is not None:
            manifest["segments"] = segments
        overlay = build_overlay(asset_id=asset_id, asset_duration_ms=6000, evidence=ev,
                                profile_id="wedding_traditional", scene_windows=segments)
        docs = assemble_documents(project=project, assets=[asset], annotations=[],
                                  evidence_by_asset={asset_id: ev}, overlays={asset_id: overlay},
                                  manifests={asset_id: manifest})
        return docs

    def test_report_scene_boundaries_come_from_manifest(self) -> None:
        docs = self.build(WINDOWS)
        validate_documents(docs, CONTRACTS)
        analysis = docs["analyses"][0]
        self.assertEqual(analysis["scenes"][0]["start_ms"], 0)
        self.assertEqual(analysis["scenes"][1]["start_ms"], 3000)
        self.assertNotIn("scene_timecode_unverified", analysis["warnings"])

    def test_missing_segments_never_report_complete(self) -> None:
        docs = self.build(None)
        analysis = docs["analyses"][0]
        self.assertEqual(analysis["status"], "partial")
        self.assertIn("scene_timecode_unverified", analysis["warnings"])


if __name__ == "__main__":
    unittest.main()
