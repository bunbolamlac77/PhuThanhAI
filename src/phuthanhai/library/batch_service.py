"""Persistent batch analysis orchestration (one heavy worker at a time)."""

from __future__ import annotations

import json
import threading
import time
import uuid
from typing import Any

from phuthanhai.library import store

RUN_ACTIVE = {"queued", "running", "paused", "waiting_for_memory"}
ITEM_ACTIVE = {"queued", "running", "waiting_for_memory"}


def aggregate_run_status(current_status: str, counts: dict[str, int]) -> str:
    """Derive lifecycle status without confusing execution with coverage."""

    if current_status == "cancelled":
        return "cancelled"
    if counts.get("waiting_for_memory", 0) or (
        current_status == "waiting_for_memory" and counts.get("queued", 0)
    ):
        return "waiting_for_memory"
    if current_status == "paused" and (
        counts.get("queued", 0) or counts.get("running", 0)
    ):
        return "paused"
    if counts.get("running", 0):
        return "running"
    if counts.get("queued", 0):
        return "running" if current_status == "running" else "queued"

    partial = counts.get("partial", 0)
    failed = counts.get("failed", 0)
    cancelled = counts.get("cancelled", 0)
    successful = counts.get("complete", 0) + counts.get("skipped", 0)
    if partial or cancelled or (failed and successful):
        return "partial"
    if failed:
        return "failed"
    return "complete"


class BatchAnalysisService:
    def __init__(self, service: Any) -> None:
        self.service = service
        self.db = service.db
        self._lock = threading.RLock()
        self._run_lock = threading.Lock()
        self._threads: dict[str, threading.Thread] = {}
        self._cancels: dict[str, threading.Event] = {}
        self._shutdown = threading.Event()
        self._reconcile()

    # -- lifecycle --------------------------------------------------------- #
    def _reconcile(self) -> None:
        """A restart must never claim an in-flight run completed."""
        with self.db.write() as conn:
            conn.execute(
                "UPDATE analysis_runs SET status='paused', updated_at=? "
                "WHERE status IN ('running','queued')",
                (store.utc_now_iso(),),
            )
            conn.execute(
                "UPDATE analysis_run_items SET status='queued', error_code='INTERRUPTED', "
                "updated_at=? WHERE status='running'",
                (store.utc_now_iso(),),
            )

    def close(self) -> None:
        self._shutdown.set()
        for event in list(self._cancels.values()):
            event.set()
        for thread in list(self._threads.values()):
            thread.join(timeout=5)

    def is_asset_active(self, asset_id: str) -> bool:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT 1 FROM analysis_run_items WHERE asset_id=? AND status IN "
                "('queued','running','waiting_for_memory') LIMIT 1",
                (asset_id,),
            ).fetchone()
        return row is not None

    # -- API --------------------------------------------------------------- #
    def create_run(
        self,
        project_id: str,
        *,
        asset_ids: list[str] | None = None,
        selection_mode: str = "smart",
        sampling_profile: str = "detailed",
        skip_existing: bool = True,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if selection_mode not in {"smart", "balanced", "full"}:
            raise store.StoreError("SELECTION_MODE_INVALID", f"unknown selection mode: {selection_mode}")
        if sampling_profile not in {"quick", "detailed", "thorough"}:
            raise store.StoreError("SAMPLING_PROFILE_INVALID", f"unknown sampling profile: {sampling_profile}")
        self.service.get_project(project_id)
        with self.db.read() as conn:
            if asset_ids is None:
                rows = conn.execute(
                    "SELECT asset_id FROM assets WHERE project_id=? ORDER BY filename",
                    (project_id,),
                ).fetchall()
                ids = [str(row[0]) for row in rows]
            else:
                ids = list(dict.fromkeys(str(item) for item in asset_ids if str(item)))
                if not ids:
                    raise store.StoreError("BATCH_EMPTY", "no assets selected", status=400)
                placeholders = ",".join("?" for _ in ids)
                valid = conn.execute(
                    f"SELECT asset_id FROM assets WHERE project_id=? AND asset_id IN ({placeholders})",
                    (project_id, *ids),
                ).fetchall()
                valid_ids = {str(row[0]) for row in valid}
                missing = [item for item in ids if item not in valid_ids]
                if missing:
                    raise store.StoreError("ASSET_NOT_IN_PROJECT", "asset is not owned by this project", status=400)
            if not ids:
                raise store.StoreError("BATCH_EMPTY", "project has no assets", status=400)
            if len(ids) > 1000:
                raise store.StoreError("BATCH_TOO_LARGE", "batch is limited to 1000 assets", status=400)
            if idempotency_key:
                existing = conn.execute(
                    "SELECT run_id FROM analysis_runs WHERE project_id=? AND idempotency_key=?",
                    (project_id, idempotency_key),
                ).fetchone()
                if existing:
                    return self.get_run(str(existing[0]))
            active = conn.execute(
                f"SELECT asset_id FROM analysis_run_items WHERE asset_id IN ({','.join('?' for _ in ids)}) "
                "AND status IN ('queued','running','waiting_for_memory') LIMIT 1",
                tuple(ids),
            ).fetchone()
            if active:
                raise store.StoreError("ASSET_ALREADY_CLAIMED", "an asset is already in an active batch", status=409)
        busy = [asset_id for asset_id in ids if self.service.is_asset_busy(asset_id)]
        if busy:
            raise store.StoreError("ASSET_ALREADY_RUNNING", "an asset is already being analyzed", status=409)

        run_id = str(uuid.uuid4())
        now = store.utc_now_iso()
        config = {
            "selection_mode": selection_mode,
            "sampling_profile": sampling_profile,
            "skip_existing": skip_existing,
        }
        with self.db.write() as conn:
            if idempotency_key:
                existing = conn.execute(
                    "SELECT run_id FROM analysis_runs WHERE project_id=? AND idempotency_key=?",
                    (project_id, idempotency_key),
                ).fetchone()
                if existing:
                    return self.get_run(str(existing[0]))
            active = conn.execute(
                f"SELECT asset_id FROM analysis_run_items WHERE asset_id IN ({','.join('?' for _ in ids)}) "
                "AND status IN ('queued','running','waiting_for_memory') LIMIT 1",
                tuple(ids),
            ).fetchone()
            if active:
                raise store.StoreError(
                    "ASSET_ALREADY_CLAIMED", "an asset is already in an active batch", status=409
                )
            active_job = conn.execute(
                f"SELECT asset_id FROM jobs WHERE asset_id IN ({','.join('?' for _ in ids)}) "
                "AND state IN ('queued','running','cancelling') LIMIT 1",
                tuple(ids),
            ).fetchone()
            if active_job:
                raise store.StoreError(
                    "ASSET_ALREADY_RUNNING", "an asset already has an active analysis job", status=409
                )
            conn.execute(
                "INSERT INTO analysis_runs(run_id, project_id, status, config_json, idempotency_key, "
                "total_items, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (run_id, project_id, "queued", json.dumps(config, sort_keys=True),
                 idempotency_key, len(ids), now, now),
            )
            for position, asset_id in enumerate(ids):
                conn.execute(
                    "INSERT INTO analysis_run_items(item_id, run_id, project_id, asset_id, position, "
                    "status, updated_at) VALUES (?,?,?,?,?,'queued',?)",
                    (str(uuid.uuid4()), run_id, project_id, asset_id, position, now),
                )
        self._start(run_id)
        return self.get_run(run_id)

    def _start(self, run_id: str) -> None:
        with self._lock:
            current = self._threads.get(run_id)
            if current is not None and current.is_alive():
                return
            cancel = self._cancels.setdefault(run_id, threading.Event())
            cancel.clear()
            thread = threading.Thread(target=self._run, args=(run_id, cancel), name=f"batch-{run_id[:8]}", daemon=True)
            self._threads[run_id] = thread
            thread.start()

    def _run(self, run_id: str, cancel: threading.Event) -> None:
        if self._shutdown.is_set():
            return
        with self._run_lock:
            while not self._shutdown.is_set() and not cancel.is_set():
                run = self.get_run(run_id)
                if run["status"] in {"paused", "waiting_for_memory", "cancelled"}:
                    return
                with self.db.read() as conn:
                    row = conn.execute(
                        "SELECT * FROM analysis_run_items WHERE run_id=? AND status='queued' "
                        "ORDER BY position LIMIT 1",
                        (run_id,),
                    ).fetchone()
                if row is None:
                    break
                item = dict(row)
                item_id = item["item_id"]
                self._update_run(run_id, status="running")
                with self.db.write() as conn:
                    conn.execute(
                        "UPDATE analysis_run_items SET status='running', updated_at=? WHERE item_id=?",
                        (store.utc_now_iso(), item_id),
                    )
                try:
                    result = self.service.auto_analyze_sync(
                        item["asset_id"],
                        selection_mode=run["config"]["selection_mode"],
                        sampling_profile=run["config"]["sampling_profile"],
                        skip_existing=run["config"]["skip_existing"],
                        cancel=cancel,
                    )
                    with self.db.write() as conn:
                        conn.execute(
                            "UPDATE analysis_run_items SET status=?, sampling_status=?, vision_status=?, "
                            "cache_reused=?, frames_selected=?, frames_committed=?, error_code=NULL, updated_at=? "
                            "WHERE item_id=? AND status='running'",
                            (
                                "skipped" if result.get("skipped")
                                else ("cancelled" if result.get("status") == "cancelled"
                                      else ("partial" if result.get("status") == "partial" else "complete")),
                                "complete", result.get("status"),
                                1 if result.get("skipped") or result.get("frame_cache_hits") else 0,
                                int(result.get("selected_frames") or 0),
                                int(result.get("frames_observed") or 0),
                                store.utc_now_iso(), item_id,
                            ),
                        )
                except Exception as exc:  # noqa: BLE001
                    code = str(getattr(exc, "code", "ANALYSIS_FAILED"))
                    if code in {"MEMORY_PRESSURE_RISING", "HEAVY_SLOT_BUSY", "MEMORY_PRESSURE_CRITICAL"}:
                        with self.db.write() as conn:
                            conn.execute(
                                "UPDATE analysis_run_items SET status='queued', error_code=?, updated_at=? WHERE item_id=?",
                                (code, store.utc_now_iso(), item_id),
                            )
                        self._update_run(run_id, status="waiting_for_memory", error_code=code)
                        return
                    item_status = "cancelled" if cancel.is_set() else "failed"
                    with self.db.write() as conn:
                        conn.execute(
                            "UPDATE analysis_run_items SET status=?, error_code=?, updated_at=? WHERE item_id=?",
                            (item_status, code, store.utc_now_iso(), item_id),
                        )
                if cancel.is_set():
                    break
            if cancel.is_set():
                self._update_run(run_id, status="cancelled")
            else:
                current = self.get_run(run_id)
                self._update_run(
                    run_id,
                    status=aggregate_run_status(current["status"], current["counts"]),
                )

    def _update_run(self, run_id: str, *, status: str, error_code: str | None = None) -> None:
        allowed_from: dict[str, tuple[str, ...]] = {
            "running": ("queued", "running"),
            "waiting_for_memory": ("queued", "running", "waiting_for_memory"),
            "paused": ("queued", "running", "waiting_for_memory", "paused"),
            "queued": ("paused", "waiting_for_memory", "partial", "failed", "queued"),
            "complete": ("queued", "running", "complete"),
            "partial": ("queued", "running", "partial"),
            "failed": ("queued", "running", "failed"),
            "cancelled": ("queued", "running", "waiting_for_memory", "paused", "cancelled"),
        }
        sources = allowed_from[status]
        with self.db.write() as conn:
            conn.execute(
                f"UPDATE analysis_runs SET status=?, updated_at=? WHERE run_id=? "
                f"AND status IN ({','.join('?' for _ in sources)})",
                (status, store.utc_now_iso(), run_id, *sources),
            )

    # -- controls --------------------------------------------------------- #
    def pause(self, run_id: str) -> dict[str, Any]:
        run = self.get_run(run_id)
        if run["status"] not in {"running", "queued", "waiting_for_memory"}:
            raise store.StoreError("BATCH_NOT_ACTIVE", "run is not active", status=409)
        self._update_run(run_id, status="paused")
        return self.get_run(run_id)

    def resume(self, run_id: str) -> dict[str, Any]:
        run = self.get_run(run_id)
        if run["status"] not in {"paused", "waiting_for_memory"}:
            raise store.StoreError("BATCH_NOT_PAUSED", "run is not paused", status=409)
        self._update_run(run_id, status="queued")
        self._start(run_id)
        return self.get_run(run_id)

    def cancel(self, run_id: str) -> dict[str, Any]:
        run = self.get_run(run_id)
        if run["status"] in {"complete", "partial", "cancelled", "failed"}:
            return run
        event = self._cancels.setdefault(run_id, threading.Event())
        event.set()
        with self.db.write() as conn:
            conn.execute(
                "UPDATE analysis_run_items SET status='cancelled', updated_at=? "
                "WHERE run_id=? AND status IN ('queued','running','waiting_for_memory')",
                (store.utc_now_iso(), run_id),
            )
        self._update_run(run_id, status="cancelled")
        return self.get_run(run_id)

    def retry_failed(self, run_id: str) -> dict[str, Any]:
        with self.db.write() as conn:
            row = conn.execute(
                "SELECT status FROM analysis_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None:
                raise store.StoreError("RUN_NOT_FOUND", "analysis run not found", status=404)
            if row["status"] not in {"partial", "failed"}:
                raise store.StoreError(
                    "BATCH_NOT_TERMINAL", "failed items can only be retried from a terminal run", status=409
                )
            updated = conn.execute(
                "UPDATE analysis_run_items SET status='queued', error_code=NULL, updated_at=? "
                "WHERE run_id=? AND status='failed'",
                (store.utc_now_iso(), run_id),
            )
            if updated.rowcount == 0:
                raise store.StoreError("BATCH_NO_FAILED_ITEMS", "run has no failed items", status=409)
            conn.execute(
                "UPDATE analysis_runs SET status='queued', updated_at=? WHERE run_id=?",
                (store.utc_now_iso(), run_id),
            )
        self._start(run_id)
        return self.get_run(run_id)

    # -- read ------------------------------------------------------------- #
    def get_run(self, run_id: str) -> dict[str, Any]:
        with self.db.read() as conn:
            row = conn.execute("SELECT * FROM analysis_runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise store.StoreError("RUN_NOT_FOUND", "analysis run not found", status=404)
            run = dict(row)
            counts = {
                status: int(count)
                for status, count in conn.execute(
                    "SELECT status, COUNT(*) FROM analysis_run_items WHERE run_id=? GROUP BY status",
                    (run_id,),
                ).fetchall()
            }
        run["config"] = json.loads(run.pop("config_json"))
        run["counts"] = counts
        run["completed"] = counts.get("complete", 0)
        run["skipped"] = counts.get("skipped", 0)
        run["failed"] = counts.get("failed", 0)
        run["running"] = counts.get("running", 0)
        run["waiting"] = counts.get("queued", 0)
        return run

    def list_items(self, run_id: str) -> list[dict[str, Any]]:
        self.get_run(run_id)
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT * FROM analysis_run_items WHERE run_id=? ORDER BY position", (run_id,)
            ).fetchall()
        return [dict(row) for row in rows]
