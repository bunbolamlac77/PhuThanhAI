"""S02 API integration: sampling jobs, progress, cancel/resume, concurrency, notes during jobs."""

from __future__ import annotations

import shutil
import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from phuthanhai.api.app import create_app
from tests.helpers import isolated_settings
from tests.media_fixtures import get

PORT = 19042
CAP = "s02-api-cap"
HEADERS = {"X-PhuThanhAI-Capability": CAP}


class SamplingApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.footage = Path(self._tmp.name) / "footage"
        self.footage.mkdir()
        shutil.copy(get("cuts"), self.footage / "cuts.mp4")
        shutil.copy(get("long_nocut"), self.footage / "long.mp4")
        self._env = isolated_settings(Path(self._tmp.name) / "data")
        self.settings = self._env.__enter__()
        self.client = TestClient(
            create_app(self.settings, port=PORT, capability=CAP), base_url=f"http://127.0.0.1:{PORT}"
        )
        self.client.__enter__()
        project = self.client.post(
            "/api/v1/projects", json={"name": "S02", "root_path": str(self.footage)}, headers=HEADERS
        ).json()
        self.project_id = project["project_id"]
        self.client.post(f"/api/v1/projects/{self.project_id}/assets:scan", json={}, headers=HEADERS)
        items = self.client.get(
            "/api/v1/assets", params={"project_id": self.project_id}
        ).json()["items"]
        self.cuts = next(item for item in items if item["filename"] == "cuts.mp4")
        self.long = next(item for item in items if item["filename"] == "long.mp4")

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        self._env.__exit__(None, None, None)
        self._tmp.cleanup()

    def _wait(self, job_id: str, timeout: float = 60.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.client.get(f"/api/v1/jobs/{job_id}").json()
            if job["state"] not in {"queued", "running", "cancelling"}:
                return job
            time.sleep(0.1)
        raise AssertionError(f"job {job_id} did not finish")

    def test_sampling_job_completes_and_reports_coverage(self) -> None:
        response = self.client.post(
            f"/api/v1/assets/{self.cuts['asset_id']}:sample", json={"profile": "detailed"}, headers=HEADERS
        )
        self.assertEqual(response.status_code, 202, response.text)
        job = self._wait(response.json()["job_id"])
        self.assertEqual(job["state"], "complete", job)
        self.assertEqual(job["progress"], 1.0)

        summary = self.client.get(f"/api/v1/assets/{self.cuts['asset_id']}/sampling").json()
        self.assertEqual(summary["sampling_status"], "complete")
        manifest = summary["manifest"]
        self.assertGreater(manifest["frame_count"], 0)
        self.assertGreaterEqual(manifest["scene_count"], 4)
        self.assertLessEqual(manifest["observed_max_gap_ms"], manifest["expected_max_gap_ms"])
        self.assertEqual(manifest["coverage"]["pts_mismatch_frames"], [])
        frame = manifest["frames"][0]
        self.assertIn("normalized_pts_ms", frame)
        self.assertIn("reason", frame)

        asset = self.client.get(f"/api/v1/assets/{self.cuts['asset_id']}").json()
        self.assertEqual(asset["sampling_status"], "complete")
        self.assertEqual(asset["sampling_profile"], "detailed")

    def test_unsampled_asset_reports_not_sampled(self) -> None:
        summary = self.client.get(f"/api/v1/assets/{self.long['asset_id']}/sampling").json()
        self.assertEqual(summary["sampling_status"], "not_sampled")
        self.assertIsNone(summary["manifest"])

    def test_sampling_requires_capability(self) -> None:
        response = self.client.post(f"/api/v1/assets/{self.cuts['asset_id']}:sample", json={"profile": "quick"})
        self.assertEqual(response.status_code, 403)

    def test_invalid_profile_is_rejected(self) -> None:
        response = self.client.post(
            f"/api/v1/assets/{self.cuts['asset_id']}:sample", json={"profile": "cinema"}, headers=HEADERS
        )
        self.assertEqual(response.status_code, 422)

    def test_second_job_for_same_asset_conflicts(self) -> None:
        first = self.client.post(
            f"/api/v1/assets/{self.long['asset_id']}:sample", json={"profile": "thorough"}, headers=HEADERS
        )
        self.assertEqual(first.status_code, 202)
        second = self.client.post(
            f"/api/v1/assets/{self.long['asset_id']}:sample", json={"profile": "quick"}, headers=HEADERS
        )
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.json()["code"], "JOB_ALREADY_RUNNING")
        self.client.post(f"/api/v1/jobs/{first.json()['job_id']}:cancel", headers=HEADERS)
        self._wait(first.json()["job_id"])

    def test_cancel_then_resume(self) -> None:
        start = self.client.post(
            f"/api/v1/assets/{self.long['asset_id']}:sample", json={"profile": "thorough"}, headers=HEADERS
        ).json()
        cancel = self.client.post(f"/api/v1/jobs/{start['job_id']}:cancel", headers=HEADERS)
        self.assertEqual(cancel.status_code, 200)
        job = self._wait(start["job_id"])
        self.assertIn(job["state"], {"cancelled", "complete"})
        if job["state"] == "cancelled":
            resume = self.client.post(
                f"/api/v1/assets/{self.long['asset_id']}:sample", json={"profile": "thorough"}, headers=HEADERS
            )
            finished = self._wait(resume.json()["job_id"], timeout=120)
            self.assertEqual(finished["state"], "complete")
            summary = self.client.get(f"/api/v1/assets/{self.long['asset_id']}/sampling").json()
            self.assertEqual(summary["sampling_status"], "complete")

    def test_library_stays_usable_while_sampling(self) -> None:
        start = self.client.post(
            f"/api/v1/assets/{self.long['asset_id']}:sample", json={"profile": "thorough"}, headers=HEADERS
        ).json()
        note = self.client.post(
            f"/api/v1/projects/{self.project_id}/annotations",
            json={"scope": "clip", "asset_id": self.long["asset_id"], "text": "Ghi chú trong lúc sampling"},
            headers=HEADERS,
        )
        self.assertEqual(note.status_code, 201, note.text)
        listing = self.client.get("/api/v1/assets", params={"project_id": self.project_id})
        self.assertEqual(listing.status_code, 200)
        self.client.post(f"/api/v1/jobs/{start['job_id']}:cancel", headers=HEADERS)
        self._wait(start["job_id"])

    def test_jobs_are_listed(self) -> None:
        self.client.post(
            f"/api/v1/assets/{self.cuts['asset_id']}:sample", json={"profile": "quick"}, headers=HEADERS
        )
        deadline = time.monotonic() + 60
        jobs: list = []
        while time.monotonic() < deadline:
            jobs = self.client.get(f"/api/v1/projects/{self.project_id}/jobs").json()
            if jobs and jobs[0]["state"] in {"complete", "failed", "cancelled"}:
                break
            time.sleep(0.1)
        self.assertGreaterEqual(len(jobs), 1)
        self.assertIn("progress", jobs[0])


if __name__ == "__main__":
    unittest.main()
