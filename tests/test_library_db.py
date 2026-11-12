"""S01 data-layer tests: WAL, migrations, natural ordering, backup, revisions."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from phuthanhai.library import store
from phuthanhai.library import db as db_module
from phuthanhai.library.db import SCHEMA_VERSION, Database, MigrationError, natural_key


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "registry.sqlite3"
        self.db = Database(self.path)

    def tearDown(self) -> None:
        self.db.close_current_thread()
        self._tmp.cleanup()

    def test_migrate_is_idempotent(self) -> None:
        self.assertEqual(self.db.migrate(), SCHEMA_VERSION)
        self.assertEqual(self.db.migrate(), SCHEMA_VERSION)
        self.assertEqual(self.db.schema_version(), SCHEMA_VERSION)

    def test_future_schema_is_rejected(self) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")

        with self.assertRaises(MigrationError):
            self.db.migrate()

    def test_v6_to_v7_enforces_one_active_claim_per_asset(self) -> None:
        with (
            mock.patch.object(db_module, "SCHEMA_VERSION", 6),
            mock.patch.object(db_module, "MIGRATIONS", db_module.MIGRATIONS[:6]),
        ):
            self.assertEqual(self.db.migrate(), 6)

        project_id = "project-1"
        asset_id = "asset-1"
        now = "2026-11-12T00:00:00+00:00"
        with self.db.write() as conn:
            conn.execute(
                "INSERT INTO projects(project_id,name,root_path,created_at,updated_at) VALUES (?,?,?,?,?)",
                (project_id, "Migration", "/tmp/migration", now, now),
            )
            store.upsert_asset(
                conn,
                asset_id=asset_id,
                project_id=project_id,
                path="/tmp/migration/clip.mp4",
                filename="clip.mp4",
                size_bytes=1,
                mtime_ns=1,
                dev=1,
                ino=1,
                duration_ms=1,
                width=1,
                height=1,
                rotation=0,
                fps_nominal=None,
                codec="h264",
                has_audio=False,
                vfr_hint=False,
                color_transfer=None,
            )

        self.assertEqual(self.db.migrate(), SCHEMA_VERSION)
        self.assertEqual(self.db.migrate(), SCHEMA_VERSION)
        with self.db.write() as conn:
            for run_id in ("run-1", "run-2"):
                conn.execute(
                    "INSERT INTO analysis_runs(run_id,project_id,status,config_json,total_items,created_at,updated_at) "
                    "VALUES (?,?,'queued','{}',1,?,?)",
                    (run_id, project_id, now, now),
                )
            conn.execute(
                "INSERT INTO analysis_run_items(item_id,run_id,project_id,asset_id,position,status,updated_at) "
                "VALUES ('item-1','run-1',?,?,0,'running',?)",
                (project_id, asset_id, now),
            )

        with self.assertRaises(sqlite3.IntegrityError):
            with self.db.write() as conn:
                conn.execute(
                    "INSERT INTO analysis_run_items(item_id,run_id,project_id,asset_id,position,status,updated_at) "
                    "VALUES ('item-2','run-2',?,?,0,'waiting_for_memory',?)",
                    (project_id, asset_id, now),
                )

        with self.db.write() as conn:
            conn.execute("UPDATE analysis_run_items SET status='complete' WHERE item_id='item-1'")
            conn.execute(
                "INSERT INTO analysis_run_items(item_id,run_id,project_id,asset_id,position,status,updated_at) "
                "VALUES ('item-2','run-2',?,?,0,'queued',?)",
                (project_id, asset_id, now),
            )

        conn = self.db.connection()
        self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_wal_mode_and_foreign_keys(self) -> None:
        self.db.migrate()
        conn = self.db.connection()
        self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
        self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertGreaterEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 1000)

    def test_expected_tables_exist(self) -> None:
        self.db.migrate()
        names = {
            row[0]
            for row in self.db.connection().execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        for table in ("projects", "assets", "annotations", "review_overlays", "jobs", "cache_entries", "settings"):
            self.assertIn(table, names)

    def test_project_asset_roundtrip(self) -> None:
        self.db.migrate()
        with self.db.write() as conn:
            project = store.create_project(conn, name="Demo", root_path="/tmp/footage")
        with self.db.write() as conn:
            store.upsert_asset(
                conn,
                asset_id="a" * 36,
                project_id=project["project_id"],
                path="/tmp/footage/C0015.MOV",
                filename="C0015.MOV",
                size_bytes=10,
                mtime_ns=1,
                dev=1,
                ino=2,
                duration_ms=42000,
                width=1920,
                height=1080,
                rotation=0,
                fps_nominal="25/1",
                codec="h264",
                has_audio=True,
                vfr_hint=False,
                color_transfer="bt709",
            )
            asset = store.get_asset(conn, "a" * 36)
        self.assertEqual(asset["filename"], "C0015.MOV")
        self.assertTrue(asset["has_audio"])

    def test_foreign_key_violation_is_rejected(self) -> None:
        self.db.migrate()
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db.write() as conn:
                store.upsert_asset(
                    conn,
                    asset_id="b" * 36,
                    project_id="missing-project",
                    path="/tmp/x.mp4",
                    filename="x.mp4",
                    size_bytes=1,
                    mtime_ns=1,
                    dev=1,
                    ino=1,
                    duration_ms=1,
                    width=1,
                    height=1,
                    rotation=0,
                    fps_nominal=None,
                    codec="h264",
                    has_audio=False,
                    vfr_hint=False,
                    color_transfer=None,
                )

    def test_backup_produces_readable_copy(self) -> None:
        self.db.migrate()
        with self.db.write() as conn:
            store.create_project(conn, name="Backup", root_path="/tmp/b")
        target = Path(self._tmp.name) / "backup.sqlite3"
        self.db.backup_to(target)
        copy = sqlite3.connect(str(target))
        try:
            count = copy.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
        finally:
            copy.close()
        self.assertEqual(count, 1)


class NaturalKeyTests(unittest.TestCase):
    def test_numeric_suffix_orders_naturally(self) -> None:
        names = ["C0015.MOV", "C0016.MOV", "C0009.MOV", "C0100.MOV"]
        ordered = sorted(names, key=natural_key)
        self.assertEqual(ordered, ["C0009.MOV", "C0015.MOV", "C0016.MOV", "C0100.MOV"])

    def test_dji_style_names(self) -> None:
        names = ["DJI_0010.MP4", "DJI_0002.MP4", "DJI_0100.MP4"]
        self.assertEqual(sorted(names, key=natural_key), ["DJI_0002.MP4", "DJI_0010.MP4", "DJI_0100.MP4"])

    def test_no_numeric_padding_breaks(self) -> None:
        self.assertLess(natural_key("IMG_9.MOV"), natural_key("IMG_10.MOV"))


if __name__ == "__main__":
    unittest.main()
