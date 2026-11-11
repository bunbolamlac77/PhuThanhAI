"""S04: report assembly against the frozen V2/V3 contracts."""

from __future__ import annotations

import unittest
import uuid
from pathlib import Path

from phuthanhai.analyzer import report
from phuthanhai.contracts.validators import parse_portable_workspace_md

CONTRACTS = Path(__file__).resolve().parents[1] / "contracts"
SHA = "a" * 64
OBS = {
    "description": "An indoor room with a long table, flowers, a shrine, incense and a statue.",
    "objects": ["long table", "flowers", "shrine", "incense", "statue"],
    "actions": [],
    "camera": "fixed",
    "lighting": "even",
    "visible_text": [],
    "uncertainty": ["shrine-like setup"],
}


def fixture_docs():
    pid, aid, anid = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    evidence = {
        "analysis_id": anid,
        "coverage_status": "sufficient",
        "warnings": [],
        "source_sha256": SHA,
        "model_identity": {"name": "visual_analysis_primary"},
        "frames": [
            {
                "frame_id": "F0001", "scene_id": "S0001", "normalized_pts_ms": 100,
                "interpretation_level": "observed", "observation": OBS, "source_sha256": SHA,
            },
            {
                "frame_id": "F0002", "scene_id": "S0002", "normalized_pts_ms": 1500,
                "interpretation_level": "observed",
                "observation": {**OBS, "description": "A long table with plates, food and guests."},
                "source_sha256": SHA,
            },
        ],
    }
    manifest = {
        "timeline": {
            "duration_ms": 2000, "width": 360, "height": 640, "fps_nominal": "25/1",
            "vfr_detected": False, "has_audio": True, "first_video_pts_ms": 0, "filename": "clip.mp4",
        },
        "profile": "detailed", "expected_max_gap_ms": 12000, "observed_max_gap_ms": 1400,
    }
    project = {
        "project_id": pid, "name": "project", "analysis_profile": "wedding_traditional",
        "profile_revision": 2, "revision": 1, "created_at": "2026-01-01T00:00:00+00:00",
    }
    asset = {
        "asset_id": aid, "filename": "clip.mp4", "duration_ms": 2000, "width": 360, "height": 640,
        "fps_nominal": "25/1", "has_audio": True, "review": {"rating": 4, "favorite": True, "tags": ["wedding"]},
    }
    annotation_id = str(uuid.uuid4())
    annotations = [{
        "annotation_id": annotation_id, "project_id": pid, "asset_id": aid, "scope": "clip",
        "start_ms": None, "end_ms": None, "text": "Ghi chú riêng tư", "note_type": "context",
        "source": "user_annotation", "assertion_status": "user_asserted",
        "wall_time_of_day": None, "wall_date": None, "wall_timezone": None, "wall_precision": None,
        "revision": 1, "created_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-01-01T00:00:00+00:00",
    }]
    overlay = {
        "profile": "wedding_traditional", "profile_label_vi": "Đám cưới",
        "candidates": [{
            "label_key": "ancestor_altar", "label_vi": "Bàn gia tiên", "description_vi": "Khu thờ",
            "uncertainty_vi": "Chưa đủ nghi lễ", "signals": ["shrine", "incense", "statue"],
            "evidence_frames": ["F0001"], "evidence_scenes": ["S0001"],
            "evidence_times_ms": [100], "interpretation_level": "hypothesis", "category": "ritual",
        }],
        "user_labels": [{
            "label_key": "ancestor_altar", "label_vi": "Bàn gia tiên", "status": "user_confirmed",
            "interpretation_level": "user_asserted", "provenance": "user_annotation",
            "annotation_id": str(uuid.uuid4()), "revision": 1, "scene_id": "S0001", "frame_id": None,
            "evidence_frames": ["F0001"], "evidence_times_ms": [100], "original_candidate": None,
        }],
        "dismissed_label_keys": [], "raw_frames": evidence["frames"],
    }
    docs = report.assemble_documents(
        project=project, assets=[asset], annotations=annotations,
        evidence_by_asset={aid: evidence}, overlays={aid: overlay}, manifests={aid: manifest},
    )
    return docs, [asset], {aid: overlay}, {aid: evidence}


class ReportContractTests(unittest.TestCase):
    def test_documents_validate_against_frozen_contracts(self) -> None:
        docs, _, _, _ = fixture_docs()
        report.validate_documents(docs, CONTRACTS)
        self.assertEqual(docs["package"]["kind"], "phuthanhai.workspace_package")
        self.assertEqual(docs["bundle"]["report_profile"], "wedding_traditional")
        self.assertEqual(docs["bundle"]["manifest"]["analysis_profile"], "wedding_traditional")

    def test_scenes_tile_the_timeline(self) -> None:
        docs, _, _, _ = fixture_docs()
        analysis = docs["analyses"][0]
        self.assertEqual(analysis["scenes"][0]["start_ms"], 0)
        self.assertEqual(analysis["scenes"][-1]["end_ms"], analysis["source"]["duration_ms"])
        for left, right in zip(analysis["scenes"], analysis["scenes"][1:]):
            self.assertEqual(left["end_ms"], right["start_ms"])

    def test_markdown_separates_layers_and_does_not_claim_audio(self) -> None:
        docs, assets, overlays, evidence = fixture_docs()
        video, project_md, portable = report.build_markdown(docs, assets, overlays, evidence)
        for heading in ("AI quan sát", "AI gợi ý theo ngữ cảnh", "Người dùng xác nhận"):
            self.assertIn(heading, video)
        self.assertIn("không nghe", video.lower())
        self.assertIn("Bàn gia tiên", video)
        self.assertIn("Báo cáo dự án", project_md)
        self.assertIn("phuthanhai_contract:", portable)

    def test_portable_roundtrip(self) -> None:
        docs, assets, overlays, evidence = fixture_docs()
        _, _, portable = report.build_markdown(docs, assets, overlays, evidence)
        parsed = parse_portable_workspace_md(portable)
        self.assertEqual(parsed["project_id"], docs["package"]["project_id"])
        self.assertEqual(
            parsed["legacy_bundle"]["report_profile"], "wedding_traditional"
        )
        report.validate_documents(
            {"bundle": parsed["legacy_bundle"], "extension": parsed["extension"],
             "package": parsed, "analyses": parsed["legacy_bundle"]["analyses"]},
            CONTRACTS,
        )

    def test_warnings_state_audio_not_transcribed(self) -> None:
        docs, _, _, _ = fixture_docs()
        self.assertIn("audio_not_transcribed", docs["analyses"][0]["warnings"])
        self.assertIn("sampled_frames_not_all_frames", docs["analyses"][0]["warnings"])

    def test_correction_is_exported_with_provenance(self) -> None:
        docs, _, _, _ = fixture_docs()
        self.assertEqual(len(docs["extension"]["corrections"]), 1)
        correction = docs["extension"]["corrections"][0]
        self.assertEqual(correction["corrected_text"], "Bàn gia tiên")
        self.assertEqual(correction["author"], "user")


if __name__ == "__main__":
    unittest.main()
