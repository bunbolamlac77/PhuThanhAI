"""Protocol v2 validation tests (no MLX imported in the test process)."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from phuthanhai.analyzer.vlm_worker import (
    MAX_REQUEST_BYTES,
    REQUEST_VERSION,
    WorkerError,
    parse_model_json,
    validate_request,
)

REPO = Path(__file__).resolve().parents[1]


class WorkerV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.model = self.root / "model"
        self.model.mkdir()
        (self.model / "config.json").write_text("{}", encoding="utf-8")
        (self.model / "model.safetensors").write_bytes(b"stub")
        self.images = self.root / "frames"
        self.images.mkdir()
        self.frames = []
        for index in (1, 2):
            path = self.images / f"F{index:04d}.jpg"
            Image.new("RGB", (64, 48), (index * 60, 40, 40)).save(path)
            self.frames.append(
                {
                    "frame_id": f"F{index:04d}",
                    "scene_id": "S0001",
                    "image_path": str(path),
                    "normalized_pts_ms": index * 400,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def payload(self, **overrides):
        payload = {
            "request_version": REQUEST_VERSION,
            "job_id": "job-1",
            "asset_id": "asset-1",
            "analysis_id": "analysis-1",
            "model_path": str(self.model),
            "allowed_image_root": str(self.images),
            "prompt": "Describe the frames.",
            "method": "sequential_frames",
            "frames": [dict(frame) for frame in self.frames],
            "max_tokens": 200,
            "temperature": 0.0,
        }
        payload.update(overrides)
        return payload

    def test_valid_sequence_is_normalised(self) -> None:
        request = validate_request(self.payload())
        self.assertEqual(request["method"], "sequential_frames")
        self.assertEqual([f["frame_id"] for f in request["frames"]], ["F0001", "F0002"])

    def test_single_frame_requires_one_frame(self) -> None:
        with self.assertRaises(WorkerError):
            validate_request(self.payload(method="single_frame"))

    def test_duplicate_frame_id_is_rejected(self) -> None:
        frames = [dict(self.frames[0]), dict(self.frames[0])]
        frames[1]["image_path"] = str(self.frames[1]["image_path"])
        with self.assertRaises(WorkerError):
            validate_request(self.payload(frames=frames))

    def test_invalid_frame_id_is_rejected(self) -> None:
        frames = [dict(self.frames[0])]
        frames[0]["frame_id"] = "frame-one"
        with self.assertRaises(WorkerError):
            validate_request(self.payload(method="single_frame", frames=frames))

    def test_frame_hash_mismatch_is_rejected(self) -> None:
        frames = [dict(self.frames[0])]
        frames[0]["sha256"] = "0" * 64
        with self.assertRaises(WorkerError) as ctx:
            validate_request(self.payload(method="single_frame", frames=frames))
        self.assertEqual(ctx.exception.code, "FRAME_HASH_MISMATCH")

    def test_image_outside_root_is_denied(self) -> None:
        outside = self.root / "outside.jpg"
        Image.new("RGB", (8, 8), (1, 2, 3)).save(outside)
        frames = [dict(self.frames[0])]
        frames[0]["image_path"] = str(outside)
        with self.assertRaises(WorkerError) as ctx:
            validate_request(self.payload(method="single_frame", frames=frames))
        self.assertEqual(ctx.exception.code, "SECURITY_DENIED")

    def test_extra_keys_in_observation_are_rejected(self) -> None:
        base = {
            "description": "x", "objects": [], "actions": [], "camera": None,
            "lighting": None, "visible_text": [], "uncertainty": [],
        }
        for extra in ({"frame_id": "F9999"}, {"timecode": "05:00"}, {"confidence": 0.99}):
            with self.assertRaises(WorkerError):
                parse_model_json(json.dumps({**base, **extra}))

    def test_oversized_message_is_rejected_before_model_import(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-m", "phuthanhai.analyzer.vlm_worker"],
            input=b"x" * (MAX_REQUEST_BYTES + 10),
            capture_output=True,
            env={"PYTHONPATH": str(REPO / "src"), "PATH": "/usr/bin:/bin"},
            timeout=60,
            check=False,
        )
        self.assertEqual(completed.returncode, 2)
        events = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
        self.assertEqual(events[-1]["code"], "WORKER_REQUEST_TOO_LARGE")


if __name__ == "__main__":
    unittest.main()
