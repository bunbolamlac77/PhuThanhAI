"""S05 P0: real S03 (v4) database migrates to v5 with backup/restore."""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from phuthanhai.library import db as db_module
from phuthanhai.library.db import Database, MigrationError

REPO = Path(__file__).resolve().parents[1]


def _s03_db_module():
    source = subprocess.check_output(
        ["git", "show", "7306ed5:src/phuthanhai/library/db.py"], cwd=REPO, text=True
    )
    namespace = {"__name__": "s03_db", "__file__": str(REPO / "src/phuthanhai/library/db.py")}
    exec(compile(source, "s03_db.py", "exec"), namespace)
    return namespace


def make_v4_database(path: Path) -> dict:
    old = _s03_db_module()
    old_db = old["Database"](path)
    old_db.migrate()
    old_db.close_current_thread()
    project_id = str(uuid.uuid4())
    asset_id = str(uuid.uuid4())
    annotation_id = str(uuid.uuid4())
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO projects(project_id,name,root_path,created_at,updated_at,revision) "
            "VALUES (?,?,?,?,?,?)",
            (project_id, "S03 project", "/tmp/s03", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00", 3),
        )
        conn.execute(
            "INSERT INTO assets(asset_id,project_id,path,filename,sort_key,size_bytes,mtime_ns,"
            "duration_ms,width,height,sha256,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (asset_id, project_id, "/tmp/s03/clip.mp4", "clip.mp4", "clip", 1234, 1700000000000000000,
             5000, 360, 640, "a" * 64, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO annotations(annotation_id,project_id,asset_id,scope,text,note_type,source,"
            "assertion_status,revision,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (annotation_id, project_id, asset_id, "clip", "Ghi chú S03", "context", "user_annotation",
             "user_asserted", 2, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO review_overlays(asset_id,project_id,status,rating,favorite,labels,"
            "selected_ranges,last_playhead_ms,revision,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (asset_id, project_id, "reviewed", 5, 1, json.dumps(["wedding"]), "[]", 1234, 1,
             "2026-01-01T00:00:00+00:00"),
        )
    return {"project_id": project_id, "asset_id": asset_id, "annotation_id": annotation_id}


class RealMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.path = self.root / "registry.sqlite3"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_v4_to_v5_preserves_ids_notes_reviews_and_adds_general_profile(self) -> None:
        ids = make_v4_database(self.path)
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 4)

        database = Database(self.path)
        self.assertEqual(database.migrate(), 6)
        self.assertEqual(database.migrate(), 6)  # idempotent
        with database.read() as conn:
            project = conn.execute("SELECT * FROM projects WHERE project_id=?", (ids["project_id"],)).fetchone()
            note = conn.execute("SELECT * FROM annotations WHERE annotation_id=?", (ids["annotation_id"],)).fetchone()
            review = conn.execute("SELECT * FROM review_overlays WHERE asset_id=?", (ids["asset_id"],)).fetchone()
            asset = conn.execute("SELECT * FROM assets WHERE asset_id=?", (ids["asset_id"],)).fetchone()
        self.assertEqual(project["project_id"], ids["project_id"])
        self.assertEqual(project["name"], "S03 project")
        self.assertEqual(project["analysis_profile"], "general")
        self.assertEqual(project["profile_revision"], 1)
        self.assertEqual(note["text"], "Ghi chú S03")
        self.assertEqual(note["revision"], 2)
        self.assertEqual(review["rating"], 5)
        self.assertEqual(review["favorite"], 1)
        self.assertEqual(json.loads(review["labels"]), ["wedding"])
        self.assertEqual(asset["sha256"], "a" * 64)
        database.close_current_thread()

    def test_failed_migration_restores_backup(self) -> None:
        ids = make_v4_database(self.path)
        def explode(conn):
            conn.execute("ALTER TABLE projects ADD COLUMN should_not_survive TEXT")
            raise RuntimeError("simulated migration failure")

        with mock.patch.object(db_module, "MIGRATIONS", (*db_module.MIGRATIONS, explode)):
            database = Database(self.path)
            with self.assertRaises(MigrationError) as ctx:
                database.migrate()
            self.assertIsNotNone(ctx.exception.backup)
            self.assertTrue(Path(ctx.exception.backup).is_file())
            database.close_current_thread()

        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 4)
            self.assertEqual(conn.execute("SELECT name FROM projects").fetchone()[0], "S03 project")
            columns = [row[1] for row in conn.execute("PRAGMA table_info(projects)")]
            self.assertNotIn("should_not_survive", columns)
            self.assertNotIn("analysis_profile", columns)


if __name__ == "__main__":
    unittest.main()
