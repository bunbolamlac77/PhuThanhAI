"""S05.4R.2 addendum: auto and batch API contracts."""

from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from phuthanhai.api.app import create_app
from tests.helpers import isolated_settings
from tests.media_fixtures import get

PORT = 19077
CAP = "s05-batch-cap"
HEADERS = {"X-PhuThanhAI-Capability": CAP}


class BatchApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        footage = Path(self._tmp.name) / "footage"
        footage.mkdir()
        for index in range(2):
            shutil.copy(get("cuts"), footage / f"clip{index}.mp4")
        self._env = isolated_settings(Path(self._tmp.name) / "data")
        self.settings = self._env.__enter__()
        os.environ["FAKE_VLM_MODE"] = "ok"
        self.client = TestClient(
            create_app(self.settings, port=PORT, capability=CAP), base_url=f"http://127.0.0.1:{PORT}"
        )
        self.client.__enter__()
        project = self.client.post(
            "/api/v1/projects", json={"name": "batch-api", "root_path": str(footage)}, headers=HEADERS
        ).json()
        self.project_id = project["project_id"]
        self.client.post(f"/api/v1/projects/{self.project_id}/assets:scan", json={}, headers=HEADERS)
        self.assets = self.client.get(
            "/api/v1/assets", params={"project_id": self.project_id}, headers=HEADERS
        ).json()["items"]

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        os.environ.pop("FAKE_VLM_MODE", None)
        self._env.__exit__(None, None, None)
        self._tmp.cleanup()

    def wait_job(self, job_id: str, timeout: float = 90.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            body = self.client.get(f"/api/v1/jobs/{job_id}", headers=HEADERS).json()
            if body["state"] not in {"queued", "running", "cancelling"}:
                return body
            time.sleep(0.1)
        raise AssertionError("job timed out")

    def test_auto_endpoint_and_run_endpoints(self) -> None:
        asset = self.assets[0]
        response = self.client.post(
            f"/api/v1/assets/{asset['asset_id']}:analyze-auto",
            json={"selection_mode": "smart", "sampling_profile": "detailed"},
            headers=HEADERS,
        )
        self.assertEqual(response.status_code, 202, response.text)
        job = self.wait_job(response.json()["job_id"])
        self.assertEqual(job["state"], "complete")

        created = self.client.post(
            f"/api/v1/projects/{self.project_id}/analysis-runs",
            json={"asset_ids": [self.assets[0]["asset_id"], self.assets[1]["asset_id"]],
                  "selection_mode": "smart", "sampling_profile": "detailed"},
            headers=HEADERS,
        )
        self.assertEqual(created.status_code, 202, created.text)
        run = created.json()
        self.assertEqual(run["total_items"], 2)
        recovered = self.client.get(
            f"/api/v1/projects/{self.project_id}/analysis-runs", headers=HEADERS
        )
        self.assertEqual(recovered.status_code, 200, recovered.text)
        self.assertEqual(recovered.json()[0]["run_id"], run["run_id"])
        items = self.client.get(f"/api/v1/analysis-runs/{run['run_id']}/items", headers=HEADERS)
        self.assertEqual(items.status_code, 200)
        self.assertEqual(len(items.json()), 2)
        paused = self.client.post(f"/api/v1/analysis-runs/{run['run_id']}:cancel", headers=HEADERS)
        self.assertEqual(paused.status_code, 200)
        self.assertEqual(paused.json()["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
