"""S01 performance guards: 1,000 assets, keyset paging, search, note save latency."""

from __future__ import annotations

import statistics
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from phuthanhai.library import store
from phuthanhai.library.db import Database
from phuthanhai.library.db import natural_key


def percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, int(round((p / 100) * (len(ordered) - 1)))))
    return ordered[index]


class ThousandAssetTests(unittest.TestCase):
    COUNT = 1000

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        cls.db = Database(root / "registry.sqlite3")
        cls.db.migrate()
        with cls.db.write() as conn:
            project = store.create_project(conn, name="Bench", root_path=str(root / "footage"))
        cls.project_id = project["project_id"]

        started = time.monotonic()
        with cls.db.write() as conn:
            for index in range(1, cls.COUNT + 1):
                filename = f"C_{index:04d}.MOV"
                store.upsert_asset(
                    conn,
                    asset_id=str(uuid.uuid4()),
                    project_id=cls.project_id,
                    path=f"/bench/{filename}",
                    filename=filename,
                    size_bytes=1000 + index,
                    mtime_ns=index,
                    dev=None,
                    ino=None,
                    duration_ms=10_000 + index,
                    width=1920,
                    height=1080,
                    rotation=0,
                    fps_nominal="25/1",
                    codec="h264",
                    has_audio=True,
                    vfr_hint=False,
                    color_transfer="bt709",
                )
        cls.insert_seconds = time.monotonic() - started

    @classmethod
    def tearDownClass(cls) -> None:
        cls.db.close_current_thread()
        cls._tmp.cleanup()

    def test_insert_throughput_is_recorded(self) -> None:
        self.assertLess(self.insert_seconds, 30, "inserting 1,000 rows must not take 30s")
        print(f"\n[perf] insert {self.COUNT} assets: {self.insert_seconds:.3f}s")

    def test_first_page_and_keyset_paging_p95(self) -> None:
        timings: list[float] = []
        with self.db.read() as conn:
            for _ in range(20):
                started = time.perf_counter()
                rows, cursor, total = store.list_assets(conn, project_id=self.project_id, limit=100)
                timings.append((time.perf_counter() - started) * 1000)
        self.assertEqual(total, self.COUNT)
        self.assertEqual(len(rows), 100)
        self.assertIsNotNone(cursor)
        p95 = percentile(timings, 95)
        print(f"\n[perf] list first page p50={statistics.median(timings):.1f}ms p95={p95:.1f}ms")
        self.assertLess(p95, 250, "first page p95 must stay under 250 ms")

    def test_deep_keyset_paging_is_flat(self) -> None:
        with self.db.read() as conn:
            rows, cursor, _ = store.list_assets(conn, project_id=self.project_id, limit=100)
            seen = len(rows)
            pages = 1
            started = time.perf_counter()
            while cursor and pages < 10:
                rows, cursor, _ = store.list_assets(
                    conn, project_id=self.project_id, limit=100, cursor=store.decode_cursor(cursor)
                )
                seen += len(rows)
                pages += 1
            elapsed = (time.perf_counter() - started) * 1000
        self.assertEqual(seen, 1000)
        print(f"\n[perf] 10 keyset pages ({seen} rows): {elapsed:.1f}ms")
        self.assertLess(elapsed, 1000)

    def test_search_p95(self) -> None:
        timings: list[float] = []
        with self.db.read() as conn:
            for _ in range(20):
                started = time.perf_counter()
                rows, _, total = store.list_assets(conn, project_id=self.project_id, q="C_05", limit=100)
                timings.append((time.perf_counter() - started) * 1000)
        self.assertGreater(total, 0)
        p95 = percentile(timings, 95)
        print(f"\n[perf] search p50={statistics.median(timings):.1f}ms p95={p95:.1f}ms")
        self.assertLess(p95, 300)

    def test_note_save_p95(self) -> None:
        with self.db.read() as conn:
            asset_id = conn.execute("SELECT asset_id FROM assets LIMIT 1").fetchone()["asset_id"]
        with self.db.write() as conn:
            note = store.create_annotation(
                conn,
                annotation_id=str(uuid.uuid4()),
                project_id=self.project_id,
                asset_id=asset_id,
                scope="clip",
                start_ms=None,
                end_ms=None,
                text="initial",
            )
        revision = note["revision"]
        timings: list[float] = []
        for index in range(20):
            started = time.perf_counter()
            with self.db.write() as conn:
                store.update_annotation(
                    conn,
                    annotation_id=note["annotation_id"],
                    expected_revision=revision,
                    text=f"note {index}",
                    note_type="context",
                    start_ms=None,
                    end_ms=None,
                    wall_clock=None,
                )
            timings.append((time.perf_counter() - started) * 1000)
            revision += 1
        p95 = percentile(timings, 95)
        print(f"\n[perf] note save p50={statistics.median(timings):.1f}ms p95={p95:.1f}ms")
        self.assertLess(p95, 150)

    def test_natural_ordering_across_thousand_rows(self) -> None:
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT filename FROM assets ORDER BY sort_key LIMIT 12"
            ).fetchall()
        names = [row["filename"] for row in rows]
        self.assertEqual(names[0], "C_0001.MOV")
        self.assertEqual(names[11], "C_0012.MOV")
        self.assertEqual(names, sorted(names, key=natural_key))


if __name__ == "__main__":
    unittest.main()
