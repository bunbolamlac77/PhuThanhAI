"""S05.4: smart selection, explanation and independent per-frame cache."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from phuthanhai.analyzer.optimizer import FrameResultCache, build_selection
from phuthanhai.analyzer.vision_service import VisionService
from phuthanhai.config import Settings
from phuthanhai.library.service import LibraryService
from tests.helpers import isolated_settings
from tests.media_fixtures import get


def make_manifest(root: Path) -> dict:
    frames = []
    for index, (scene, pts, kind) in enumerate(
        [
            ("S0001", 100, "anchor_start"), ("S0001", 900, "gap"), ("S0001", 1700, "anchor_mid"),
            ("S0001", 2600, "anchor_end"), ("S0002", 3200, "anchor_start"),
            ("S0002", 4200, "refine"), ("S0002", 5200, "anchor_end"), ("S0003", 6100, "anchor_mid"),
        ],
        start=1,
    ):
        path = root / f"F{index:04d}.jpg"
        # Frames 1 and 2 are deliberately near-identical (dedup case).
        colour = (30, 120, 200) if index in (1, 2) else (index * 25 % 255, 80, 60)
        Image.new("RGB", (64, 48), colour).save(path)
        frames.append({
            "frame_id": f"F{index:04d}", "scene_id": scene, "normalized_pts_ms": pts,
            "kind": kind, "reason": kind, "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        })
    return {
        "segments": [
            {"scene_id": "S0001", "start_ms": 0, "end_ms": 3000, "max_motion": 3, "blur_ratio": 0, "dark_ratio": 0},
            {"scene_id": "S0002", "start_ms": 3000, "end_ms": 6000, "max_motion": 14, "blur_ratio": 0, "dark_ratio": 0},
            {"scene_id": "S0003", "start_ms": 6000, "end_ms": 7000, "max_motion": 2, "blur_ratio": 0, "dark_ratio": 0},
        ],
        "frames": frames,
    }


class SelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.manifest = make_manifest(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_full_keeps_every_verified_frame(self) -> None:
        selection = build_selection(self.manifest, "full")
        self.assertEqual(selection["selected_frames"], 8)
        self.assertEqual(selection["frame_ids"], [f["frame_id"] for f in self.manifest["frames"]])
        self.assertFalse(selection["partial"])

    def test_smart_budget_and_scene_coverage(self) -> None:
        selection = build_selection(self.manifest, "smart")
        self.assertLessEqual(selection["selected_frames"], 6)
        self.assertGreaterEqual(len(selection["scenes_covered"]), 3)
        self.assertEqual(selection["scenes_missing"], [])
        self.assertTrue(selection["partial"])
        for item in selection["selected"]:
            self.assertIn(item["frame_id"], {f["frame_id"] for f in self.manifest["frames"]})
            self.assertTrue(item["reason"])

    def test_timestamps_are_not_invented(self) -> None:
        selection = build_selection(self.manifest, "balanced")
        by_id = {f["frame_id"]: f for f in self.manifest["frames"]}
        for item in selection["selected"]:
            self.assertEqual(item["normalized_pts_ms"], by_id[item["frame_id"]]["normalized_pts_ms"])

    def test_near_duplicate_is_dropped_when_safe(self) -> None:
        selection = build_selection(self.manifest, "balanced")
        ids = set(selection["frame_ids"])
        self.assertFalse({"F0001", "F0002"} <= ids)

    def test_user_range_is_prioritised(self) -> None:
        selection = build_selection(self.manifest, "smart", user_ranges=[[5000, 5300]])
        reasons = {item["frame_id"]: item["reason"] for item in selection["selected"]}
        self.assertTrue(any(item == "user_range" for item in reasons.values()))

    def test_dropped_frames_have_reasons(self) -> None:
        selection = build_selection(self.manifest, "smart")
        self.assertTrue(selection["dropped"])
        for item in selection["dropped"]:
            self.assertEqual(item["reason"], "duplicate_or_budget")


class FrameCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.cache = FrameResultCache(self.root / "frames")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def key(self, frame_sha="a" * 64, prompt="p1", model_name="m1"):
        return FrameResultCache.key(
            source_sha="b" * 64, frame_sha=frame_sha,
            model={"name": model_name}, runtime={"mlx": "0.32.4"},
            prompt_version=prompt, max_tokens=320, temperature=0.0,
        )

    def observation(self):
        return {
            "description": "A table with flowers.", "objects": ["table"], "actions": [],
            "camera": "fixed", "lighting": "even", "visible_text": [], "uncertainty": [],
        }

    def test_roundtrip_and_prompt_model_isolation(self) -> None:
        self.cache.put(self.key(), self.observation(), method="single_frame")
        self.assertIsNotNone(self.cache.get(self.key()))
        self.assertIsNone(self.cache.get(self.key(prompt="p2")))
        self.assertIsNone(self.cache.get(self.key(model_name="m2")))

    def test_corrupt_cache_is_deleted(self) -> None:
        key = self.key()
        path = self.root / "frames" / key[:2] / f"{key}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{broken", encoding="utf-8")
        self.assertIsNone(self.cache.get(key))
        self.assertFalse(path.exists())

    def test_batch_size_is_not_part_of_frame_key(self) -> None:
        base = dict(source_sha="b" * 64, frame_sha="a" * 64, model={"name": "m"},
                    runtime={"mlx": "x"}, prompt_version="p", max_tokens=320, temperature=0.0)
        self.assertEqual(FrameResultCache.key(**base), FrameResultCache.key(**base))


class VisionSelectionIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.footage = self.root / "footage"
        self.footage.mkdir()
        shutil.copy(get("cuts"), self.footage / "clip.mp4")
        self._env = isolated_settings(self.root / "data")
        self.settings = self._env.__enter__()
        self.service = LibraryService(self.settings)
        project = self.service.create_project("S05.4", str(self.footage))
        self.service.scan(project["project_id"])
        self.asset = self.service.list_assets(project["project_id"], limit=5)["items"][0]
        self.service.pipeline.run(self.asset, Path(self.asset["path"]), profile="quick")
        self.asset = self.service.get_asset(self.asset["asset_id"])
        model = self.root / "model"
        model.mkdir()
        (model / "config.json").write_text("{}", encoding="utf-8")
        (model / "model.safetensors").write_bytes(b"stub")
        self.model = model

    def tearDown(self) -> None:
        self.service.close()
        self._env.__exit__(None, None, None)
        self._tmp.cleanup()

    def vision(self, batch_size=8):
        return VisionService(
            self.service.db, self.settings,
            runtime_python=Path(sys.executable), worker_module="tests.fake_vlm_worker",
            model_path=self.model, request_timeout_s=10, batch_size=batch_size,
            require_governor=False,
        )

    def test_smart_selects_fewer_frames_and_marks_partial_evidence(self) -> None:
        with mock.patch.dict(os.environ, {"FAKE_VLM_MODE": "ok"}):
            outcome = self.vision().analyze(self.asset, Path(self.asset["path"]), selection_mode="smart")
        self.assertEqual(outcome.selection_mode, "smart")
        self.assertGreater(outcome.sampling_frames, outcome.selected_frames)
        self.assertEqual(outcome.inferences, outcome.selected_frames)
        self.assertTrue(outcome.warnings)
        evidence = self.vision().load_evidence(self.asset["asset_id"])
        self.assertTrue(evidence["visual_coverage"]["partial"])
        self.assertEqual(evidence["visual_coverage"]["selected"], outcome.selected_frames)

    def test_frame_cache_hits_survive_batch_size_change(self) -> None:
        with mock.patch.dict(os.environ, {"FAKE_VLM_MODE": "ok"}):
            first = self.vision(batch_size=8).analyze(self.asset, Path(self.asset["path"]), selection_mode="smart")
        service = self.vision(batch_size=2)
        with mock.patch.object(service, "_run_worker", side_effect=AssertionError("must use frame cache")):
            second = service.analyze(self.asset, Path(self.asset["path"]), selection_mode="smart")
        self.assertEqual(second.inferences, 0)
        self.assertEqual(second.frame_cache_hits, first.selected_frames)
        self.assertEqual(second.selected_frames, first.selected_frames)


if __name__ == "__main__":
    unittest.main()
