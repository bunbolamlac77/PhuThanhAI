"""API integration tests: loopback binding policy, capability, headers, leakage."""

from __future__ import annotations

import json
import tempfile
import uuid
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from phuthanhai.api.app import create_app
from phuthanhai.library.service import LibraryService
from phuthanhai.library import store
from phuthanhai.api.security import CAPABILITY_HEADER, CSP, _has_traversal
from tests.helpers import isolated_settings

PORT = 18765


class ApiSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._env = isolated_settings(Path(self._tmp.name))
        self.settings = self._env.__enter__()
        self.app = create_app(self.settings, port=PORT, capability="test-capability-0123456789")
        self.client = TestClient(self.app, base_url=f"http://127.0.0.1:{PORT}")
        self.client.__enter__()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        self._env.__exit__(None, None, None)
        self._tmp.cleanup()

    # --- happy path ------------------------------------------------------- #
    def test_health_is_loopback_and_model_free(self) -> None:
        response = self.client.get("/api/v1/health")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "ok")
        self.assertTrue(body["loopback_only"])
        self.assertFalse(body["model_loaded_in_process"])
        self.assertTrue(body["offline_enforced"])
        self.assertFalse(body["heavy_slot_busy"])
        self.assertTrue(body["web_bundle_present"], "web/dist must be built before the gate")

    def test_session_hands_capability_in_body_only(self) -> None:
        response = self.client.get("/api/v1/session")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["capability"], "test-capability-0123456789")
        self.assertEqual(body["header_name"], CAPABILITY_HEADER)
        self.assertNotIn("capability", response.url.path)

    def test_allowed_origin_is_accepted(self) -> None:
        response = self.client.get("/api/v1/health", headers={"origin": f"http://127.0.0.1:{PORT}"})
        self.assertEqual(response.status_code, 200)

    def test_state_changing_request_with_capability_succeeds(self) -> None:
        response = self.client.post(
            "/api/v1/contracts:recheck",
            headers={CAPABILITY_HEADER: "test-capability-0123456789"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "pass")

    # --- rejected paths --------------------------------------------------- #
    def test_foreign_host_is_rejected(self) -> None:
        response = self.client.get("/api/v1/health", headers={"host": "evil.example.com"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "SECURITY_DENIED")

    def test_foreign_origin_is_rejected(self) -> None:
        response = self.client.get("/api/v1/health", headers={"origin": "http://evil.example.com"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "SECURITY_DENIED")

    def test_cross_site_fetch_metadata_is_rejected(self) -> None:
        response = self.client.get("/api/v1/health", headers={"sec-fetch-site": "cross-site"})
        self.assertEqual(response.status_code, 403)

    def test_missing_capability_is_rejected(self) -> None:
        response = self.client.post("/api/v1/contracts:recheck")
        self.assertEqual(response.status_code, 403)

    def test_wrong_capability_is_rejected(self) -> None:
        response = self.client.post(
            "/api/v1/contracts:recheck", headers={CAPABILITY_HEADER: "wrong"}
        )
        self.assertEqual(response.status_code, 403)

    def test_encoded_traversal_is_not_served(self) -> None:
        response = self.client.get("/api/v1/%2e%2e/%2e%2e/etc/passwd")
        self.assertIn(response.status_code, {400, 404})
        self.assertNotIn("root:", response.text)

    def test_traversal_detector(self) -> None:
        self.assertTrue(_has_traversal("/api/../secret"))
        self.assertTrue(_has_traversal("/api//secret"))
        self.assertTrue(_has_traversal("/api\\secret"))
        self.assertFalse(_has_traversal("/api/v1/assets"))

    # --- hardening -------------------------------------------------------- #
    def test_security_headers_are_present(self) -> None:
        response = self.client.get("/api/v1/health")
        self.assertEqual(response.headers["Content-Security-Policy"], CSP)
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertNotIn("http://", response.headers["Content-Security-Policy"])
        self.assertNotIn("https://", response.headers["Content-Security-Policy"])

    def test_health_does_not_leak_absolute_paths(self) -> None:
        text = json.dumps(self.client.get("/api/v1/health").json())
        self.assertNotIn("/Users/", text)
        self.assertNotIn(str(Path.home()), text)

    def test_unknown_api_route_returns_json_error(self) -> None:
        response = self.client.get("/api/v1/does-not-exist")
        self.assertEqual(response.status_code, 404)

    def test_session_file_is_private(self) -> None:
        path = self.settings.data.session_file
        self.assertTrue(path.is_file())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["port"], PORT)
        self.assertNotIn("token=", path.name)

    def test_instance_lock_is_private_and_held(self) -> None:
        path = self.settings.data.instance_lock
        self.assertTrue(path.is_file())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.read_text(encoding="utf-8").strip(), str(self.app.state.pid))


class StartupOwnershipTests(unittest.TestCase):
    def test_app_construction_does_not_reconcile_before_instance_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            footage = root / "footage"
            footage.mkdir()
            with isolated_settings(root / "data") as settings:
                service = LibraryService(settings)
                project = service.create_project("startup", str(footage))
                run_id = str(uuid.uuid4())
                now = store.utc_now_iso()
                with service.db.write() as conn:
                    conn.execute(
                        "INSERT INTO analysis_runs(run_id, project_id, status, config_json, "
                        "total_items, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                        (run_id, project["project_id"], "running", "{}", 0, now, now),
                    )
                service.close()

                app = create_app(settings, port=PORT + 1, capability="startup-capability")
                with service.db.read() as conn:
                    status = conn.execute(
                        "SELECT status FROM analysis_runs WHERE run_id=?", (run_id,)
                    ).fetchone()[0]
                library = getattr(app.state, "library", None)
                if library is not None:
                    library.close()
                self.assertEqual(status, "running")


if __name__ == "__main__":
    unittest.main()
