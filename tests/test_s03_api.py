"""S03 API integration: analysis jobs, evidence, cancel, notes during inference."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from phuthanhai.analyzer.vision_service import VisionService
from phuthanhai.api.app import create_app
from tests.helpers import isolated_settings
from tests.media_fixtures import get

PORT = 19043
CAP = "s03-cap"
HEADERS = {"X-PhuThanhAI-Capability": CAP}


class AnalysisApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.footage = self.root / "footage"
        self.footage.mkdir()
        shutil.copy(get("cuts"), self.footage / "cuts.mp4")
        shutil.copy(get("cfr"), self.footage / "cfr.mp4")
        self._env = isolated_settings(self.root / "data")
        self.settings = self._env.__enter__()
        app = create_app(self.settings, port=PORT, capability=CAP)
        self.client = TestClient(app, base_url=f"http://127.0.0.1:{PORT}")
        self.client.__enter__()
        self.library = app.state.library
        project = self.client.post(
            "/api/v1/projects", json={"name": "S03", "root_path": str(self.footage)}, headers=HEADERS
        ).json()
        self.project_id = project["project_id"]
        self.client.post(f"/api/v1/projects/{self.project_id}/assets:scan", json={}, headers=HEADERS)
        items = self.client.get("/api/v1/assets", params={"project_id": self.project_id}).json()["items"]
        self.cuts = next(item for item in items if item["filename"] == "cuts.mp4")
        self.cfr = next(item for item in items if item["filename"] == "cfr.mp4")

        model = self.root / "model"
        model.mkdir()
        (model / "config.json").write_text("{}", encoding="utf-8")
        (model / "model.safetensors").write_bytes(b"stub")
        self.model = model
        self.install_fake_vision()

    def install_fake_vision(self, timeout: float = 10) -> None:
        self.library.vision = VisionService(
            self.library.db,
            self.settings,
            runtime_python=Path(sys.executable),
            worker_module="tests.fake_vlm_worker",
            model_path=self.model,
            request_timeout_s=timeout,
            batch_size=8,
            require_governor=False,
        )

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        self._env.__exit__(None, None, None)
        self._tmp.cleanup()

    def sample(self, asset: dict) -> None:
        job = self.client.post(
            f"/api/v1/assets/{asset['asset_id']}:sample", json={"profile": "quick"}, headers=HEADERS
        ).json()
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            state = self.client.get(f"/api/v1/jobs/{job['job_id']}").json()
            if state["state"] not in {"queued", "running", "cancelling"}:
                self.assertEqual(state["state"], "complete", state)
                return
            time.sleep(0.1)
        raise AssertionError("sampling did not finish")

    def wait_job(self, job_id: str, timeout: float = 90) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.client.get(f"/api/v1/jobs/{job_id}").json()
            if job["state"] not in {"queued", "running", "cancelling"}:
                return job
            time.sleep(0.1)
        raise AssertionError("analysis did not finish")

    def test_analysis_requires_sampling(self) -> None:
        response = self.client.post(
            f"/api/v1/assets/{self.cuts['asset_id']}:analyze", json={"method": "auto"}, headers=HEADERS
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "SAMPLING_REQUIRED")

    def test_analysis_requires_capability(self) -> None:
        response = self.client.post(
            f"/api/v1/assets/{self.cuts['asset_id']}:analyze", json={"method": "auto"}
        )
        self.assertEqual(response.status_code, 403)

    def test_analysis_job_produces_evidence(self) -> None:
        self.sample(self.cuts)
        with mock.patch.dict(os.environ, {"FAKE_VLM_MODE": "ok"}):
            started = self.client.post(
                f"/api/v1/assets/{self.cuts['asset_id']}:analyze",
                json={"method": "sequential_frames"},
                headers=HEADERS,
            )
            self.assertEqual(started.status_code, 202, started.text)
            job = self.wait_job(started.json()["job_id"])
        self.assertEqual(job["state"], "complete", job)

        summary = self.client.get(f"/api/v1/assets/{self.cuts['asset_id']}/analysis").json()
        self.assertEqual(summary["analysis_status"], "complete")
        self.assertGreater(summary["frames_observed"], 0)
        self.assertEqual(summary["coverage_status"], "sufficient")
        evidence = summary["evidence"]
        self.assertEqual(evidence["coverage_status"], "sufficient")
        self.assertFalse(evidence["is_every_frame_analyzed"])
        self.assertTrue(all(frame["interpretation_level"] == "observed" for frame in evidence["frames"]))

        asset = self.client.get(f"/api/v1/assets/{self.cuts['asset_id']}").json()
        self.assertEqual(asset["analysis_status"], "complete")
        self.assertEqual(asset["analysis_coverage"], "sufficient")

    def test_second_analysis_for_same_asset_conflicts(self) -> None:
        self.sample(self.cfr)
        with mock.patch.dict(os.environ, {"FAKE_VLM_MODE": "slow"}):
            first = self.client.post(
                f"/api/v1/assets/{self.cfr['asset_id']}:analyze", json={"method": "auto"}, headers=HEADERS
            )
            self.assertEqual(first.status_code, 202, first.text)
            second = self.client.post(
                f"/api/v1/assets/{self.cfr['asset_id']}:analyze", json={"method": "auto"}, headers=HEADERS
            )
            self.assertEqual(second.status_code, 409)
            self.assertEqual(second.json()["code"], "JOB_ALREADY_RUNNING")
            self.client.post(f"/api/v1/jobs/{first.json()['job_id']}:cancel", headers=HEADERS)
            self.wait_job(first.json()["job_id"])

    def test_notes_work_while_inference_runs(self) -> None:
        self.sample(self.cfr)
        with mock.patch.dict(os.environ, {"FAKE_VLM_MODE": "slow"}):
            started = self.client.post(
                f"/api/v1/assets/{self.cfr['asset_id']}:analyze", json={"method": "auto"}, headers=HEADERS
            )
            time.sleep(0.3)
            note = self.client.post(
                f"/api/v1/projects/{self.project_id}/annotations",
                json={"scope": "clip", "asset_id": self.cfr["asset_id"], "text": "Ghi chú trong lúc AI chạy"},
                headers=HEADERS,
            )
            self.assertEqual(note.status_code, 201, note.text)
            listing = self.client.get("/api/v1/assets", params={"project_id": self.project_id})
            self.assertEqual(listing.status_code, 200)
            self.client.post(f"/api/v1/jobs/{started.json()['job_id']}:cancel", headers=HEADERS)
            self.wait_job(started.json()["job_id"])
        notes = self.client.get(f"/api/v1/projects/{self.project_id}/annotations").json()
        self.assertTrue(any("trong lúc AI" in item["text"] for item in notes))

    def test_cancel_leaves_partial_evidence(self) -> None:
        self.sample(self.cfr)
        with mock.patch.dict(os.environ, {"FAKE_VLM_MODE": "slow"}):
            started = self.client.post(
                f"/api/v1/assets/{self.cfr['asset_id']}:analyze", json={"method": "auto"}, headers=HEADERS
            )
            time.sleep(0.4)
            self.client.post(f"/api/v1/jobs/{started.json()['job_id']}:cancel", headers=HEADERS)
            job = self.wait_job(started.json()["job_id"])
        self.assertEqual(job["state"], "cancelled")
        summary = self.client.get(f"/api/v1/assets/{self.cfr['asset_id']}/analysis").json()
        self.assertIsNotNone(summary["evidence"])
        self.assertTrue(summary["warnings"])


if __name__ == "__main__":
    unittest.main()
