"""S03 vision-service tests with a fake worker for error paths.

Real Qwen3-VL inference is exercised by tools/run_s03_gate.py; this file never
claims to replace that.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from phuthanhai.analyzer.vision_service import VisionError, VisionService
from phuthanhai.analyzer.evidence import validate_evidence
from phuthanhai.config import Settings
from phuthanhai.library.service import LibraryService
from phuthanhai.runtime.scheduler import HEAVY_SLOT, PROCESSES
from tests.helpers import isolated_settings
from tests.media_fixtures import get


class BaseVisionCase(unittest.TestCase):
    fixture = "cuts"
    frames_expected_min = 1

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
        project = self.service.create_project("s03", str(self.footage))
        self.service.scan(project["project_id"])
        self.asset = self.service.list_assets(project["project_id"], limit=10)["items"][0]
        outcome = self.service.pipeline.run(self.asset, self.source, profile="quick")
        self.asset = self.service.get_asset(self.asset["asset_id"])
        self.assertTrue(outcome.coverage_status)
        self.model = self.root / "model"
        self.model.mkdir()
        (self.model / "config.json").write_text("{}", encoding="utf-8")
        (self.model / "model.safetensors").write_bytes(b"stub")
        self.vision = VisionService(
            self.service.db,
            self.settings,
            runtime_python=Path(sys.executable),
            worker_module="tests.fake_vlm_worker",
            model_path=self.model,
            request_timeout_s=10,
            batch_size=8,
            require_governor=False,
        )
        self.service.vision = self.vision

    def tearDown(self) -> None:
        self.service.close()
        self._env.__exit__(None, None, None)
        self._tmp.cleanup()

    def analyze(self, mode: str = "ok", **kwargs):
        with mock.patch.dict(os.environ, {"FAKE_VLM_MODE": mode}):
            return self.vision.analyze(self.asset, self.source, **kwargs)

    def analyze_with(self, vision, mode: str, **kwargs):
        with mock.patch.dict(os.environ, {"FAKE_VLM_MODE": mode}):
            return vision.analyze(self.asset, self.source, **kwargs)


class SuccessTests(BaseVisionCase):
    def test_evidence_is_valid_and_observed(self) -> None:
        outcome = self.analyze("ok")
        self.assertEqual(outcome.status, "complete")
        self.assertEqual(outcome.frames_unknown, 0)
        evidence = self.vision.load_evidence(self.asset["asset_id"])
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence["kind"], "phuthanhai.visual_evidence")
        self.assertTrue(all(frame["interpretation_level"] == "observed" for frame in evidence["frames"]))
        self.assertEqual(evidence["coverage_status"], "sufficient")
        self.assertFalse(evidence["is_every_frame_analyzed"])
        for frame in evidence["frames"]:
            self.assertNotIn("confidence", frame)
            self.assertEqual(frame["source_sha256"], evidence["source_sha256"])

    def test_rerun_uses_inference_checkpoint(self) -> None:
        first = self.analyze("ok")
        second = self.analyze("crash")  # would fail if it re-ran the worker
        self.assertEqual(first.analysis_id, second.analysis_id)
        self.assertEqual(second.frames_observed, first.frames_total)
        evidence = self.vision.load_evidence(self.asset["asset_id"])
        validate_evidence(evidence, evidence["frames"])

    def test_injection_text_is_stored_as_data(self) -> None:
        outcome = self.analyze("injection")
        self.assertEqual(outcome.status, "complete")
        evidence = self.vision.load_evidence(self.asset["asset_id"])
        texts = [text for frame in evidence["frames"] for text in frame["observation"]["visible_text"]]
        self.assertTrue(any("IGNORE ALL PREVIOUS" in text for text in texts))
        self.assertTrue(self.source.is_file())


class FailurePathTests(BaseVisionCase):
    fixture = "cfr"

    def test_invalid_model_json_becomes_unknown_not_fabricated(self) -> None:
        outcome = self.analyze("invalid_json")
        self.assertEqual(outcome.status, "partial")
        self.assertGreater(outcome.frames_unknown, 0)
        evidence = self.vision.load_evidence(self.asset["asset_id"])
        unknown = [f for f in evidence["frames"] if f["interpretation_level"] == "unknown"]
        self.assertTrue(unknown)
        self.assertIsNone(unknown[0]["observation"])
        self.assertTrue(unknown[0]["uncertainty"])

    def test_fabricated_frame_id_is_rejected(self) -> None:
        outcome = self.analyze("fabricated_frame")
        self.assertEqual(outcome.status, "partial")
        evidence = self.vision.load_evidence(self.asset["asset_id"])
        ids = [frame["frame_id"] for frame in evidence["frames"]]
        self.assertNotIn("F9999", ids)
        self.assertTrue(all(frame["interpretation_level"] == "unknown" for frame in evidence["frames"]))

    def test_worker_crash_becomes_unknown(self) -> None:
        outcome = self.analyze("crash")
        self.assertEqual(outcome.status, "partial")
        self.assertEqual(outcome.frames_observed, 0)

    def test_worker_sigkill_is_treated_as_oom_and_retried_safely(self) -> None:
        outcome = self.analyze("sigkill")
        self.assertEqual(outcome.status, "partial")
        self.assertEqual(outcome.frames_observed, 0)
        self.assertEqual(PROCESSES.active_count, 0)
        self.assertFalse(HEAVY_SLOT.busy)

    def test_worker_timeout_is_bounded(self) -> None:
        vision = VisionService(
            self.service.db,
            self.settings,
            runtime_python=Path(sys.executable),
            worker_module="tests.fake_vlm_worker",
            model_path=self.model,
            request_timeout_s=0.6,
            batch_size=8,
            require_governor=False,
        )
        self.service.vision = vision
        started = time.monotonic()
        outcome = self.analyze_with(vision, "hang")
        self.assertLess(time.monotonic() - started, 15)
        self.assertEqual(outcome.status, "partial")
        self.assertEqual(PROCESSES.active_count, 0)

    def test_stdout_noise_is_a_protocol_error(self) -> None:
        outcome = self.analyze("stdout_noise")
        self.assertEqual(outcome.status, "partial")

    def test_stderr_noise_is_harmless(self) -> None:
        outcome = self.analyze("ok")
        self.assertEqual(outcome.status, "complete")

    def test_oversized_request_is_rejected(self) -> None:
        with self.assertRaises(VisionError) as ctx:
            self.vision._run_worker({"x": "y" * 600_000}, cancel=None)
        self.assertEqual(ctx.exception.code, "WORKER_REQUEST_TOO_LARGE")

    def test_missing_model_snapshot_is_reported(self) -> None:
        empty = self.root / "empty_model"
        empty.mkdir()
        vision = VisionService(
            self.service.db,
            self.settings,
            runtime_python=Path(sys.executable),
            worker_module="tests.fake_vlm_worker",
            model_path=empty,
        )
        with self.assertRaises(VisionError) as ctx:
            vision.analyze(self.asset, self.source)
        self.assertEqual(ctx.exception.code, "MODEL_SNAPSHOT_MISSING")

    def test_corrupt_frame_cache_is_repaired(self) -> None:
        manifest = self.vision.sampling.load_manifest(
            self.asset["asset_id"], self.asset["sampling_fingerprint"]
        )
        Path(manifest["frames"][0]["path"]).write_bytes(b"broken jpeg")
        outcome = self.analyze("ok")
        self.assertEqual(outcome.status, "complete")
        self.assertEqual(outcome.frames_unknown, 0)

    def test_coverage_insufficient_is_propagated(self) -> None:
        manifest = self.vision.sampling.load_manifest(
            self.asset["asset_id"], self.asset["sampling_fingerprint"]
        )
        # Simulate an insufficient S02 manifest without touching the sampling cache.
        original = self.vision.sampling.load_manifest

        def fake_load(asset_id, fingerprint):
            data = original(asset_id, fingerprint)
            if data is None:
                return None
            data = dict(data)
            data["coverage_status"] = "insufficient"
            data["warnings"] = ["observed max gap over target"]
            data["coverage"] = {**data.get("coverage", {}), "status": "insufficient"}
            return data

        with mock.patch.object(self.vision.sampling, "load_manifest", side_effect=fake_load):
            outcome = self.analyze("ok")
        self.assertEqual(outcome.coverage_status, "insufficient")
        evidence = self.vision.load_evidence(self.asset["asset_id"])
        self.assertEqual(evidence["coverage_status"], "insufficient")
        self.assertTrue(any("coverage" in warning for warning in evidence["warnings"]))


class CancelAndResumeTests(BaseVisionCase):
    fixture = "cfr"

    def test_cancel_stops_worker_and_keeps_evidence(self) -> None:
        cancel = threading.Event()
        result: dict = {}

        def run() -> None:
            with mock.patch.dict(os.environ, {"FAKE_VLM_MODE": "slow"}):
                result["outcome"] = self.vision.analyze(
                    self.asset, self.source, cancel=cancel
                )

        thread = threading.Thread(target=run)
        thread.start()
        time.sleep(0.4)
        cancel.set()
        thread.join(timeout=30)
        self.assertFalse(thread.is_alive())
        outcome = result["outcome"]
        self.assertEqual(outcome.status, "cancelled")
        evidence = self.vision.load_evidence(self.asset["asset_id"])
        self.assertIsNotNone(evidence)
        self.assertTrue(all(frame["interpretation_level"] == "unknown" for frame in evidence["frames"]))
        self.assertEqual(PROCESSES.active_count, 0)
        self.assertFalse(HEAVY_SLOT.busy)

    def test_crash_after_one_frame_resumes_without_duplicates(self) -> None:
        vision = VisionService(
            self.service.db,
            self.settings,
            runtime_python=Path(sys.executable),
            worker_module="tests.fake_vlm_worker",
            model_path=self.model,
            request_timeout_s=10,
            batch_size=1,
            require_governor=False,
        )
        first = self.analyze_with(vision, "crash_after_first")
        self.assertIn(first.status, {"partial", "cancelled"})
        committed = first.frames_observed
        second = self.analyze_with(vision, "ok")
        self.assertEqual(second.status, "complete")
        evidence = vision.load_evidence(self.asset["asset_id"])
        ids = [frame["frame_id"] for frame in evidence["frames"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(second.frames_total, len(ids))

    def test_source_changed_with_same_size_and_mtime_is_rejected(self) -> None:
        import subprocess

        def colour(path: Path, name: str) -> None:
            subprocess.run(
                [
                    "ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi",
                    "-i", f"color=c={name}:size=320x240:rate=25:duration=1",
                    "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast", str(path),
                ],
                check=True,
            )

        red = self.root / "red.mp4"
        colour(red, "red")
        shutil.copy(red, self.source)
        self.service.scan(self.asset["project_id"])
        self.asset = self.service.get_asset(self.asset["asset_id"])
        self.service.pipeline.run(self.asset, self.source, profile="quick")
        self.asset = self.service.get_asset(self.asset["asset_id"])

        original_size = self.source.stat().st_size
        original_mtime = self.source.stat().st_mtime_ns
        blue = self.root / "blue.mp4"
        colour(blue, "blue")
        self.assertEqual(blue.stat().st_size, original_size, "colour fixtures must share size")
        shutil.copy(blue, self.source)
        os.utime(self.source, ns=(original_mtime, original_mtime))
        self.assertEqual(self.source.stat().st_mtime_ns, original_mtime)

        with self.assertRaises(VisionError) as ctx:
            self.analyze("ok")
        self.assertEqual(ctx.exception.code, "SOURCE_CHANGED")

if __name__ == "__main__":
    unittest.main()
