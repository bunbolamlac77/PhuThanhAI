"""S02 pipeline tests: cache, resume, invalidation, source mutation, disk full, cancel."""

from __future__ import annotations

import errno
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from phuthanhai.analyzer.sampling_pipeline import SamplingError
from phuthanhai.config import Settings
from phuthanhai.library.service import LibraryService
from tests.helpers import isolated_settings
from tests.media_fixtures import get


class PipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.footage = self.root / "footage"
        self.footage.mkdir()
        for fixture in ("cuts", "cfr", "rotation", "dark", "long_nocut"):
            shutil.copy(get(fixture), self.footage / f"{fixture}.mp4")
        self._env = isolated_settings(self.root / "data")
        self.settings = self._env.__enter__()
        self.service = LibraryService(self.settings)
        project = self.service.create_project("s02", str(self.footage))
        self.service.scan(project["project_id"])
        self.assets = {
            item["filename"]: item for item in self.service.list_assets(project["project_id"], limit=100)["items"]
        }

    def tearDown(self) -> None:
        self.service.close()
        self._env.__exit__(None, None, None)
        self._tmp.cleanup()

    def _run(self, filename: str, profile: str = "detailed", **kwargs):
        asset = self.assets[filename]
        return self.service.pipeline.run(asset, Path(asset["path"]), profile=profile, **kwargs)

    def test_complete_run_records_verified_frames(self) -> None:
        outcome = self._run("cuts.mp4")
        self.assertEqual(outcome.status, "complete")
        self.assertGreater(len(outcome.frames), 0)
        ids = [frame["frame_id"] for frame in outcome.frames]
        self.assertEqual(len(ids), len(set(ids)))
        times = [frame["normalized_pts_ms"] for frame in outcome.frames]
        self.assertEqual(times, sorted(times))
        for frame in outcome.frames:
            self.assertFalse(frame["pts_mismatch"])
            self.assertEqual(frame["sha256"], frame["sha256"].lower())
            self.assertEqual(len(frame["sha256"]), 64)
        self.assertLessEqual(outcome.observed_max_gap_ms, outcome.plan.max_gap_ms)
        asset = self.service.get_asset(self.assets["cuts.mp4"]["asset_id"])
        self.assertEqual(asset["sampling_status"], "complete")

    def test_rotation_asset_produces_portrait_frames(self) -> None:
        outcome = self._run("rotation.mp4")
        self.assertTrue(all(frame["height"] > frame["width"] for frame in outcome.frames))

    def test_rerun_is_served_from_cache(self) -> None:
        first = self._run("cuts.mp4")
        second = self._run("cuts.mp4")
        self.assertEqual(second.extracted, 0)
        self.assertEqual(second.cache_hits, len(first.frames))
        self.assertEqual(
            [f["frame_id"] for f in second.frames], [f["frame_id"] for f in first.frames]
        )

    def test_missing_frame_image_is_re_extracted_once(self) -> None:
        first = self._run("cuts.mp4")
        Path(first.frames[2]["path"]).unlink()
        second = self._run("cuts.mp4")
        self.assertEqual(second.extracted, 1)
        self.assertEqual(second.cache_hits, len(first.frames) - 1)

    def test_note_edit_does_not_invalidate_frame_cache(self) -> None:
        first = self._run("cuts.mp4")
        project_id = self.assets["cuts.mp4"]["project_id"]
        self.service.create_annotation(
            project_id,
            {"scope": "clip", "asset_id": self.assets["cuts.mp4"]["asset_id"], "text": "Lên phà lúc 5 giờ sáng"},
        )
        second = self._run("cuts.mp4")
        self.assertEqual(second.extracted, 0)
        self.assertEqual(second.cache_hits, len(first.frames))

    def test_source_content_change_invalidates_cache(self) -> None:
        first = self._run("cuts.mp4")
        target = Path(self.assets["cuts.mp4"]["path"])
        shutil.copy(get("dark"), target)  # same path, different bytes
        second = self._run("cuts.mp4")
        self.assertNotEqual(second.fingerprint, first.fingerprint)
        self.assertEqual(second.extracted, len(second.frames))
        asset_root = self.settings.data.sampling / self.assets["cuts.mp4"]["asset_id"]
        self.assertEqual(len([p for p in asset_root.iterdir() if p.is_dir()]), 1)

    def test_mid_run_source_change_aborts(self) -> None:
        asset = self.assets["cuts.mp4"]
        real_stat = __import__("phuthanhai.analyzer.sampling_pipeline", fromlist=["_source_stat"])._source_stat
        initial = real_stat(Path(asset["path"]))
        sequence = [initial, initial, (initial[0] + 1, initial[1])]
        with mock.patch(
            "phuthanhai.analyzer.sampling_pipeline._source_stat", side_effect=sequence
        ):
            with self.assertRaises(SamplingError) as ctx:
                self._run("cuts.mp4")
        self.assertEqual(ctx.exception.code, "SOURCE_CHANGED")

    def test_disk_full_maps_to_storage_full(self) -> None:
        with mock.patch(
            "phuthanhai.analyzer.sampling_pipeline.extract_verified_frame",
            side_effect=OSError(errno.ENOSPC, "No space left on device"),
        ):
            with self.assertRaises(SamplingError) as ctx:
                self._run("cuts.mp4")
        self.assertEqual(ctx.exception.code, "STORAGE_FULL")

    def test_cancel_then_resume_has_no_duplicates(self) -> None:
        import threading

        cancel = threading.Event()

        def progress(payload):
            if payload.get("stage") == "extracting" and payload.get("done", 0) >= 2:
                cancel.set()

        first = self._run("cuts.mp4", cancel=cancel, progress=progress)
        self.assertEqual(first.status, "cancelled")
        resumed = self._run("cuts.mp4")
        self.assertEqual(resumed.status, "complete")
        ids = [frame["frame_id"] for frame in resumed.frames]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(resumed.extracted + resumed.cache_hits, len(resumed.frames))

    def test_corrupted_checkpoint_is_recovered(self) -> None:
        first = self._run("cuts.mp4")
        checkpoint = first.cache_dir / "checkpoint.json"
        checkpoint.write_text("{not json", encoding="utf-8")
        second = self._run("cuts.mp4")
        self.assertEqual(second.status, "complete")
        self.assertEqual(len(second.frames), len(first.frames))

    def test_corrupt_source_is_rejected(self) -> None:
        shutil.copy(get("corrupt"), self.footage / "broken.mp4")
        project_id = self.assets["cuts.mp4"]["project_id"]
        self.service.scan(project_id)
        broken = next(
            item
            for item in self.service.list_assets(project_id, q="broken", limit=5)["items"]
        )
        with self.assertRaises(SamplingError) as ctx:
            self.service.pipeline.run(broken, Path(broken["path"]), profile="quick")
        self.assertIn(ctx.exception.code, {"CODEC_UNSUPPORTED", "FFMPEG_FAILED"})


if __name__ == "__main__":
    unittest.main()
