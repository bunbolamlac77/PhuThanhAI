"""S04 API: profile revision, labels, context overlay, reports, targeted review."""

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
from phuthanhai.contracts.validators import parse_portable_workspace_md
from tests.helpers import isolated_settings
from tests.media_fixtures import get

PORT = 19044
CAP = "s04-cap"
HEADERS = {"X-PhuThanhAI-Capability": CAP}


class ContextApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.footage = self.root / "footage"
        self.footage.mkdir()
        shutil.copy(get("cuts"), self.footage / "clip.mp4")
        self._env = isolated_settings(self.root / "data")
        self.settings = self._env.__enter__()
        app = create_app(self.settings, port=PORT, capability=CAP)
        self.client = TestClient(app, base_url=f"http://127.0.0.1:{PORT}")
        self.client.__enter__()
        self.library = app.state.library
        project = self.client.post(
            "/api/v1/projects", json={"name": "S04", "root_path": str(self.footage)}, headers=HEADERS
        ).json()
        self.project_id = project["project_id"]
        self.client.post(f"/api/v1/projects/{self.project_id}/assets:scan", json={}, headers=HEADERS)
        self.asset = self.client.get("/api/v1/assets", params={"project_id": self.project_id}).json()["items"][0]
        job = self.client.post(
            f"/api/v1/assets/{self.asset['asset_id']}:sample", json={"profile": "quick"}, headers=HEADERS
        ).json()
        self.wait(job["job_id"])

        model = self.root / "model"
        model.mkdir()
        (model / "config.json").write_text("{}", encoding="utf-8")
        (model / "model.safetensors").write_bytes(b"stub")
        self.model = model
        self.library.vision = VisionService(
            self.library.db,
            self.settings,
            runtime_python=Path(sys.executable),
            worker_module="tests.fake_vlm_worker",
            model_path=model,
            request_timeout_s=10,
            batch_size=8,
            require_governor=False,
        )
        with mock.patch.dict(os.environ, {"FAKE_VLM_MODE": "ok"}):
            started = self.client.post(
                f"/api/v1/assets/{self.asset['asset_id']}:analyze", json={"method": "auto"}, headers=HEADERS
            ).json()
            self.wait(started["job_id"])

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        self._env.__exit__(None, None, None)
        self._tmp.cleanup()

    def wait(self, job_id: str, timeout: float = 90) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.client.get(f"/api/v1/jobs/{job_id}").json()
            if job["state"] not in {"queued", "running", "cancelling"}:
                return job
            time.sleep(0.1)
        raise AssertionError("job did not finish")

    def profile(self) -> dict:
        return self.client.get(f"/api/v1/projects/{self.project_id}/profile").json()

    def test_default_profile_and_revision_conflict(self) -> None:
        self.assertEqual(self.profile()["analysis_profile"], "general")
        updated = self.client.put(
            f"/api/v1/projects/{self.project_id}/profile",
            json={"profile": "wedding_traditional", "expected_revision": 1},
            headers=HEADERS,
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()["analysis_profile"], "wedding_traditional")
        self.assertEqual(updated.json()["profile_revision"], 2)
        stale = self.client.put(
            f"/api/v1/projects/{self.project_id}/profile",
            json={"profile": "general", "expected_revision": 1},
            headers=HEADERS,
        )
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["code"], "PROFILE_REVISION_CONFLICT")

    def test_profile_put_requires_capability(self) -> None:
        response = self.client.put(
            f"/api/v1/projects/{self.project_id}/profile",
            json={"profile": "general", "expected_revision": 1},
        )
        self.assertEqual(response.status_code, 403)

    def test_context_overlay_and_frame_endpoint(self) -> None:
        overlay = self.client.get(f"/api/v1/assets/{self.asset['asset_id']}/context").json()
        self.assertEqual(overlay["profile"], "general")
        self.assertTrue(overlay["raw_evidence_sha256"])
        self.assertIn("scenes", overlay)
        frame_id = overlay["scenes"][0]["frame_ids"][0]
        response = self.client.get(f"/api/v1/assets/{self.asset['asset_id']}/frames/{frame_id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "image/jpeg")
        self.assertGreater(len(response.content), 0)

    def test_confirm_label_persists_and_notes_list_stays_clean(self) -> None:
        overlay = self.client.get(f"/api/v1/assets/{self.asset['asset_id']}/context").json()
        scene_id = overlay["scenes"][0]["scene_id"]
        saved = self.client.post(
            f"/api/v1/assets/{self.asset['asset_id']}/labels",
            json={
                "label_key": "ancestor_altar",
                "label_vi": "Bàn gia tiên",
                "status": "confirmed",
                "scene_id": scene_id,
            },
            headers=HEADERS,
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(len(saved.json()["user_labels"]), 1)
        notes = self.client.get(f"/api/v1/projects/{self.project_id}/annotations").json()
        self.assertFalse(any(note["text"] == "Bàn gia tiên" for note in notes))

    def test_report_hides_private_notes_by_default(self) -> None:
        self.client.post(
            f"/api/v1/projects/{self.project_id}/annotations",
            json={"scope": "clip", "asset_id": self.asset["asset_id"], "text": "PRIVATE_NOTE_SHOULD_NOT_LEAK"},
            headers=HEADERS,
        )
        hidden = self.client.post(
            f"/api/v1/projects/{self.project_id}/reports",
            json={"include_private_notes": False},
            headers=HEADERS,
        )
        self.assertEqual(hidden.status_code, 200, hidden.text)
        portable = self.client.get(
            f"/api/v1/projects/{self.project_id}/reports/workspace_report_portable.md"
        ).text
        self.assertNotIn("PRIVATE_NOTE_SHOULD_NOT_LEAK", portable)
        parsed = parse_portable_workspace_md(portable)
        self.assertEqual(parsed["project_id"], self.project_id)

        shared = self.client.post(
            f"/api/v1/projects/{self.project_id}/reports",
            json={"include_private_notes": True},
            headers=HEADERS,
        )
        self.assertEqual(shared.status_code, 200, shared.text)
        portable2 = self.client.get(
            f"/api/v1/projects/{self.project_id}/reports/workspace_report_portable.md"
        ).text
        self.assertIn("PRIVATE_NOTE_SHOULD_NOT_LEAK", portable2)

    def test_profile_change_never_runs_inference(self) -> None:
        with mock.patch.object(
            self.library.vision, "_run_worker", side_effect=AssertionError("raw inference must not run")
        ):
            for target, revision in (("wedding_traditional", 1), ("general", 2)):
                response = self.client.put(
                    f"/api/v1/projects/{self.project_id}/profile",
                    json={"profile": target, "expected_revision": revision},
                    headers=HEADERS,
                )
                self.assertEqual(response.status_code, 200, response.text)
                self.client.get(f"/api/v1/assets/{self.asset['asset_id']}/context")
            built = self.client.post(
                f"/api/v1/projects/{self.project_id}/reports",
                json={"include_private_notes": False},
                headers=HEADERS,
            )
            self.assertEqual(built.status_code, 200, built.text)

    def test_targeted_review_is_cached_and_does_not_touch_raw_evidence(self) -> None:
        before = self.client.get(f"/api/v1/assets/{self.asset['asset_id']}/context").json()["raw_evidence_sha256"]
        frame_id = self.client.get(f"/api/v1/assets/{self.asset['asset_id']}/context").json()["scenes"][0]["frame_ids"][0]
        with mock.patch.dict(os.environ, {"FAKE_VLM_MODE": "ok"}):
            first = self.client.post(
                f"/api/v1/assets/{self.asset['asset_id']}:context-review",
                json={"profile": "wedding_traditional", "frame_ids": [frame_id]},
                headers=HEADERS,
            )
            self.assertEqual(first.status_code, 200, first.text)
            self.assertFalse(first.json()["cached"])
        with mock.patch.object(
            self.library.vision, "_run_worker", side_effect=AssertionError("cache must be used")
        ):
            second = self.client.post(
                f"/api/v1/assets/{self.asset['asset_id']}:context-review",
                json={"profile": "wedding_traditional", "frame_ids": [frame_id]},
                headers=HEADERS,
            )
        self.assertEqual(second.status_code, 200, second.text)
        self.assertTrue(second.json()["cached"])
        after = self.client.get(f"/api/v1/assets/{self.asset['asset_id']}/context").json()["raw_evidence_sha256"]
        self.assertEqual(before, after)
        self.assertNotIn("confidence", second.text)


if __name__ == "__main__":
    unittest.main()
