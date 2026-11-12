"""S05.4R.2 addendum: one-click auto analysis and persistent batch queue."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from phuthanhai.library.service import LibraryService
from tests.helpers import isolated_settings
from tests.media_fixtures import get


def wait_job(service: LibraryService, job_id: str, timeout: float = 60.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = service.get_job(job_id)
        if job["state"] not in {"queued", "running", "cancelling"}:
            return job
        time.sleep(0.1)
    raise AssertionError(f"job did not finish: {job_id}")


def wait_run(service: LibraryService, run_id: str, timeout: float = 90.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = service.batch.get_run(run_id)
        if run["status"] in {"complete", "partial", "failed", "cancelled", "paused", "waiting_for_memory"}:
            return run
        time.sleep(0.2)
    raise AssertionError(f"run did not settle: {run_id}")


class BatchBase(unittest.TestCase):
    clips = 3

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        footage = self.root / "footage"
        footage.mkdir()
        for index in range(self.clips):
            shutil.copy(get("cuts"), footage / f"clip{index:02d}.mp4")
        self._env = isolated_settings(self.root / "data")
        self.settings = self._env.__enter__()
        self.service = LibraryService(self.settings)
        self.project = self.service.create_project("batch", str(footage))
        self.service.scan(self.project["project_id"])
        os.environ["FAKE_VLM_MODE"] = "ok"
        self.assets = self.service.list_assets(self.project["project_id"])["items"]

    def tearDown(self) -> None:
        os.environ.pop("FAKE_VLM_MODE", None)
        self.service.close()
        self._env.__exit__(None, None, None)
        self._tmp.cleanup()


class AutoAnalysisTests(BatchBase):
    clips = 1

    def test_one_click_samples_then_analyzes_and_skips_existing(self) -> None:
        asset = self.assets[0]
        self.assertNotEqual(asset["sampling_status"], "complete")
        first = wait_job(self.service, self.service.start_auto_analysis(asset["asset_id"])["job_id"])
        self.assertEqual(first["state"], "complete")
        asset = self.service.get_asset(asset["asset_id"])
        self.assertEqual(asset["sampling_status"], "complete")
        summary = self.service.analysis_summary(asset["asset_id"])
        self.assertIn(summary["analysis_status"], {"complete", "partial"})
        started = time.monotonic()
        second = wait_job(self.service, self.service.start_auto_analysis(asset["asset_id"])["job_id"])
        self.assertEqual(second["state"], "complete")
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(self.service.get_asset(asset["asset_id"])["sampling_status"], "complete")

    def test_source_change_forces_new_sampling(self) -> None:
        asset = self.assets[0]
        wait_job(self.service, self.service.start_auto_analysis(asset["asset_id"])["job_id"])
        before = self.service.get_asset(asset["asset_id"])
        shutil.copy(get("long_nocut"), Path(before["path"]))
        wait_job(self.service, self.service.start_auto_analysis(asset["asset_id"])["job_id"])
        after = self.service.get_asset(asset["asset_id"])
        self.assertEqual(after["sampling_status"], "complete")
        self.assertNotEqual(after["sampling_fingerprint"], before["sampling_fingerprint"])


class BatchQueueTests(BatchBase):
    clips = 3

    def test_selected_batch_processes_all_and_skips_second_run(self) -> None:
        selected = [asset["asset_id"] for asset in self.assets[:2]]
        run = self.service.batch.create_run(self.project["project_id"], asset_ids=selected)
        final = wait_run(self.service, run["run_id"])
        self.assertIn(final["status"], {"complete", "partial"})
        self.assertEqual(final["total_items"], 2)
        self.assertEqual(final["counts"].get("complete", 0) + final["counts"].get("partial", 0), 2)
        second = self.service.batch.create_run(self.project["project_id"], asset_ids=selected, skip_existing=True)
        final2 = wait_run(self.service, second["run_id"])
        self.assertEqual(final2["counts"].get("skipped", 0), 2)
        self.assertEqual(len({item["asset_id"] for item in self.service.batch.list_items(second["run_id"])}), 2)

    def test_entire_project_snapshot_uses_all_asset_ids_not_pagination(self) -> None:
        run = self.service.batch.create_run(self.project["project_id"], asset_ids=None)
        self.assertEqual(run["total_items"], 3)
        items = self.service.batch.list_items(run["run_id"])
        self.assertEqual(len(items), 3)
        self.assertEqual({item["asset_id"] for item in items}, {asset["asset_id"] for asset in self.assets})
        self.service.batch.cancel(run["run_id"])

    def test_pause_and_resume(self) -> None:
        selected = [asset["asset_id"] for asset in self.assets]
        run = self.service.batch.create_run(self.project["project_id"], asset_ids=selected)
        # Wait until an item is active, then pause.
        for _ in range(100):
            if self.service.batch.get_run(run["run_id"])["status"] == "running":
                break
            time.sleep(0.05)
        self.service.batch.pause(run["run_id"])
        paused = wait_run(self.service, run["run_id"])
        self.assertEqual(paused["status"], "paused")
        self.service.batch.resume(run["run_id"])
        final = wait_run(self.service, run["run_id"])
        self.assertIn(final["status"], {"complete", "partial"})
        self.assertGreaterEqual(final["counts"].get("complete", 0), 1)

    def test_cancel_marks_run_and_items_and_reaps(self) -> None:
        run = self.service.batch.create_run(self.project["project_id"], asset_ids=[a["asset_id"] for a in self.assets])
        self.service.batch.cancel(run["run_id"])
        final = wait_run(self.service, run["run_id"])
        self.assertEqual(final["status"], "cancelled")
        statuses = {item["status"] for item in self.service.batch.list_items(run["run_id"])}
        self.assertTrue(statuses <= {"cancelled", "complete", "partial", "failed"})

    def test_restart_reconciles_running_items_without_claiming_complete(self) -> None:
        run = self.service.batch.create_run(self.project["project_id"], asset_ids=[self.assets[0]["asset_id"]])
        self.service.batch.cancel(run["run_id"])
        with self.service.db.write() as conn:
            conn.execute("UPDATE analysis_runs SET status='running' WHERE run_id=?", (run["run_id"],))
            conn.execute("UPDATE analysis_run_items SET status='running' WHERE run_id=?", (run["run_id"],))
        self.service.batch._reconcile()
        reconciled = self.service.batch.get_run(run["run_id"])
        self.assertEqual(reconciled["status"], "paused")
        self.assertEqual(self.service.batch.list_items(run["run_id"])[0]["status"], "queued")

    def test_concurrent_creators_cannot_claim_the_same_asset(self) -> None:
        asset_id = self.assets[0]["asset_id"]
        contenders = 20
        barrier = threading.Barrier(contenders)

        def aligned_busy_check(_asset_id: str) -> bool:
            barrier.wait(timeout=10)
            return False

        def create(index: int) -> str:
            try:
                run = self.service.batch.create_run(
                    self.project["project_id"], asset_ids=[asset_id], idempotency_key=f"race-{index}"
                )
                return run["run_id"]
            except Exception as exc:
                return str(getattr(exc, "code", type(exc).__name__))

        with mock.patch.object(self.service.batch, "_start"), mock.patch.object(
            self.service, "is_asset_busy", side_effect=aligned_busy_check
        ), ThreadPoolExecutor(max_workers=contenders) as pool:
            outcomes = list(pool.map(create, range(contenders)))

        run_ids = [value for value in outcomes if value != "ASSET_ALREADY_CLAIMED"]
        self.assertEqual(len(run_ids), 1)
        self.assertEqual(outcomes.count("ASSET_ALREADY_CLAIMED"), contenders - 1)

    def test_retry_is_rejected_while_run_is_active(self) -> None:
        with mock.patch.object(self.service.batch, "_start"):
            run = self.service.batch.create_run(
                self.project["project_id"], asset_ids=[self.assets[0]["asset_id"]]
            )
            with self.assertRaises(Exception) as ctx:
                self.service.batch.retry_failed(run["run_id"])
        self.assertEqual(getattr(ctx.exception, "code", None), "BATCH_NOT_TERMINAL")

    def test_cancel_fences_stale_worker_completion(self) -> None:
        with mock.patch.object(self.service.batch, "_start"):
            run = self.service.batch.create_run(
                self.project["project_id"], asset_ids=[self.assets[0]["asset_id"]]
            )
        item = self.service.batch.list_items(run["run_id"])[0]
        with self.service.db.write() as conn:
            conn.execute("UPDATE analysis_runs SET status='running' WHERE run_id=?", (run["run_id"],))
            conn.execute("UPDATE analysis_run_items SET status='running' WHERE item_id=?", (item["item_id"],))
        self.service.batch.cancel(run["run_id"])
        self.service.batch._update_run(run["run_id"], status="complete")
        with self.service.db.write() as conn:
            conn.execute(
                "UPDATE analysis_run_items SET status='complete' WHERE item_id=? AND status='running'",
                (item["item_id"],),
            )
        self.assertEqual(self.service.batch.get_run(run["run_id"])["status"], "cancelled")
        self.assertEqual(self.service.batch.list_items(run["run_id"])[0]["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()


class ProjectSnapshot54Tests(unittest.TestCase):
    def test_entire_project_snapshots_all_54_asset_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            footage = root / "footage"
            footage.mkdir()
            source = get("cuts")
            for index in range(54):
                shutil.copy(source, footage / f"clip{index:02d}.mp4")
            env = isolated_settings(root / "data")
            settings = env.__enter__()
            service = LibraryService(settings)
            try:
                project = service.create_project("snapshot54", str(footage))
                service.scan(project["project_id"])
                os.environ["FAKE_VLM_MODE"] = "ok"
                run = service.batch.create_run(project["project_id"], asset_ids=None)
                self.assertEqual(run["total_items"], 54)
                items = service.batch.list_items(run["run_id"])
                self.assertEqual(len(items), 54)
                self.assertEqual(len({item["asset_id"] for item in items}), 54)
                service.batch.cancel(run["run_id"])
            finally:
                os.environ.pop("FAKE_VLM_MODE", None)
                service.close()
                env.__exit__(None, None, None)
