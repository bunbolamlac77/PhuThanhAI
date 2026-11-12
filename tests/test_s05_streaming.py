"""S05.4R.1 streaming, per-frame durability and protocol limits."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from phuthanhai.analyzer.vision_service import VisionError, VisionService
from phuthanhai.library.service import LibraryService
from phuthanhai.runtime.scheduler import PROCESSES
from tests.helpers import isolated_settings
from tests.media_fixtures import get


class StreamingCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        footage = self.root / "footage"
        footage.mkdir()
        shutil.copy(get("cuts"), footage / "clip.mp4")
        self._env = isolated_settings(self.root / "data")
        self.settings = self._env.__enter__()
        self.service = LibraryService(self.settings)
        project = self.service.create_project("stream", str(footage))
        self.service.scan(project["project_id"])
        self.asset = self.service.list_assets(project["project_id"], limit=5)["items"][0]
        self.service.pipeline.run(self.asset, Path(self.asset["path"]), profile="detailed")
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

    def vision(self, batch_size=4):
        return VisionService(
            self.service.db, self.settings,
            runtime_python=Path(sys.executable), worker_module="tests.fake_vlm_worker",
            model_path=self.model, request_timeout_s=20, batch_size=batch_size,
            require_governor=False,
        )

    def analyze(self, mode: str, vision=None):
        with mock.patch.dict(os.environ, {"FAKE_VLM_MODE": mode}):
            return (vision or self.vision()).analyze(self.asset, Path(self.asset["path"]), selection_mode="full")


class StreamingProtocolTests(StreamingCase):
    def test_duplicate_sequence_is_not_accepted_as_evidence(self) -> None:
        outcome = self.analyze("duplicate_seq")
        self.assertIn(outcome.status, {"partial", "cancelled"})
        self.assertLess(outcome.frames_observed, outcome.frames_total)
        evidence = self.vision().load_evidence(self.asset["asset_id"])
        ids = [frame["frame_id"] for frame in evidence["frames"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_duplicate_frame_id_is_idempotent_or_rejected_without_duplicates(self) -> None:
        try:
            outcome = self.analyze("duplicate_frame")
        except VisionError as exc:
            self.assertEqual(exc.code, "WORKER_PROTOCOL_INVALID")
            return
        evidence = self.vision().load_evidence(self.asset["asset_id"])
        ids = [frame["frame_id"] for frame in evidence["frames"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_invalid_jsonl_is_not_committed(self) -> None:
        outcome = self.analyze("invalid_jsonl")
        self.assertIn(outcome.status, {"partial", "cancelled"})
        self.assertEqual(outcome.frames_observed, 0)

    def test_oversized_stdout_is_not_committed(self) -> None:
        outcome = self.analyze("oversized_line")
        self.assertIn(outcome.status, {"partial", "cancelled"})
        self.assertEqual(outcome.frames_observed, 0)

    def test_stderr_flood_does_not_deadlock(self) -> None:
        outcome = self.analyze("stderr_flood")
        self.assertIn(outcome.status, {"complete", "partial"})
        self.assertEqual(PROCESSES.active_count, 0)

    def test_valid_frame_then_error_keeps_committed_evidence(self) -> None:
        try:
            outcome = self.analyze("error_after_frame")
        except VisionError as exc:
            self.assertEqual(exc.code, "MODEL_INFERENCE_FAILED")
            evidence = self.vision().load_evidence(self.asset["asset_id"])
            self.assertIsNotNone(evidence)
            self.assertGreaterEqual(sum(1 for f in evidence["frames"] if f["interpretation_level"] == "observed"), 1)
            return
        self.assertGreaterEqual(outcome.frames_observed, 1)

    def test_multi_batch_kill_does_not_reinfer_committed_frames(self) -> None:
        vision = self.vision(batch_size=2)
        outcome = self.analyze("kill_after_two", vision)
        self.assertLessEqual(outcome.inferences, outcome.frames_total)
        evidence = vision.load_evidence(self.asset["asset_id"])
        ids = [frame["frame_id"] for frame in evidence["frames"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(outcome.frames_observed, sum(
            1 for frame in evidence["frames"] if frame["interpretation_level"] == "observed"
        ))

    def test_final_then_nonzero_exit_is_not_complete(self) -> None:
        outcome = self.analyze("final_then_exit7")
        self.assertEqual(outcome.status, "partial")
        self.assertIn("worker failed", " ".join(outcome.warnings))

    def test_invalid_final_job_identity_is_rejected(self) -> None:
        outcome = self.analyze("wrong_final_job")
        self.assertIn(outcome.status, {"partial", "cancelled"})
        self.assertEqual(outcome.frames_observed, 0)

    def test_missing_stream_identity_is_rejected_before_commit(self) -> None:
        outcome = self.analyze("missing_frame_identity")
        self.assertIn(outcome.status, {"partial", "cancelled"})
        self.assertEqual(outcome.frames_observed, 0)

    def test_wrong_stream_analysis_id_is_rejected_before_commit(self) -> None:
        outcome = self.analyze("wrong_frame_analysis")
        self.assertIn(outcome.status, {"partial", "cancelled"})
        self.assertEqual(outcome.frames_observed, 0)

    def test_long_line_without_newline_is_bounded(self) -> None:
        outcome = self.analyze("long_line")
        self.assertIn(outcome.status, {"partial", "cancelled"})
        self.assertEqual(outcome.frames_observed, 0)

    def test_worker_that_never_reads_stdin_times_out(self) -> None:
        vision = VisionService(
            self.service.db, self.settings,
            runtime_python=Path(sys.executable), worker_module="tests.fake_vlm_worker",
            model_path=self.model, request_timeout_s=2, batch_size=4, require_governor=False,
        )
        outcome = self.analyze("no_stdin_read", vision)
        self.assertIn(outcome.status, {"partial", "cancelled"})
        self.assertEqual(PROCESSES.active_count, 0)

    def test_worker_request_log_proves_no_committed_frame_is_reinferred(self) -> None:
        from tempfile import NamedTemporaryFile
        handle = NamedTemporaryFile(suffix=".jsonl", delete=False)
        handle.close()
        log = Path(handle.name)
        try:
            with mock.patch.dict(os.environ, {"FAKE_VLM_REQUESTS": str(log)}):
                first = self.analyze("kill_after_two", self.vision(batch_size=20))
                first_requests = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
                evidence = self.vision().load_evidence(self.asset["asset_id"])
                committed_before = {f["frame_id"] for f in evidence["frames"] if f["interpretation_level"] == "observed"}
                log.write_text("", encoding="utf-8")
                second = self.vision(batch_size=20).analyze(self.asset, Path(self.asset["path"]), selection_mode="full")
                resume_requests = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertGreaterEqual(len(first_requests), 1)
            timing = first.frame_timings[0]
            self.assertIn("received_ms", timing)
            self.assertIn("checkpoint_write_ms", timing)
            self.assertIn("committed_ms", timing)
            self.assertGreaterEqual(timing["committed_ms"], timing["received_ms"])
            self.assertIsNotNone(first.to_dict()["checkpoint_p95_ms"])
            for request in first_requests:
                self.assertIsInstance(request["frames"], list)
            for request in resume_requests:
                self.assertFalse(committed_before & set(request["frames"]), "resume resent a committed frame")
            self.assertIn(first.status, {"partial", "cancelled", "complete"})
            self.assertEqual(second.status, "complete")
        finally:
            log.unlink(missing_ok=True)

    def test_invalid_checkpoint_observation_is_reinferred_not_accepted(self) -> None:
        first = self.analyze("ok")
        checkpoint = first.cache_dir / "analysis_checkpoint.json"
        data = json.loads(checkpoint.read_text(encoding="utf-8"))
        frame_id = next(iter(data["results"]))
        data["results"][frame_id]["observation"] = {"not": "an observation"}
        checkpoint.write_text(json.dumps(data), encoding="utf-8")
        shutil.rmtree(self.settings.data.inference / "frames", ignore_errors=True)
        second = self.vision().analyze(self.asset, Path(self.asset["path"]), selection_mode="full")
        self.assertEqual(second.status, "complete")
        self.assertEqual(second.inferences, 1)
        evidence = self.vision().load_evidence(self.asset["asset_id"])
        self.assertTrue(all(frame["interpretation_level"] == "observed" for frame in evidence["frames"]))

    def test_kill_after_two_frames_resumes_without_reinference(self) -> None:
        first_vision = self.vision(batch_size=20)
        first = self.analyze("kill_after_two", first_vision)
        self.assertIn(first.status, {"partial", "cancelled"})
        evidence = first_vision.load_evidence(self.asset["asset_id"])
        self.assertIsNotNone(evidence)
        observed = [f for f in evidence["frames"] if f["interpretation_level"] == "observed"]
        self.assertGreaterEqual(len(observed), 2)
        committed_ids = {f["frame_id"] for f in observed}

        second_vision = self.vision(batch_size=20)
        outcome = second_vision.analyze(self.asset, Path(self.asset["path"]), selection_mode="full")
        final = second_vision.load_evidence(self.asset["asset_id"])
        ids = [frame["frame_id"] for frame in final["frames"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(committed_ids <= set(ids))
        self.assertGreaterEqual(outcome.frames_observed, len(observed))


if __name__ == "__main__":
    unittest.main()
