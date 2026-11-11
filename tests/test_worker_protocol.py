"""VLM worker protocol tests (no model loaded, no MLX imported in this process)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from phuthanhai.analyzer.vlm_worker import (
    REQUEST_VERSION,
    VISUAL_PROMPT,
    WorkerError,
    parse_model_json,
    validate_request,
)
from phuthanhai.config import offline_environment


class RequestValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.model = self.root / "model"
        self.model.mkdir()
        (self.model / "config.json").write_text("{}", encoding="utf-8")
        (self.model / "model.safetensors").write_bytes(b"stub")
        self.images = self.root / "images"
        self.images.mkdir()
        self.image = self.images / "frame.jpg"
        self.image.write_bytes(b"stub")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _payload(self, **overrides: object) -> dict:
        payload = {
            "request_version": REQUEST_VERSION,
            "job_id": "job-1",
            "model_path": str(self.model),
            "image_path": str(self.image),
            "allowed_image_root": str(self.images),
            "prompt": VISUAL_PROMPT,
            "max_tokens": 320,
            "temperature": 0.0,
        }
        payload.update(overrides)
        return payload

    def test_valid_request_is_normalised(self) -> None:
        request = validate_request(self._payload())
        self.assertEqual(request["job_id"], "job-1")
        self.assertEqual(request["max_tokens"], 320)
        self.assertEqual(request["temperature"], 0.0)

    def test_wrong_request_version_is_rejected(self) -> None:
        with self.assertRaises(WorkerError) as ctx:
            validate_request(self._payload(request_version="0.9.0"))
        self.assertEqual(ctx.exception.code, "WORKER_REQUEST_INVALID")

    def test_image_outside_allowed_root_is_denied(self) -> None:
        outside = self.root / "outside.jpg"
        outside.write_bytes(b"x")
        with self.assertRaises(WorkerError) as ctx:
            validate_request(self._payload(image_path=str(outside)))
        self.assertEqual(ctx.exception.code, "SECURITY_DENIED")

    def test_missing_model_snapshot_is_reported(self) -> None:
        with self.assertRaises(WorkerError) as ctx:
            validate_request(self._payload(model_path=str(self.root / "absent")))
        self.assertEqual(ctx.exception.code, "MODEL_SNAPSHOT_MISSING")

    def test_missing_model_artifact_is_reported(self) -> None:
        (self.model / "model.safetensors").unlink()
        with self.assertRaises(WorkerError) as ctx:
            validate_request(self._payload())
        self.assertEqual(ctx.exception.code, "MODEL_SNAPSHOT_MISSING")

    def test_temperature_bounds(self) -> None:
        with self.assertRaises(WorkerError):
            validate_request(self._payload(temperature=3.0))

    def test_token_bounds(self) -> None:
        with self.assertRaises(WorkerError):
            validate_request(self._payload(max_tokens=0))

    def test_oversized_prompt_is_rejected(self) -> None:
        with self.assertRaises(WorkerError):
            validate_request(self._payload(prompt="x" * 9000))


class ModelOutputParsingTests(unittest.TestCase):
    VALID = {
        "description": "Bến sông có phương tiện và vài người đứng gần bờ.",
        "objects": ["phương tiện", "người"],
        "actions": [],
        "camera": "góc rộng",
        "lighting": "ánh sáng yếu",
        "visible_text": [],
        "uncertainty": ["Không xác định được thời điểm quay"],
    }

    def test_valid_json_is_parsed(self) -> None:
        parsed = parse_model_json(json.dumps(self.VALID))
        self.assertEqual(parsed["objects"], ["phương tiện", "người"])

    def test_fenced_json_is_parsed(self) -> None:
        text = "```json\n" + json.dumps(self.VALID) + "\n```"
        self.assertEqual(parse_model_json(text)["camera"], "góc rộng")

    def test_json_embedded_in_prose_is_extracted(self) -> None:
        text = "Here is the observation: " + json.dumps(self.VALID) + " Done."
        self.assertEqual(parse_model_json(text)["description"], self.VALID["description"])

    def test_missing_key_is_rejected(self) -> None:
        broken = dict(self.VALID)
        broken.pop("uncertainty")
        with self.assertRaises(WorkerError) as ctx:
            parse_model_json(json.dumps(broken))
        self.assertEqual(ctx.exception.code, "MODEL_OUTPUT_INVALID")

    def test_extra_key_is_rejected(self) -> None:
        with self.assertRaises(WorkerError):
            parse_model_json(json.dumps(dict(self.VALID, confidence=0.97)))

    def test_no_json_is_rejected(self) -> None:
        with self.assertRaises(WorkerError):
            parse_model_json("I cannot see anything.")

    def test_wrong_type_is_rejected(self) -> None:
        with self.assertRaises(WorkerError):
            parse_model_json(json.dumps(dict(self.VALID, objects="người")))


class WorkerProcessProtocolTests(unittest.TestCase):
    def _run(self, payload: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "phuthanhai.analyzer.vlm_worker"],
            input=payload,
            capture_output=True,
            text=True,
            env=offline_environment(),
            timeout=60,
            check=False,
        )

    def test_invalid_request_exits_nonzero_with_structured_event(self) -> None:
        completed = self._run('{"request_version": "0.0.1"}')
        self.assertEqual(completed.returncode, 2)
        events = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "error")
        self.assertEqual(events[0]["code"], "WORKER_REQUEST_INVALID")
        # Diagnostics belong on stderr, never on the protocol stream.
        self.assertIn("WORKER_REQUEST_INVALID", completed.stderr)

    def test_empty_stdin_exits_nonzero(self) -> None:
        completed = self._run("")
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stdout.strip(), "")

    def test_non_json_stdin_exits_nonzero(self) -> None:
        completed = self._run("not json")
        self.assertEqual(completed.returncode, 2)
        events = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
        self.assertEqual(events[0]["code"], "WORKER_REQUEST_INVALID")

    def test_worker_module_does_not_import_mlx_at_module_load(self) -> None:
        # If the module imported MLX eagerly this would fail in the app venv.
        completed = subprocess.run(
            [sys.executable, "-c", "import sys, phuthanhai.analyzer.vlm_worker as w; print('mlx_vlm' in sys.modules)"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            env=offline_environment(),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "False")


if __name__ == "__main__":
    unittest.main()
