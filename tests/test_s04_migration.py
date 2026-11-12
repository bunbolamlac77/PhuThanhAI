"""S04: project profile and label persistence survive restart; S03 data is intact."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from phuthanhai.analyzer.context_fusion import raw_evidence_digest
from phuthanhai.analyzer.vision_service import VisionService
from phuthanhai.config import Settings
from phuthanhai.library.db import SCHEMA_VERSION
from phuthanhai.library.service import LibraryService
from tests.helpers import isolated_settings
from tests.media_fixtures import get


class PersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.footage = self.root / "footage"
        self.footage.mkdir()
        shutil.copy(get("cuts"), self.footage / "clip.mp4")
        self._env = isolated_settings(self.root / "data")
        self.settings = self._env.__enter__()
        self.service = LibraryService(self.settings)
        project = self.service.create_project("S04", str(self.footage))
        self.project_id = project["project_id"]
        self.service.scan(self.project_id)
        self.asset = self.service.list_assets(self.project_id, limit=5)["items"][0]
        self.service.pipeline.run(self.asset, Path(self.asset["path"]), profile="quick")
        self.asset = self.service.get_asset(self.asset["asset_id"])
        model = self.root / "model"
        model.mkdir()
        (model / "config.json").write_text("{}", encoding="utf-8")
        (model / "model.safetensors").write_bytes(b"stub")
        self.service.vision = VisionService(
            self.service.db,
            self.settings,
            runtime_python=Path(sys.executable),
            worker_module="tests.fake_vlm_worker",
            model_path=model,
            request_timeout_s=10,
            batch_size=8,
            require_governor=False,
        )
        with mock.patch.dict(os.environ, {"FAKE_VLM_MODE": "ok"}):
            self.service.vision.analyze(self.service.get_asset(self.asset["asset_id"]), Path(self.asset["path"]))

    def tearDown(self) -> None:
        self.service.close()
        self._env.__exit__(None, None, None)
        self._tmp.cleanup()

    def test_profile_note_and_label_survive_reopen(self) -> None:
        project = self.service.set_project_profile(self.project_id, "wedding_traditional", 1)
        self.assertEqual(project["analysis_profile"], "wedding_traditional")
        note = self.service.create_annotation(
            self.project_id, {"scope": "clip", "asset_id": self.asset["asset_id"], "text": "Ghi chú riêng tư"}
        )
        overlay = self.service.context_overlay(self.asset["asset_id"])
        digest = overlay["raw_evidence_sha256"]
        self.service.confirm_label(
            self.asset["asset_id"],
            label_key="ancestor_altar",
            label_vi="Bàn gia tiên",
            status="confirmed",
            scene_id=overlay["scenes"][0]["scene_id"],
        )
        self.service.close()

        with mock.patch.dict(os.environ, {"FAKE_VLM_MODE": "ok"}):
            reopened = LibraryService(Settings.load())
            try:
                project = reopened.get_project(self.project_id)
                self.assertEqual(project["analysis_profile"], "wedding_traditional")
                self.assertEqual(project["profile_revision"], 2)
                notes = reopened.list_annotations(self.project_id)["items"] if isinstance(reopened.list_annotations(self.project_id), dict) else reopened.list_annotations(self.project_id)
                self.assertTrue(any(item["annotation_id"] == note["annotation"]["annotation_id"] for item in notes))
                reopened_asset = reopened.get_asset(self.asset["asset_id"])
                self.assertEqual(reopened_asset["sampling_status"], "complete")
                fresh_overlay = reopened.context_overlay(self.asset["asset_id"])
                self.assertEqual(fresh_overlay["raw_evidence_sha256"], digest)
                self.assertEqual(len(fresh_overlay["user_labels"]), 1)
                self.assertEqual(fresh_overlay["user_labels"][0]["label_vi"], "Bàn gia tiên")
                with reopened.db.read() as conn:
                    version = conn.execute("PRAGMA user_version").fetchone()[0]
                self.assertEqual(version, SCHEMA_VERSION)
                self.service = reopened
            finally:
                pass


if __name__ == "__main__":
    unittest.main()
