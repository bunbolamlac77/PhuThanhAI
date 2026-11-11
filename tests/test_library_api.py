"""S01 API integration: scan, pagination, Range, thumbnails, proxy, notes, reviews."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from phuthanhai.api.app import create_app
from tests.helpers import isolated_settings, make_test_video

PORT = 18901
CAP = "s01-test-capability"
HEADERS = {"X-PhuThanhAI-Capability": CAP}


class LibraryApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.footage = Path(self._tmp.name) / "footage"
        self.footage.mkdir()
        make_test_video(self.footage / "C0002.MOV", seconds=0.8)
        make_test_video(self.footage / "C0010.MOV", seconds=0.8)
        make_test_video(self.footage / "sub" / "DJI_0001.MP4", seconds=0.8)
        (self.footage / "broken.mp4").write_bytes(b"not a video")
        self._env = isolated_settings(Path(self._tmp.name) / "data")
        self.settings = self._env.__enter__()
        self.client = TestClient(
            create_app(self.settings, port=PORT, capability=CAP),
            base_url=f"http://127.0.0.1:{PORT}",
        )
        self.client.__enter__()
        response = self.client.post(
            "/api/v1/projects",
            json={"name": "Demo", "root_path": str(self.footage)},
            headers=HEADERS,
        )
        assert response.status_code == 201, response.text
        self.project = response.json()
        self.project_id = self.project["project_id"]
        scan = self.client.post(f"/api/v1/projects/{self.project_id}/assets:scan", json={}, headers=HEADERS)
        assert scan.status_code == 200, scan.text
        self.scan = scan.json()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        self._env.__exit__(None, None, None)
        self._tmp.cleanup()

    def _assets(self, **params: str) -> dict:
        query = {"project_id": self.project_id, **params}
        response = self.client.get("/api/v1/assets", params=query)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    # -- projects ---------------------------------------------------------- #
    def test_project_response_hides_local_root(self) -> None:
        self.assertEqual(self.project["name"], "Demo")
        self.assertNotIn("root_path", self.project)
        self.assertTrue(self.project["created"])

    def test_duplicate_project_root_is_reused(self) -> None:
        response = self.client.post(
            "/api/v1/projects",
            json={"name": "Again", "root_path": str(self.footage)},
            headers=HEADERS,
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["created"])
        self.assertEqual(response.json()["project_id"], self.project_id)

    def test_scan_indexes_and_reports_errors(self) -> None:
        self.assertEqual(self.scan["added"], 4)
        self.assertEqual(len(self.scan["errors"]), 1)

    # -- listing ----------------------------------------------------------- #
    def test_assets_sorted_naturally_and_path_hidden(self) -> None:
        page = self._assets()
        self.assertEqual(page["total"], 4)
        names = [item["filename"] for item in page["items"]]
        self.assertLess(names.index("C0002.MOV"), names.index("C0010.MOV"))
        self.assertNotIn("path", page["items"][0])

    def test_keyset_pagination_has_no_overlap(self) -> None:
        first = self._assets(limit="2")
        self.assertEqual(len(first["items"]), 2)
        self.assertIsNotNone(first["next_cursor"])
        second = self._assets(limit="2", cursor=first["next_cursor"])
        ids_first = {item["asset_id"] for item in first["items"]}
        ids_second = {item["asset_id"] for item in second["items"]}
        self.assertEqual(ids_first & ids_second, set())

    def test_search_matches_filename(self) -> None:
        page = self._assets(q="DJI")
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["items"][0]["filename"], "DJI_0001.MP4")

    def test_missing_project_id_is_rejected(self) -> None:
        response = self.client.get("/api/v1/assets")
        self.assertEqual(response.status_code, 422)

    # -- range / preview --------------------------------------------------- #
    def test_stream_supports_partial_content(self) -> None:
        asset = self._assets(q="C0002")["items"][0]
        response = self.client.get(
            f"/api/v1/assets/{asset['asset_id']}/stream", headers={"Range": "bytes=0-99"}
        )
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.headers["content-range"], f"bytes 0-99/{asset['size_bytes']}")
        self.assertEqual(response.headers["accept-ranges"], "bytes")
        self.assertEqual(len(response.content), 100)

    def test_unsatisfiable_range_returns_416(self) -> None:
        asset = self._assets(q="C0002")["items"][0]
        response = self.client.get(
            f"/api/v1/assets/{asset['asset_id']}/stream",
            headers={"Range": f"bytes={asset['size_bytes']}-"},
        )
        self.assertEqual(response.status_code, 416)
        self.assertEqual(response.headers["content-range"], f"bytes */{asset['size_bytes']}")

    def test_full_stream_has_content_length(self) -> None:
        asset = self._assets(q="C0002")["items"][0]
        response = self.client.get(f"/api/v1/assets/{asset['asset_id']}/stream")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(int(response.headers["content-length"]), asset["size_bytes"])

    def test_thumbnail_is_jpeg(self) -> None:
        asset = self._assets(q="C0002")["items"][0]
        response = self.client.get(f"/api/v1/assets/{asset['asset_id']}/thumbnail")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "image/jpeg")
        self.assertGreater(len(response.content), 100)

    def test_proxy_is_created_on_demand_and_streams(self) -> None:
        asset = self._assets(q="C0002")["items"][0]
        response = self.client.post(f"/api/v1/assets/{asset['asset_id']}/proxy", headers=HEADERS)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["created"])
        proxy = self.client.get(
            f"/api/v1/assets/{asset['asset_id']}/proxy-stream", headers={"Range": "bytes=0-9"}
        )
        self.assertEqual(proxy.status_code, 206)
        self.assertEqual(len(proxy.content), 10)

    def test_unknown_asset_is_404(self) -> None:
        response = self.client.get("/api/v1/assets/not-a-real-id/stream")
        self.assertEqual(response.status_code, 404)

    # -- annotations ------------------------------------------------------- #
    def _clip_asset(self) -> dict:
        return self._assets(q="C0002")["items"][0]

    def test_wall_clock_is_suggested_not_asserted(self) -> None:
        asset = self._clip_asset()
        response = self.client.post(
            f"/api/v1/projects/{self.project_id}/annotations",
            json={
                "scope": "clip",
                "asset_id": asset["asset_id"],
                "text": "Lên phà lúc 5 giờ sáng, đợi khoảng 30 phút",
            },
            headers=HEADERS,
        )
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        annotation = body["annotation"]
        self.assertIsNone(annotation["wall_clock"])
        self.assertIsNone(annotation["start_ms"])
        self.assertEqual(body["wall_clock_suggestion"]["time_of_day"], "05:00")
        self.assertEqual(annotation["scope"], "clip")

    def test_annotation_revision_and_conflict(self) -> None:
        asset = self._clip_asset()
        created = self.client.post(
            f"/api/v1/projects/{self.project_id}/annotations",
            json={"scope": "clip", "asset_id": asset["asset_id"], "text": "ban đầu"},
            headers=HEADERS,
        ).json()["annotation"]
        updated = self.client.put(
            f"/api/v1/annotations/{created['annotation_id']}",
            json={
                "expected_revision": 1,
                "text": "Lên phà lúc 5 giờ sáng",
                "wall_clock": {"time_of_day": "05:00", "precision": "time_of_day"},
            },
            headers=HEADERS,
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()["annotation"]["revision"], 2)
        self.assertEqual(updated.json()["annotation"]["wall_clock"]["time_of_day"], "05:00")
        self.assertIsNone(updated.json()["annotation"]["start_ms"])

        stale = self.client.put(
            f"/api/v1/annotations/{created['annotation_id']}",
            json={"expected_revision": 1, "text": "ghi đè"},
            headers=HEADERS,
        )
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["code"], "ANNOTATION_CONFLICT")

    def test_time_range_note_and_bounds(self) -> None:
        asset = self._clip_asset()
        ok = self.client.post(
            f"/api/v1/projects/{self.project_id}/annotations",
            json={
                "scope": "time_range",
                "asset_id": asset["asset_id"],
                "start_ms": 100,
                "end_ms": 500,
                "text": "Đoạn chuẩn bị bước xuống phà",
            },
            headers=HEADERS,
        )
        self.assertEqual(ok.status_code, 201, ok.text)
        invalid = self.client.post(
            f"/api/v1/projects/{self.project_id}/annotations",
            json={
                "scope": "time_range",
                "asset_id": asset["asset_id"],
                "start_ms": 500,
                "end_ms": 400,
                "text": "sai",
            },
            headers=HEADERS,
        )
        self.assertEqual(invalid.status_code, 400)

    def test_project_scope_note_is_stored(self) -> None:
        response = self.client.post(
            f"/api/v1/projects/{self.project_id}/annotations",
            json={"scope": "project", "text": "Ekip xuất phát từ 3h sáng"},
            headers=HEADERS,
        )
        self.assertEqual(response.status_code, 201, response.text)
        annotation = response.json()["annotation"]
        self.assertIsNone(annotation["asset_id"])
        self.assertEqual(annotation["scope"], "project")
        listed = self.client.get(f"/api/v1/projects/{self.project_id}/annotations").json()
        self.assertTrue(any(note["scope"] == "project" for note in listed))

    def test_label_filter(self) -> None:
        asset = self._clip_asset()
        self.client.put(
            f"/api/v1/assets/{asset['asset_id']}/review",
            json={"status": "reviewed", "labels": ["phà", "ngoại cảnh"]},
            headers=HEADERS,
        )
        tagged = self._assets(label="phà")
        self.assertEqual(tagged["total"], 1)
        self.assertEqual(tagged["items"][0]["asset_id"], asset["asset_id"])
        self.assertEqual(self._assets(label="không-tồn-tại")["total"], 0)

    def test_project_scope_must_not_bind_asset(self) -> None:
        asset = self._clip_asset()
        response = self.client.post(
            f"/api/v1/projects/{self.project_id}/annotations",
            json={"scope": "project", "asset_id": asset["asset_id"], "text": "sai phạm vi"},
            headers=HEADERS,
        )
        self.assertEqual(response.status_code, 400)

    def test_annotations_require_capability(self) -> None:
        asset = self._clip_asset()
        response = self.client.post(
            f"/api/v1/projects/{self.project_id}/annotations",
            json={"scope": "clip", "asset_id": asset["asset_id"], "text": "no cap"},
        )
        self.assertEqual(response.status_code, 403)

    # -- reviews ----------------------------------------------------------- #
    def test_review_revision_and_stale_conflict(self) -> None:
        asset = self._clip_asset()
        first = self.client.put(
            f"/api/v1/assets/{asset['asset_id']}/review",
            json={"status": "reviewed", "rating": 5, "favorite": True},
            headers=HEADERS,
        )
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["rating"], 5)
        second = self.client.put(
            f"/api/v1/assets/{asset['asset_id']}/review",
            json={"expected_revision": 1, "status": "reviewed", "rating": 4, "favorite": True},
            headers=HEADERS,
        )
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["revision"], 2)
        stale = self.client.put(
            f"/api/v1/assets/{asset['asset_id']}/review",
            json={"expected_revision": 1, "status": "reviewed", "rating": 3, "favorite": False},
            headers=HEADERS,
        )
        self.assertEqual(stale.status_code, 409)

    def test_invalid_rating_is_rejected(self) -> None:
        asset = self._clip_asset()
        response = self.client.put(
            f"/api/v1/assets/{asset['asset_id']}/review",
            json={"status": "reviewed", "rating": 9},
            headers=HEADERS,
        )
        self.assertEqual(response.status_code, 422)

    # -- security ---------------------------------------------------------- #
    def test_scan_outside_project_root_is_denied(self) -> None:
        outside = Path(self._tmp.name) / "outside"
        outside.mkdir()
        response = self.client.post(
            f"/api/v1/projects/{self.project_id}/assets:scan",
            json={"directory": str(outside)},
            headers=HEADERS,
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "SECURITY_DENIED")


class PersistenceTests(unittest.TestCase):
    def test_notes_survive_a_backend_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            footage = Path(tmp) / "footage"
            footage.mkdir()
            make_test_video(footage / "C0001.MOV", seconds=0.6)
            data_root = Path(tmp) / "data"
            with isolated_settings(data_root) as settings:
                client = TestClient(
                    create_app(settings, port=PORT + 1, capability=CAP),
                    base_url=f"http://127.0.0.1:{PORT + 1}",
                )
                with client:
                    project = client.post(
                        "/api/v1/projects",
                        json={"name": "Restart", "root_path": str(footage)},
                        headers=HEADERS,
                    ).json()
                    client.post(f"/api/v1/projects/{project['project_id']}/assets:scan", json={}, headers=HEADERS)
                    asset = client.get(
                        "/api/v1/assets", params={"project_id": project["project_id"]}
                    ).json()["items"][0]
                    created = client.post(
                        f"/api/v1/projects/{project['project_id']}/annotations",
                        json={"scope": "clip", "asset_id": asset["asset_id"], "text": "Ghi chú sống sót"},
                        headers=HEADERS,
                    ).json()["annotation"]

                restarted = TestClient(
                    create_app(settings, port=PORT + 1, capability=CAP),
                    base_url=f"http://127.0.0.1:{PORT + 1}",
                )
                with restarted:
                    notes = restarted.get(
                        f"/api/v1/projects/{project['project_id']}/annotations"
                    ).json()
                self.assertEqual(len(notes), 1)
                self.assertEqual(notes[0]["annotation_id"], created["annotation_id"])
                self.assertEqual(notes[0]["text"], "Ghi chú sống sót")


if __name__ == "__main__":
    unittest.main()
