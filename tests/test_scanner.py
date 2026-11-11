"""Scanner tests: idempotence, symlink discipline, corrupt files, real footage."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from phuthanhai.library import store
from phuthanhai.library.db import Database
from phuthanhai.library.index import refresh_source_states, scan_project
from phuthanhai.library.service import LibraryService
from tests.helpers import isolated_settings, make_test_video


class ScannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.footage = self.root / "footage"
        self.footage.mkdir()
        self.videos = [
            make_test_video(self.footage / "C0002.MOV", seconds=0.6),
            make_test_video(self.footage / "C0010.MOV", seconds=0.6),
            make_test_video(self.footage / "sub" / "DJI_0001.MP4", seconds=0.6),
        ]
        (self.footage / "broken.mp4").write_bytes(b"this is not a video")
        (self.footage / "notes.txt").write_text("ignore me", encoding="utf-8")
        self.db = Database(self.root / "registry.sqlite3")
        self.db.migrate()
        with self.db.write() as conn:
            self.project = store.create_project(conn, name="Scan", root_path=str(self.footage))

    def tearDown(self) -> None:
        self.db.close_current_thread()
        self._tmp.cleanup()

    def test_scan_indexes_videos_and_records_corrupt_files(self) -> None:
        stats = scan_project(self.db, self.project["project_id"], self.footage)
        self.assertEqual(stats.added, 4)  # 3 playable + 1 corrupt
        self.assertEqual(len(stats.errors), 1)
        self.assertEqual(stats.errors[0]["code"], "CODEC_UNSUPPORTED")
        self.assertGreaterEqual(stats.skipped_non_video, 1)

        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT filename, source_state, error_code, duration_ms FROM assets ORDER BY filename"
            ).fetchall()
        states = {row["filename"]: row["source_state"] for row in rows}
        self.assertEqual(states["broken.mp4"], "error")
        self.assertEqual(states["C0002.MOV"], "available")
        self.assertEqual(states["DJI_0001.MP4"], "available")

    def test_rescan_is_idempotent(self) -> None:
        first = scan_project(self.db, self.project["project_id"], self.footage)
        second = scan_project(self.db, self.project["project_id"], self.footage)
        self.assertEqual(second.added, 0, "no duplicate rows may be created")
        # The unreadable file is deliberately re-probed (it may have been fixed),
        # so it counts as an update while every playable asset stays unchanged.
        self.assertEqual(second.updated, len(first.errors))
        self.assertEqual(second.unchanged, first.added - len(first.errors))

    def test_asset_ids_are_stable_across_scans(self) -> None:
        scan_project(self.db, self.project["project_id"], self.footage)
        with self.db.read() as conn:
            first = {row["path"]: row["asset_id"] for row in conn.execute("SELECT path, asset_id FROM assets")}
        scan_project(self.db, self.project["project_id"], self.footage)
        with self.db.read() as conn:
            second = {row["path"]: row["asset_id"] for row in conn.execute("SELECT path, asset_id FROM assets")}
        self.assertEqual(first, second)

    def test_symlink_loop_is_not_followed(self) -> None:
        loop_dir = self.footage / "loop"
        loop_dir.mkdir()
        os.symlink(self.footage, loop_dir / "back")
        os.symlink(self.videos[0], self.footage / "linked.mov")
        stats = scan_project(self.db, self.project["project_id"], self.footage)
        self.assertGreaterEqual(stats.skipped_symlinks, 2)
        with self.db.read() as conn:
            linked = conn.execute("SELECT COUNT(*) FROM assets WHERE filename = 'linked.mov'").fetchone()[0]
        self.assertEqual(linked, 0, "symlinked media must not be indexed")

    def test_scan_outside_project_root_is_denied(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        make_test_video(outside / "x.mp4", seconds=0.4)
        with self.assertRaises(store.StoreError) as ctx:
            scan_project(self.db, self.project["project_id"], outside)
        self.assertEqual(ctx.exception.code, "SECURITY_DENIED")

    def test_missing_source_is_detected(self) -> None:
        scan_project(self.db, self.project["project_id"], self.footage)
        self.videos[0].unlink()
        counts = refresh_source_states(self.db, self.project["project_id"])
        self.assertGreaterEqual(counts["missing"], 1)

    def test_source_files_are_not_modified(self) -> None:
        before = {path: (path.stat().st_size, path.stat().st_mtime_ns) for path in self.videos}
        scan_project(self.db, self.project["project_id"], self.footage)
        after = {path: (path.stat().st_size, path.stat().st_mtime_ns) for path in self.videos}
        self.assertEqual(before, after)


class RealFootageScanTests(unittest.TestCase):
    """Runs only when the operator supplies real footage (authorized for S01).

    The path is never committed: set ``PHUTHANHAI_TEST_FOOTAGE`` to a folder.
    """

    FOOTAGE = Path(os.environ["PHUTHANHAI_TEST_FOOTAGE"]).expanduser() if os.environ.get("PHUTHANHAI_TEST_FOOTAGE") else None

    def test_scan_real_folder_read_only(self) -> None:
        if self.FOOTAGE is None or not self.FOOTAGE.is_dir():
            self.skipTest("PHUTHANHAI_TEST_FOOTAGE not set")
        with tempfile.TemporaryDirectory() as tmp:
            with isolated_settings(Path(tmp)) as settings:
                service = LibraryService(settings)
                try:
                    project = service.create_project("Real footage", str(self.FOOTAGE))
                    stats = service.scan(project["project_id"])
                    self.assertGreater(stats.added, 0)
                    self.assertEqual(stats.errors, [])
                    page = service.list_assets(project["project_id"], limit=10)
                    self.assertGreater(page["total"], 0)
                    first = page["items"][0]
                    self.assertEqual(first["source_state"], "available")
                    self.assertGreater(first["duration_ms"] or 0, 0)

                    _asset, path = service.resolve_asset_file(first["asset_id"])
                    self.assertTrue(path.is_file())
                    before = (path.stat().st_size, path.stat().st_mtime_ns)
                    service.scan(project["project_id"])
                    after = (path.stat().st_size, path.stat().st_mtime_ns)
                    self.assertEqual(before, after, "scanning must not touch source bytes")
                finally:
                    service.close()


if __name__ == "__main__":
    unittest.main()
