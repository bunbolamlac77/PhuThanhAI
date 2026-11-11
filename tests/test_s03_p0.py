"""S03 P0 regression tests: cache integrity, verified identity, coverage semantics."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from phuthanhai.analyzer.sampling import FrameTarget
from phuthanhai.analyzer.sampling_pipeline import SamplingPipeline
from phuthanhai.config import Settings
from phuthanhai.library.service import LibraryService
from tests.helpers import isolated_settings
from tests.media_fixtures import get


def _color_video(path: Path, color: str) -> Path:
    import subprocess

    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-v", "error", "-y",
            "-f", "lavfi", "-i", f"color=c={color}:size=320x240:rate=25:duration=1",
            "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast", str(path),
        ],
        check=True,
    )
    return path


class BasePipelineCase(unittest.TestCase):
    fixture = "cuts"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.footage = self.root / "footage"
        self.footage.mkdir()
        self.source = self.footage / "clip.mp4"
        shutil.copy(get(self.fixture), self.source)
        self._env = isolated_settings(self.root / "data")
        self.settings = self._env.__enter__()
        self.service = LibraryService(self.settings)
        project = self.service.create_project("p0", str(self.footage))
        self.service.scan(project["project_id"])
        self.asset = self.service.list_assets(project["project_id"], limit=10)["items"][0]
        self.pipeline: SamplingPipeline = self.service.pipeline

    def tearDown(self) -> None:
        self.service.close()
        self._env.__exit__(None, None, None)
        self._tmp.cleanup()

    def run_pipeline(self, profile: str = "quick"):
        return self.pipeline.run(self.asset, self.source, profile=profile)


class CacheIntegrityTests(BasePipelineCase):
    def test_corrupted_jpeg_with_nonzero_size_is_re_extracted(self) -> None:
        first = self.run_pipeline()
        victim = Path(first.frames[0]["path"])
        victim.write_bytes(b"this is not a jpeg but it is not empty" * 8)
        self.assertGreater(victim.stat().st_size, 0)

        second = self.run_pipeline()
        self.assertEqual(second.extracted, 1)
        self.assertEqual(second.cache_hits, len(first.frames) - 1)

        manifest = self.pipeline.load_manifest(self.asset["asset_id"], second.fingerprint)
        record = next(frame for frame in manifest["frames"] if frame["frame_id"] == first.frames[0]["frame_id"])
        self.assertFalse(victim.is_symlink())
        from PIL import Image

        with Image.open(victim) as image:
            self.assertEqual((image.width, image.height), (record["width"], record["height"]))
        self.assertEqual(record["sha256"], __import__("hashlib").sha256(victim.read_bytes()).hexdigest())

    def test_symlinked_frame_is_rejected_and_regular_file_restored(self) -> None:
        first = self.run_pipeline()
        victim = Path(first.frames[0]["path"])
        outside = self.root / "outside.jpg"
        shutil.copy(victim, outside)
        victim.unlink()
        victim.symlink_to(outside)

        second = self.run_pipeline()
        self.assertEqual(second.extracted, 1)
        self.assertFalse(victim.is_symlink())

    def test_record_pointing_outside_cache_is_rejected(self) -> None:
        outcome = self.run_pipeline()
        record = dict(outcome.frames[0])
        outside = self.root / "outside.jpg"
        shutil.copy(record["path"], outside)
        record["path"] = str(outside)
        target = next(t for t in outcome.plan.targets if t.frame_id == record["frame_id"])
        reusable = self.pipeline._frame_is_reusable(
            record,
            target,
            outcome.cache_dir / "frames",
            max_edge=record["max_edge"],
            quality=record["quality"],
        )
        self.assertFalse(reusable)

    def test_sha_mismatch_with_valid_image_is_rejected(self) -> None:
        outcome = self.run_pipeline()
        record = dict(outcome.frames[0])
        record["sha256"] = "0" * 64
        target = next(t for t in outcome.plan.targets if t.frame_id == record["frame_id"])
        self.assertFalse(
            self.pipeline._frame_is_reusable(
                record,
                target,
                outcome.cache_dir / "frames",
                max_edge=record["max_edge"],
                quality=record["quality"],
            )
        )


class SourceIdentityTests(unittest.TestCase):
    def test_same_size_and_mtime_but_different_bytes_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            footage = root / "footage"
            footage.mkdir()
            source = footage / "clip.mp4"
            _color_video(source, "red")
            original_size = source.stat().st_size
            original_mtime = source.stat().st_mtime_ns

            with isolated_settings(root / "data") as settings:
                service = LibraryService(settings)
                try:
                    project = service.create_project("identity", str(footage))
                    service.scan(project["project_id"])
                    asset = service.list_assets(project["project_id"], limit=5)["items"][0]
                    first = service.pipeline.run(asset, source, profile="quick")
                    stored_sha = service.get_asset(asset["asset_id"])["sampling_fingerprint"]
                    self.assertTrue(stored_sha)

                    temp = root / "blue.mp4"
                    _color_video(temp, "blue")
                    self.assertEqual(temp.stat().st_size, original_size, "fixtures must share byte size")
                    shutil.copy(temp, source)
                    os.utime(source, ns=(original_mtime, original_mtime))
                    self.assertEqual(source.stat().st_size, original_size)
                    self.assertEqual(source.stat().st_mtime_ns, original_mtime)

                    refreshed = service.get_asset(asset["asset_id"])
                    second = service.pipeline.run(refreshed, source, profile="quick")
                    self.assertNotEqual(first.fingerprint, second.fingerprint)
                    self.assertEqual(second.extracted, len(second.frames))
                finally:
                    service.close()


class CoverageSemanticsTests(BasePipelineCase):
    def test_execution_complete_does_not_imply_sufficient_coverage(self) -> None:
        with mock.patch(
            "phuthanhai.analyzer.sampling_pipeline.observed_max_gap", return_value=9_999_999
        ):
            outcome = self.run_pipeline()
        self.assertEqual(outcome.status, "complete")
        self.assertEqual(outcome.execution_status, "complete")
        self.assertEqual(outcome.coverage_status, "insufficient")
        self.assertTrue(outcome.warnings)

        manifest = self.pipeline.load_manifest(self.asset["asset_id"], outcome.fingerprint)
        self.assertEqual(manifest["execution_status"], "complete")
        self.assertEqual(manifest["coverage_status"], "insufficient")
        self.assertIn("observed_gap_over_target", manifest["coverage"]["reasons"])

        asset = self.service.get_asset(self.asset["asset_id"])
        self.assertEqual(asset["sampling_status"], "complete")
        self.assertEqual(asset["sampling_coverage"], "insufficient")

        summary = self.service.sampling_summary(self.asset["asset_id"])
        self.assertEqual(summary["coverage_status"], "insufficient")
        self.assertEqual(summary["manifest"]["coverage_status"], "insufficient")

    def test_normal_run_reports_sufficient_coverage(self) -> None:
        outcome = self.run_pipeline()
        self.assertEqual(outcome.coverage_status, "sufficient")
        self.assertEqual(outcome.execution_status, "complete")
        self.assertEqual(outcome.warnings, [])


if __name__ == "__main__":
    unittest.main()
