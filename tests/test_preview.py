"""Preview tests: HTTP Range arithmetic, thumbnails, proxy mapping, cache quota."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from PIL import Image

from phuthanhai.library import store
from phuthanhai.library.db import Database
from phuthanhai.library.preview import (
    RangeNotSatisfiable,
    PreviewService,
    iter_file_range,
    parse_range_header,
)
from tests.helpers import make_test_video


class RangeParserTests(unittest.TestCase):
    SIZE = 1000

    def test_no_header_means_full_file(self) -> None:
        self.assertIsNone(parse_range_header(None, self.SIZE))
        self.assertIsNone(parse_range_header("", self.SIZE))

    def test_closed_range_is_end_exclusive(self) -> None:
        self.assertEqual(parse_range_header("bytes=0-99", self.SIZE), (0, 100))
        self.assertEqual(parse_range_header("bytes=100-199", self.SIZE), (100, 200))

    def test_open_ended_range(self) -> None:
        self.assertEqual(parse_range_header("bytes=900-", self.SIZE), (900, 1000))

    def test_suffix_range(self) -> None:
        self.assertEqual(parse_range_header("bytes=-100", self.SIZE), (900, 1000))

    def test_end_beyond_file_is_clamped(self) -> None:
        self.assertEqual(parse_range_header("bytes=990-99999", self.SIZE), (990, 1000))

    def test_start_at_size_is_unsatisfiable(self) -> None:
        with self.assertRaises(RangeNotSatisfiable):
            parse_range_header("bytes=1000-", self.SIZE)

    def test_reversed_range_is_unsatisfiable(self) -> None:
        with self.assertRaises(RangeNotSatisfiable):
            parse_range_header("bytes=200-100", self.SIZE)

    def test_multiple_ranges_are_refused(self) -> None:
        with self.assertRaises(RangeNotSatisfiable):
            parse_range_header("bytes=0-10,20-30", self.SIZE)

    def test_garbage_is_refused(self) -> None:
        for header in ("items=0-1", "bytes=abc-def", "bytes=-", "bytes=1"):
            with self.assertRaises(RangeNotSatisfiable):
                parse_range_header(header, self.SIZE)


class FileRangeTests(unittest.TestCase):
    def test_exact_bytes_are_returned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "data.bin"
            payload = bytes(range(256)) * 4
            path.write_bytes(payload)
            chunks = b"".join(iter_file_range(path, 100, 400, chunk_size=64))
            self.assertEqual(chunks, payload[100:400])

    def test_empty_when_range_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "data.bin"
            path.write_bytes(b"abc")
            self.assertEqual(b"".join(iter_file_range(path, 3, 3)), b"")


class PreviewServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.db = Database(self.root / "registry.sqlite3")
        self.db.migrate()
        self.service = PreviewService(
            self.db,
            thumbs_dir=self.root / "thumbs",
            proxies_dir=self.root / "proxies",
            quota_bytes=2 * 1024**2,
            max_workers=2,
        )
        with self.db.write() as conn:
            project = store.create_project(conn, name="Preview", root_path=str(self.root))
        self.project_id = project["project_id"]
        self.video = make_test_video(self.root / "clip.mp4", seconds=1.2, size="320x240")
        with self.db.write() as conn:
            store.upsert_asset(
                conn,
                asset_id="11111111-1111-4111-8111-111111111111",
                project_id=self.project_id,
                path=str(self.video),
                filename=self.video.name,
                size_bytes=self.video.stat().st_size,
                mtime_ns=self.video.stat().st_mtime_ns,
                dev=None,
                ino=None,
                duration_ms=1200,
                width=320,
                height=240,
                rotation=0,
                fps_nominal="25/1",
                codec="h264",
                has_audio=False,
                vfr_hint=False,
                color_transfer="bt709",
            )
        self.asset = {
            "asset_id": "11111111-1111-4111-8111-111111111111",
            "path": str(self.video),
            "duration_ms": 1200,
        }

    def tearDown(self) -> None:
        self.service.shutdown()
        self.db.close_current_thread()
        self._tmp.cleanup()

    def test_thumbnail_is_generated_and_bounded(self) -> None:
        path = self.service.get_thumbnail(self.asset)
        self.assertTrue(path.is_file())
        with Image.open(path) as image:
            self.assertLessEqual(max(image.size), 320)
            self.assertGreater(min(image.size), 0)

    def test_thumbnail_is_cached(self) -> None:
        first = self.service.get_thumbnail(self.asset)
        second = self.service.get_thumbnail(self.asset)
        self.assertEqual(first, second)
        with self.db.read() as conn:
            entries = store.cache_stats(conn)
        self.assertEqual(entries["thumb"]["entries"], 1)

    def test_proxy_matches_source_duration(self) -> None:
        result = self.service.get_proxy(self.asset)
        self.assertTrue(result.path.is_file())
        self.assertTrue(result.created)
        self.assertLessEqual(result.mapping_delta_ms or 0, 250)
        self.assertEqual(self.service.get_proxy(self.asset).created, False)

    def test_quota_eviction_never_touches_source(self) -> None:
        self.service.get_thumbnail(self.asset)
        self.service.get_proxy(self.asset)
        before_source = (self.video.stat().st_size, self.video.stat().st_mtime_ns)
        self.service.quota_bytes = 1
        result = self.service.enforce_quota()
        self.assertGreaterEqual(result["removed"], 1)
        self.assertEqual((self.video.stat().st_size, self.video.stat().st_mtime_ns), before_source)
        self.assertTrue(self.video.is_file())

    def test_cache_entries_are_recorded_with_sizes(self) -> None:
        path = self.service.get_thumbnail(self.asset)
        with self.db.read() as conn:
            entries = store.list_cache_entries(conn)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["path"], str(path))
        self.assertEqual(entries[0]["size_bytes"], path.stat().st_size)


if __name__ == "__main__":
    unittest.main()
