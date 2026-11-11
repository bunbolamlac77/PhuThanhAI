"""Service layer: one place that composes the database, scanner, preview and notes.

The API routes and the tests both call this class, so there is a single
implementation of "open a project", "scan a folder", "save a note" and "resolve an
asset to a file on disk". No route touches SQL or the filesystem directly.
"""

from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any

from phuthanhai.analyzer.sampling import PROFILE_GAPS_MS
from phuthanhai.analyzer.sampling_pipeline import SamplingError, SamplingPipeline
from phuthanhai.analyzer.vision_service import VisionError, VisionService
from phuthanhai.config import Settings
from phuthanhai.library import notes as note_rules
from phuthanhai.library import store
from phuthanhai.library.db import Database
from phuthanhai.library.index import ScanStats, refresh_source_states, scan_project
from phuthanhai.library.preview import DEFAULT_CACHE_QUOTA_BYTES, PreviewService
from phuthanhai.utils.fs import is_inside


class LibraryService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        settings.data.ensure()
        self.db = Database(settings.data.registry_db)
        self.db.migrate()
        with self.db.read() as conn:
            raw_quota = store.get_setting(conn, "cache.quota_bytes")
        quota = int(raw_quota) if raw_quota and raw_quota.isdigit() else DEFAULT_CACHE_QUOTA_BYTES
        self.preview = PreviewService(
            self.db,
            thumbs_dir=settings.data.thumbs,
            proxies_dir=settings.data.preview_proxies,
            quota_bytes=quota,
        )
        self.pipeline = SamplingPipeline(self.db, settings.data.sampling)
        self.vision = VisionService(self.db, settings)
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="sampling")
        self._jobs: dict[str, dict[str, Any]] = {}
        self._jobs_lock = threading.Lock()

    # -- lifecycle --------------------------------------------------------- #
    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
        self.preview.shutdown()
        self.db.close_current_thread()

    # -- projects ---------------------------------------------------------- #
    def create_project(self, name: str, root_path: str) -> dict[str, Any]:
        root = Path(root_path).expanduser()
        try:
            root = root.resolve(strict=True)
        except OSError as exc:
            raise store.StoreError("SOURCE_MISSING", "project folder not found", status=404) from exc
        if not root.is_dir():
            raise store.StoreError("SOURCE_NOT_DIRECTORY", "project root must be a directory")

        with self.db.write() as conn:
            existing = store.find_project_by_root(conn, str(root))
            if existing is not None:
                existing["created"] = False
                return existing
            project = store.create_project(conn, name=name, root_path=str(root))
            project["created"] = True
            return project

    def list_projects(self) -> list[dict[str, Any]]:
        with self.db.read() as conn:
            projects = store.list_projects(conn)
            for project in projects:
                project["asset_count"] = store.count_assets(conn, project["project_id"])
            return projects

    def get_project(self, project_id: str) -> dict[str, Any]:
        with self.db.read() as conn:
            project = store.get_project(conn, project_id)
            project["asset_count"] = store.count_assets(conn, project_id)
            return project

    def scan(self, project_id: str, directory: str | None = None, *, max_files: int = 50_000) -> ScanStats:
        project = self.get_project(project_id)
        target = directory or project["root_path"]
        return scan_project(self.db, project_id, target, max_files=max_files)

    def refresh_sources(self, project_id: str) -> dict[str, int]:
        self.get_project(project_id)
        return refresh_source_states(self.db, project_id)

    # -- assets ------------------------------------------------------------ #
    def list_assets(self, project_id: str, **kwargs: Any) -> dict[str, Any]:
        cursor_raw = kwargs.pop("cursor", None)
        cursor = store.decode_cursor(cursor_raw) if cursor_raw else None
        self.get_project(project_id)
        with self.db.read() as conn:
            rows, next_cursor, total = store.list_assets(conn, project_id=project_id, cursor=cursor, **kwargs)
        return {"items": rows, "next_cursor": next_cursor, "total": total}

    def get_asset(self, asset_id: str) -> dict[str, Any]:
        with self.db.read() as conn:
            return store.get_asset(conn, asset_id)

    def resolve_asset_file(self, asset_id: str) -> tuple[dict[str, Any], Path]:
        """Map an asset id to a validated, read-only local file.

        A caller can never supply a filesystem path: only a registered asset id is
        accepted, and the resolved path must still live inside the project root.
        """

        asset = self.get_asset(asset_id)
        project = self.get_project(asset["project_id"])
        candidate = Path(asset["path"])
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            with self.db.write() as conn:
                store.set_source_state(conn, asset_id, "missing")
            raise store.StoreError("SOURCE_MISSING", "source file is no longer present", status=404) from exc
        if not is_inside(resolved, Path(project["root_path"]).resolve()):
            raise store.StoreError("SECURITY_DENIED", "asset path escaped the project root", status=403)
        if not resolved.is_file():
            raise store.StoreError("SOURCE_MISSING", "source is not a regular file", status=404)
        return asset, resolved

    def thumbnail(self, asset_id: str) -> tuple[Path, dict[str, Any]]:
        asset = self.get_asset(asset_id)
        path = self.preview.get_thumbnail(asset)
        return path, asset

    def proxy(self, asset_id: str) -> dict[str, Any]:
        asset = self.get_asset(asset_id)
        result = self.preview.get_proxy(asset)
        return {
            "asset_id": asset_id,
            "created": result.created,
            "duration_ms": result.duration_ms,
            "mapping_delta_ms": result.mapping_delta_ms,
            "url": f"/api/v1/assets/{asset_id}/preview",
            "mode": "proxy",
        }

    def preview_info(self, asset_id: str) -> dict[str, Any]:
        asset = self.get_asset(asset_id)
        proxy_exists = self.preview.proxy_path(asset_id).is_file()
        return {
            "asset_id": asset_id,
            "mode": "proxy" if proxy_exists else "direct",
            "source_available": asset["source_state"] == "available",
            "codec": asset["codec"],
            "rotation": asset["rotation"],
            "duration_ms": asset["duration_ms"],
            "stream_url": f"/api/v1/assets/{asset_id}/stream",
            "thumbnail_url": f"/api/v1/assets/{asset_id}/thumbnail",
            "proxy_available": proxy_exists,
            "proxy_url": f"/api/v1/assets/{asset_id}/proxy-stream" if proxy_exists else None,
        }

    def proxy_file(self, asset_id: str) -> Path:
        path = self.preview.proxy_path(asset_id)
        if not path.is_file():
            raise store.StoreError("PROXY_FAILED", "proxy has not been created", status=404)
        return path

    def cache_stats(self) -> dict[str, Any]:
        return self.preview.stats()

    def prune_cache(self) -> dict[str, int]:
        return self.preview.enforce_quota()

    # -- annotations ------------------------------------------------------- #
    def create_annotation(self, project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        text = note_rules.normalize_text(payload.get("text", ""))
        scope = payload.get("scope", "clip")
        asset_id = payload.get("asset_id")
        start_ms = payload.get("start_ms")
        end_ms = payload.get("end_ms")

        duration_ms = None
        assets = None
        if asset_id:
            asset = self.get_asset(asset_id)
            if asset["project_id"] != project_id:
                raise store.StoreError("SECURITY_DENIED", "asset belongs to another project", status=403)
            duration_ms = asset["duration_ms"]

        note_rules.validate_scope(
            scope=scope, asset_id=asset_id, start_ms=start_ms, end_ms=end_ms, duration_ms=duration_ms
        )
        note_type = payload.get("note_type") or note_rules.infer_note_type(text)
        note_rules.validate_note_type(note_type)

        wall_clock = payload.get("wall_clock")
        suggestion = None
        if not wall_clock:
            suggestion = note_rules.suggest_wall_clock(text)
        annotation_id = str(uuid.uuid4())
        with self.db.write() as conn:
            annotation = store.create_annotation(
                conn,
                annotation_id=annotation_id,
                project_id=project_id,
                asset_id=asset_id,
                scope=scope,
                start_ms=start_ms,
                end_ms=end_ms,
                text=text,
                note_type=note_type,
                wall_clock=wall_clock,
            )
        return {"annotation": annotation, "wall_clock_suggestion": suggestion}

    def update_annotation(self, annotation_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self.db.read() as conn:
            current = store.get_annotation(conn, annotation_id)
        text = note_rules.normalize_text(payload.get("text", current["text"]))
        note_type = payload.get("note_type") or current["note_type"]
        note_rules.validate_note_type(note_type)
        start_ms = payload.get("start_ms", current["start_ms"])
        end_ms = payload.get("end_ms", current["end_ms"])
        duration_ms = None
        if current["asset_id"]:
            duration_ms = self.get_asset(current["asset_id"])["duration_ms"]
        note_rules.validate_scope(
            scope=current["scope"],
            asset_id=current["asset_id"],
            start_ms=start_ms,
            end_ms=end_ms,
            duration_ms=duration_ms,
        )
        wall_clock = payload.get("wall_clock", current["wall_clock"])
        with self.db.write() as conn:
            annotation = store.update_annotation(
                conn,
                annotation_id=annotation_id,
                expected_revision=int(payload["expected_revision"]),
                text=text,
                note_type=note_type,
                start_ms=start_ms,
                end_ms=end_ms,
                wall_clock=wall_clock,
            )
        return {"annotation": annotation, "wall_clock_suggestion": note_rules.suggest_wall_clock(text)}

    def delete_annotation(self, annotation_id: str, *, expected_revision: int) -> None:
        with self.db.write() as conn:
            store.delete_annotation(conn, annotation_id, expected_revision=expected_revision)

    def list_annotations(self, project_id: str, *, asset_id: str | None = None) -> list[dict[str, Any]]:
        self.get_project(project_id)
        with self.db.read() as conn:
            return store.list_annotations(conn, project_id, asset_id=asset_id)

    # -- reviews ----------------------------------------------------------- #
    def get_review(self, asset_id: str) -> dict[str, Any]:
        self.get_asset(asset_id)
        with self.db.read() as conn:
            return store.get_review(conn, asset_id)

    def update_review(self, asset_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        asset = self.get_asset(asset_id)
        duration = asset["duration_ms"] or 0
        ranges = payload.get("selected_ranges") or []
        for span in ranges:
            if not (0 <= int(span["start_ms"]) < int(span["end_ms"]) <= duration):
                raise store.StoreError(
                    "REVIEW_RANGE_INVALID", "selected range is outside the asset duration"
                )
        playhead = payload.get("last_playhead_ms")
        if playhead is not None and not (0 <= int(playhead) <= duration):
            raise store.StoreError("REVIEW_RANGE_INVALID", "playhead is outside the asset duration")
        rating = payload.get("rating")
        if rating is not None and not (1 <= int(rating) <= 5):
            raise store.StoreError("REVIEW_RATING_INVALID", "rating must be between 1 and 5")
        with self.db.write() as conn:
            return store.upsert_review(
                conn,
                project_id=asset["project_id"],
                asset_id=asset_id,
                expected_revision=payload.get("expected_revision"),
                status=payload.get("status", "reviewed"),
                rating=rating,
                favorite=bool(payload.get("favorite", False)),
                labels=payload.get("labels") or [],
                selected_ranges=ranges,
                last_playhead_ms=playhead,
            )

    # -- sampling jobs ----------------------------------------------------- #
    def start_sampling(self, asset_id: str, profile: str = "detailed") -> dict[str, Any]:
        if profile not in PROFILE_GAPS_MS:
            raise store.StoreError("SAMPLING_PROFILE_INVALID", f"unknown profile: {profile}")
        from phuthanhai.runtime.scheduler import HEAVY_SLOT

        if HEAVY_SLOT.busy:
            raise store.StoreError(
                "RESOURCE_BUSY", "a heavy AI worker is running; sampling is paused to protect memory", status=409
            )
        asset, path = self.resolve_asset_file(asset_id)
        with self._jobs_lock:
            for handle in self._jobs.values():
                if handle["asset_id"] == asset_id and not handle["future"].done():
                    raise store.StoreError(
                        "JOB_ALREADY_RUNNING", "a sampling job is already running for this asset", status=409
                    )
        job_id = str(uuid.uuid4())
        with self.db.write() as conn:
            store.create_job(conn, job_id=job_id, project_id=asset["project_id"], asset_id=asset_id, kind="sample")
            store.set_sampling_state(
                conn,
                asset_id,
                status="queued",
                profile=profile,
                fingerprint=asset.get("sampling_fingerprint"),
                sampled_at=asset.get("sampled_at"),
                coverage=asset.get("sampling_coverage"),
            )
        cancel_event = threading.Event()
        future = self._executor.submit(self._run_sampling_job, job_id, asset, path, profile, cancel_event)
        with self._jobs_lock:
            self._jobs[job_id] = {
                "asset_id": asset_id,
                "future": future,
                "cancel": cancel_event,
                "started": time.monotonic(),
            }
        return self.get_job(job_id)

    def _run_sampling_job(self, job_id: str, asset: dict[str, Any], path: Path, profile: str, cancel: threading.Event) -> None:
        last_update = 0.0

        def progress(payload: dict[str, Any]) -> None:
            nonlocal last_update
            now = time.monotonic()
            if now - last_update < 0.4 and payload.get("fraction", 0) < 1:
                return
            last_update = now
            try:
                with self.db.write() as conn:
                    store.update_job(
                        conn,
                        job_id,
                        state="running",
                        progress=round(0.05 + 0.9 * float(payload.get("fraction", 0.0)), 3),
                    )
            except Exception:  # noqa: BLE001 - progress must never kill the job
                pass

        try:
            with self.db.write() as conn:
                store.update_job(conn, job_id, state="running", progress=0.02)
                store.set_sampling_state(
                    conn,
                    asset["asset_id"],
                    status="sampling",
                    profile=profile,
                    fingerprint=asset.get("sampling_fingerprint"),
                    sampled_at=asset.get("sampled_at"),
                    coverage=asset.get("sampling_coverage"),
                )
            outcome = self.pipeline.run(asset, path, profile=profile, progress=progress, cancel=cancel)
            with self.db.write() as conn:
                if outcome.status == "cancelled":
                    store.update_job(conn, job_id, state="cancelled", progress=round(outcome.cache_hits / max(1, len(outcome.plan.targets)), 3))
                else:
                    store.update_job(conn, job_id, state="complete", progress=1.0)
        except SamplingError as exc:
            with self.db.write() as conn:
                store.update_job(conn, job_id, state="failed", error_code=exc.code)
                store.set_sampling_state(
                    conn, asset["asset_id"], status="failed", profile=profile,
                    fingerprint=asset.get("sampling_fingerprint"), sampled_at=asset.get("sampled_at"),
                    coverage="unknown",
                )
        except Exception as exc:  # noqa: BLE001 - always record a stable code
            code = getattr(exc, "code", "SAMPLING_FAILED")
            with self.db.write() as conn:
                store.update_job(conn, job_id, state="failed", error_code=str(code))
                store.set_sampling_state(
                    conn, asset["asset_id"], status="failed", profile=profile,
                    fingerprint=asset.get("sampling_fingerprint"), sampled_at=asset.get("sampled_at"),
                    coverage="unknown",
                )
        finally:
            with self._jobs_lock:
                self._jobs.pop(job_id, None)

    def cancel_sampling(self, job_id: str) -> dict[str, Any]:
        return self.cancel_job(job_id)

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self.db.read() as conn:
            job = store.get_job(conn, job_id)
        with self._jobs_lock:
            handle = self._jobs.get(job_id)
        job["running"] = bool(handle and not handle["future"].done())
        return job

    def list_jobs(self, project_id: str) -> list[dict[str, Any]]:
        self.get_project(project_id)
        with self.db.read() as conn:
            return store.list_jobs(conn, project_id)

    def sampling_summary(self, asset_id: str) -> dict[str, Any]:
        asset = self.get_asset(asset_id)
        fingerprint = asset.get("sampling_fingerprint")
        manifest = None
        if fingerprint:
            manifest = self.pipeline.load_manifest(asset_id, fingerprint)
        return {
            "asset_id": asset_id,
            "sampling_status": asset.get("sampling_status", "not_sampled"),
            "sampling_profile": asset.get("sampling_profile"),
            "sampled_at": asset.get("sampled_at"),
            "coverage_status": asset.get("sampling_coverage"),
            "fingerprint": fingerprint,
            "manifest": None
            if manifest is None
            else {
                "frame_count": len(manifest.get("frames", [])),
                "scene_count": len(manifest.get("segments", [])),
                "execution_status": manifest.get("execution_status"),
                "coverage_status": manifest.get("coverage_status"),
                "observed_max_gap_ms": manifest.get("observed_max_gap_ms"),
                "expected_max_gap_ms": manifest.get("expected_max_gap_ms"),
                "coverage": manifest.get("coverage"),
                "warnings": manifest.get("warnings"),
                "timeline": manifest.get("timeline"),
                "segments": manifest.get("segments"),
                "frames": [
                    {
                        "frame_id": frame["frame_id"],
                        "scene_id": frame["scene_id"],
                        "requested_ms": frame["requested_ms"],
                        "normalized_pts_ms": frame["normalized_pts_ms"],
                        "offset_ms": frame["offset_ms"],
                        "reason": frame["reason"],
                        "width": frame["width"],
                        "height": frame["height"],
                    }
                    for frame in manifest.get("frames", [])
                ],
            },
        }

    # -- visual analysis --------------------------------------------------- #
    def start_analysis(self, asset_id: str, method: str = "auto") -> dict[str, Any]:
        if method not in {"auto", "single_frame", "sequential_frames", "multi_frame"}:
            raise store.StoreError("ANALYSIS_METHOD_INVALID", f"unknown method: {method}")
        asset, path = self.resolve_asset_file(asset_id)
        if asset.get("sampling_status") != "complete":
            raise store.StoreError("SAMPLING_REQUIRED", "run S02 sampling before visual analysis", status=409)
        with self._jobs_lock:
            for handle in self._jobs.values():
                if handle["asset_id"] == asset_id and not handle["future"].done():
                    raise store.StoreError(
                        "JOB_ALREADY_RUNNING", "a job is already running for this asset", status=409
                    )
        job_id = str(uuid.uuid4())
        with self.db.write() as conn:
            store.create_job(conn, job_id=job_id, project_id=asset["project_id"], asset_id=asset_id, kind="visual_analysis")
            store.set_analysis_state(
                conn, asset_id, status="queued",
                analysis_id=asset.get("analysis_id"),
                fingerprint=asset.get("analysis_fingerprint"),
                coverage=asset.get("analysis_coverage"),
            )
        cancel_event = threading.Event()
        future = self._executor.submit(self._run_analysis_job, job_id, asset, path, method, cancel_event)
        with self._jobs_lock:
            self._jobs[job_id] = {
                "asset_id": asset_id,
                "kind": "visual_analysis",
                "future": future,
                "cancel": cancel_event,
                "started": time.monotonic(),
            }
        return self.get_job(job_id)

    def _run_analysis_job(
        self, job_id: str, asset: dict[str, Any], path: Path, method: str, cancel: threading.Event
    ) -> None:
        last_update = 0.0

        def progress(payload: dict[str, Any]) -> None:
            nonlocal last_update
            now = time.monotonic()
            if now - last_update < 0.5 and payload.get("fraction", 0) < 1:
                return
            last_update = now
            try:
                with self.db.write() as conn:
                    store.update_job(
                        conn, job_id, state="running",
                        progress=round(0.05 + 0.9 * float(payload.get("fraction", 0.0)), 3),
                    )
            except Exception:  # noqa: BLE001
                pass

        try:
            with self.db.write() as conn:
                store.update_job(conn, job_id, state="running", progress=0.02)
            outcome = self.vision.analyze(
                asset, path, method=method, progress=progress, cancel=cancel, job_id=job_id
            )
            with self.db.write() as conn:
                state = "cancelled" if outcome.status == "cancelled" else "complete"
                store.update_job(conn, job_id, state=state, progress=1.0 if state == "complete" else outcome.frames_observed / max(1, outcome.frames_total))
        except VisionError as exc:
            with self.db.write() as conn:
                store.update_job(conn, job_id, state="failed", error_code=exc.code)
                store.set_analysis_state(
                    conn, asset["asset_id"], status="failed",
                    analysis_id=asset.get("analysis_id"),
                    fingerprint=asset.get("analysis_fingerprint"),
                    coverage="unknown",
                )
        except Exception as exc:  # noqa: BLE001
            code = getattr(exc, "code", "ANALYSIS_FAILED")
            with self.db.write() as conn:
                store.update_job(conn, job_id, state="failed", error_code=str(code))
                store.set_analysis_state(
                    conn, asset["asset_id"], status="failed",
                    analysis_id=asset.get("analysis_id"),
                    fingerprint=asset.get("analysis_fingerprint"),
                    coverage="unknown",
                )
        finally:
            with self._jobs_lock:
                self._jobs.pop(job_id, None)

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        with self._jobs_lock:
            handle = self._jobs.get(job_id)
        if handle is not None:
            handle["cancel"].set()
        with self.db.write() as conn:
            store.update_job(conn, job_id, state="cancelling")
        return self.get_job(job_id)

    def analysis_summary(self, asset_id: str) -> dict[str, Any]:
        asset = self.get_asset(asset_id)
        evidence = self.vision.load_evidence(asset_id)
        if evidence is None:
            return {
                "asset_id": asset_id,
                "analysis_status": asset.get("analysis_status", "not_started"),
                "analysis_id": asset.get("analysis_id"),
                "coverage_status": asset.get("analysis_coverage"),
                "method": None,
                "frames_total": 0,
                "frames_observed": 0,
                "frames_unknown": 0,
                "warnings": [],
                "evidence": None,
            }
        observed = sum(1 for frame in evidence["frames"] if frame["interpretation_level"] == "observed")
        return {
            "asset_id": asset_id,
            "analysis_status": asset.get("analysis_status", "not_started"),
            "analysis_id": evidence["analysis_id"],
            "coverage_status": evidence["coverage_status"],
            "method": evidence["analysis_method"],
            "frames_total": len(evidence["frames"]),
            "frames_observed": observed,
            "frames_unknown": len(evidence["frames"]) - observed,
            "warnings": evidence.get("warnings", []),
            "evidence": evidence,
        }

    # -- diagnostics ------------------------------------------------------- #
    def stats(self) -> dict[str, Any]:
        with self.db.read() as conn:
            projects = len(store.list_projects(conn))
        return {
            "projects": projects,
            "schema_version": self.db.schema_version(),
            "cache": self.cache_stats(),
        }
