"""Runtime tests: atomic writes, checkpoints, heavy slot, process reaping, resources."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from phuthanhai.runtime.checkpoint import (
    Checkpoint,
    CheckpointMismatch,
    append_events,
    read_checkpoint,
    write_checkpoint,
)
from phuthanhai.runtime.resource import GOVERNOR, snapshot
from phuthanhai.runtime.scheduler import HeavySlot, HeavySlotBusy, ProcessRegistry
from phuthanhai.utils.fs import FsError, atomic_write_json, atomic_write_text, read_json_strict


class AtomicWriteTests(unittest.TestCase):
    def test_roundtrip_and_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "file.json"
            atomic_write_json(path, {"b": 1, "a": "xin chào"})
            self.assertEqual(read_json_strict(path), {"a": "xin chào", "b": 1})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_duplicate_keys_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dup.json"
            path.write_text('{"a": 1, "a": 2}', encoding="utf-8")
            with self.assertRaises(FsError):
                read_json_strict(path)

    def test_missing_file_raises_typed_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FsError) as ctx:
                read_json_strict(Path(tmp) / "absent.json")
            self.assertEqual(ctx.exception.code, "FS_ERROR")

    def test_text_write_is_atomic_and_utf8(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "note.txt"
            atomic_write_text(path, "Lên phà lúc 5 giờ sáng")
            self.assertEqual(path.read_text(encoding="utf-8"), "Lên phà lúc 5 giờ sáng")


class CheckpointTests(unittest.TestCase):
    FINGERPRINT = {"source_sha256": "a" * 64, "model_snapshot": "qwen3-vl-4b", "prompt_version": "v1"}

    def test_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "checkpoint.json"
            checkpoint = Checkpoint(job_id="job-1", stage="sampling", fingerprint=self.FINGERPRINT)
            write_checkpoint(path, checkpoint)
            restored = read_checkpoint(path, expected_fingerprint=self.FINGERPRINT)
            self.assertEqual(restored.job_id, "job-1")
            self.assertEqual(restored.stage, "sampling")

    def test_fingerprint_mismatch_is_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "checkpoint.json"
            write_checkpoint(path, Checkpoint(job_id="job-1", stage="sampling", fingerprint=self.FINGERPRINT))
            changed = dict(self.FINGERPRINT, source_sha256="b" * 64)
            with self.assertRaises(CheckpointMismatch):
                read_checkpoint(path, expected_fingerprint=changed)

    def test_unknown_schema_version_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "checkpoint.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": "9.9.9",
                        "job_id": "j",
                        "stage": "s",
                        "fingerprint": {},
                        "committed_frame_ids": [],
                        "payload": {},
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(Exception):
                read_checkpoint(path)

    def test_event_dedup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            seen: set[str] = set()
            first = append_events(
                path,
                [{"dedup_key": "job-1:f0001:sampling", "stage": "sampling"}],
                dedup_seen=seen,
            )
            second = append_events(
                path,
                [{"dedup_key": "job-1:f0001:sampling", "stage": "sampling"}],
                dedup_seen=seen,
            )
            self.assertEqual((first, second), (1, 0))
            self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 1)


class HeavySlotTests(unittest.TestCase):
    def test_only_one_owner(self) -> None:
        slot = HeavySlot()
        self.assertTrue(slot.try_acquire("vlm-1"))
        self.assertFalse(slot.try_acquire("vlm-2"))
        self.assertTrue(slot.busy)
        slot.release("vlm-1")
        self.assertFalse(slot.busy)
        self.assertTrue(slot.try_acquire("llm-1"))
        slot.release()

    def test_release_by_wrong_owner_is_refused(self) -> None:
        slot = HeavySlot()
        slot.try_acquire("vlm-1")
        with self.assertRaises(HeavySlotBusy):
            slot.release("other")
        slot.release("vlm-1")

    def test_concurrent_attempts_never_yield_two_winners(self) -> None:
        slot = HeavySlot()
        winners: list[str] = []
        barrier = threading.Barrier(8)

        def attempt(name: str) -> None:
            barrier.wait()
            if slot.try_acquire(name):
                winners.append(name)
                time.sleep(0.15)
                slot.release(name)

        threads = [threading.Thread(target=attempt, args=(f"t{i}",)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(winners), 1, f"more than one heavy owner: {winners}")

    def test_lock_is_machine_wide_across_processes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "heavy.lock"
            ready = Path(tmp) / "ready"
            code = (
                "import sys,time; from pathlib import Path; "
                "from phuthanhai.runtime.scheduler import HeavySlot; "
                "slot=HeavySlot(lock_path=Path(sys.argv[1])); slot.acquire('child'); "
                "Path(sys.argv[2]).write_text('ready'); time.sleep(5)"
            )
            child = subprocess.Popen([sys.executable, "-c", code, str(lock_path), str(ready)])
            try:
                deadline = time.monotonic() + 3
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(ready.exists(), "child did not acquire the test slot")
                contender = HeavySlot(lock_path=lock_path)
                self.assertFalse(contender.try_acquire("parent"))
            finally:
                child.terminate()
                child.wait(timeout=5)
            recovered = HeavySlot(lock_path=lock_path)
            self.assertTrue(recovered.try_acquire("after-crash"))
            recovered.release("after-crash")


class ProcessRegistryTests(unittest.TestCase):
    def test_captures_stdout_stderr_and_rss(self) -> None:
        registry = ProcessRegistry()
        for _ in range(5):
            capture = registry.run_captured(
                [sys.executable, "-c", "import sys;print('out');print('err', file=sys.stderr)"],
                timeout_s=30,
            )
            self.assertEqual(capture.returncode, 0)
            self.assertIn("out", capture.stdout)
            self.assertIn("err", capture.stderr)
            self.assertIsNotNone(capture.peak_rss_bytes)
            self.assertFalse(capture.timed_out)

    def test_timeout_terminates_the_child(self) -> None:
        registry = ProcessRegistry()
        capture = registry.run_captured(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            timeout_s=0.6,
        )
        self.assertTrue(capture.timed_out)
        self.assertNotEqual(capture.returncode, 0)
        self.assertEqual(registry.active_count, 0)

    def test_shutdown_reaps_running_child(self) -> None:
        registry = ProcessRegistry()
        popen = registry.spawn([sys.executable, "-c", "import time; time.sleep(30)"])
        self.assertIsNone(popen.poll())
        terminated = registry.shutdown()
        self.assertIn(popen.pid, terminated)
        self.assertIsNotNone(popen.poll())
        self.assertEqual(registry.active_count, 0)

    def test_environment_is_passed_explicitly(self) -> None:
        registry = ProcessRegistry()
        capture = registry.run_captured(
            [sys.executable, "-c", "import os;print(os.environ.get('PHUTHANHAI_TEST_FLAG'))"],
            env={**os.environ, "PHUTHANHAI_TEST_FLAG": "1"},
        )
        self.assertEqual(capture.stdout.strip(), "1")


class ResourceTests(unittest.TestCase):
    def test_snapshot_contains_measured_values(self) -> None:
        facts = snapshot()
        self.assertIn(facts.level, {"normal", "pressured", "critical", "unknown"})
        self.assertGreater(facts.disk_total_bytes, 0)
        self.assertIsNotNone(facts.total_bytes)
        self.assertTrue(facts.thresholds_provisional)

    def test_governor_reports_decision(self) -> None:
        decision, code = GOVERNOR.can_start_heavy_worker()
        self.assertIn(decision, {True, False})
        self.assertIn(code, {"OK", "STORAGE_FULL", "MEMORY_CRITICAL", "MEMORY_PRESSURED"})


if __name__ == "__main__":
    unittest.main()
