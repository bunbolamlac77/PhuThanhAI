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
from phuthanhai.analyzer.vision_service import VisionError, VisionService, model_identity, runtime_versions
from phuthanhai.analyzer.context_fusion import build_overlay
from phuthanhai.analyzer.vlm_worker import PROMPT_VERSION, REQUEST_VERSION, VISUAL_PROMPT
from phuthanhai.runtime.resource import GOVERNOR
from phuthanhai.runtime.scheduler import HEAVY_SLOT
from phuthanhai.utils.fs import atomic_write_json
from phuthanhai.utils.hashes import sha256_file
import hashlib, json, uuid
from phuthanhai.analyzer import context_profiles
from phuthanhai.analyzer.context_fusion import build_overlay
from phuthanhai.analyzer import report as report_builder
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

    # -- S04 project profile / context ------------------------------------ #
    def set_project_profile(self, project_id: str, profile: str, expected_revision: int) -> dict[str, Any]:
        if profile not in context_profiles.PROFILE_IDS:
            raise store.StoreError("PROFILE_INVALID", f"unknown profile: {profile}", status=422)
        with self.db.write() as conn:
            project = store.set_project_profile(conn, project_id, profile, int(expected_revision))
            project["asset_count"] = store.count_assets(conn, project_id)
            return project

    def _labels(self, project_id: str, asset_id: str) -> list[dict[str, Any]]:
        with self.db.read() as conn:
            return store.get_context_labels(conn, project_id, asset_id)

    def context_overlay(self, asset_id: str) -> dict[str, Any]:
        asset = self.get_asset(asset_id)
        if asset.get("analysis_status") not in {"complete", "partial"}:
            raise store.StoreError("EVIDENCE_REQUIRED", "run visual analysis before context fusion", status=409)
        evidence = self.vision.load_evidence(asset_id)
        if evidence is None:
            raise store.StoreError("EVIDENCE_REQUIRED", "visual evidence is missing", status=409)
        project = self.get_project(asset["project_id"])
        manifest = (
            self.pipeline.load_manifest(asset_id, asset["sampling_fingerprint"])
            if asset.get("sampling_fingerprint")
            else None
        )
        overlay = build_overlay(
            asset_id=asset_id,
            asset_duration_ms=int(asset.get("duration_ms") or 1),
            evidence=evidence,
            profile_id=project.get("analysis_profile", context_profiles.DEFAULT_PROFILE),
            user_labels=self._labels(asset["project_id"], asset_id),
            scene_windows=(manifest or {}).get("segments"),
        )
        overlay["project_id"] = asset["project_id"]
        overlay["profile_revision"] = project.get("profile_revision")
        return overlay

    def confirm_label(
        self,
        asset_id: str,
        *,
        label_key: str,
        label_vi: str | None,
        status: str,
        scene_id: str | None = None,
        frame_id: str | None = None,
    ) -> dict[str, Any]:
        if status not in {"confirmed", "corrected", "dismissed"}:
            raise store.StoreError("LABEL_STATUS_INVALID", "status must be confirmed, corrected or dismissed", status=422)
        asset = self.get_asset(asset_id)
        evidence = self.vision.load_evidence(asset_id)
        if evidence is None:
            raise store.StoreError("EVIDENCE_REQUIRED", "visual evidence is missing", status=409)
        frames = {frame["frame_id"]: frame for frame in evidence["frames"]}
        if frame_id is not None and frame_id not in frames:
            raise store.StoreError("LABEL_FRAME_UNKNOWN", "frame does not belong to this evidence", status=422)
        if scene_id is not None and not any(frame["scene_id"] == scene_id for frame in frames.values()):
            raise store.StoreError("LABEL_SCENE_UNKNOWN", "scene does not belong to this evidence", status=422)
        resolved_vi = (label_vi or "").strip()
        if not resolved_vi:
            profile_id = self.get_project(asset["project_id"]).get(
                "analysis_profile", context_profiles.DEFAULT_PROFILE
            )
            spec = context_profiles.label(profile_id, label_key)
            resolved_vi = spec.label_vi if spec else label_key
        if status != "dismissed" and not resolved_vi:
            raise store.StoreError("LABEL_TEXT_REQUIRED", "label text is required", status=422)
        if len(resolved_vi) > 200:
            raise store.StoreError("LABEL_TEXT_TOO_LONG", "label exceeds 200 characters", status=422)
        with self.db.write() as conn:
            store.upsert_context_label(
                conn,
                asset["project_id"],
                asset_id,
                label_key=label_key,
                label_vi=resolved_vi,
                status=status,
                scene_id=scene_id,
                frame_id=frame_id,
            )
        return self.context_overlay(asset_id)

    def build_project_reports(self, project_id: str, *, include_private_notes: bool = False) -> dict[str, Any]:
        started = time.monotonic()
        project = self.get_project(project_id)
        assets = self.list_assets(project_id, limit=50_000)["items"]
        with self.db.read() as conn:
            all_annotations = store.list_export_annotations(conn, project_id)
        annotations = [
            note
            for note in all_annotations
            if include_private_notes or note.get("note_type") == "context_label"
        ]
        evidence_by_asset: dict[str, dict[str, Any]] = {}
        manifests: dict[str, dict[str, Any]] = {}
        overlays: dict[str, dict[str, Any]] = {}
        for asset in assets:
            if asset.get("analysis_status") not in {"complete", "partial"}:
                continue
            evidence = self.vision.load_evidence(asset["asset_id"])
            if evidence is None:
                continue
            evidence_by_asset[asset["asset_id"]] = evidence
            if asset.get("sampling_fingerprint"):
                manifest = self.pipeline.load_manifest(asset["asset_id"], asset["sampling_fingerprint"])
                if manifest is not None:
                    manifests[asset["asset_id"]] = manifest
            overlays[asset["asset_id"]] = build_overlay(
                asset_id=asset["asset_id"],
                asset_duration_ms=int(asset.get("duration_ms") or 1),
                evidence=evidence,
                profile_id=project.get("analysis_profile", context_profiles.DEFAULT_PROFILE),
                user_labels=self._labels(project_id, asset["asset_id"]),
                scene_windows=(manifests.get(asset["asset_id"]) or {}).get("segments"),
            )
        documents = report_builder.assemble_documents(
            project=project,
            assets=assets,
            annotations=annotations,
            evidence_by_asset=evidence_by_asset,
            overlays=overlays,
            manifests=manifests,
        )
        report_builder.validate_documents(documents, self.settings.contracts_dir)
        out_dir = self.settings.data.reports / project_id
        written = report_builder.write_reports(
            documents=documents,
            assets=assets,
            overlays=overlays,
            evidence_by_asset=evidence_by_asset,
            out_dir=out_dir,
        )
        return {
            "project_id": project_id,
            "profile": project.get("analysis_profile"),
            "analysed_assets": len(documents["analyses"]),
            "candidate_labels": sum(len(overlay.get("candidates", [])) for overlay in overlays.values()),
            "user_labels": sum(len(overlay.get("user_labels", [])) for overlay in overlays.values()),
            "files": written["files"],
            "dir": written["dir"],
            "elapsed_s": round(time.monotonic() - started, 3),
            "validated": True,
        }

    def targeted_context_review(
        self, asset_id: str, profile_id: str, frame_ids: list[str]
    ) -> dict[str, Any]:
        """Level 2: optional profile-aware re-look, separate from raw evidence."""

        if profile_id not in context_profiles.PROFILE_IDS:
            raise store.StoreError("PROFILE_INVALID", "unknown profile", status=422)
        if not frame_ids or len(frame_ids) > 3:
            raise store.StoreError("REVIEW_FRAME_COUNT", "select 1–3 frames", status=422)
        asset = self.get_asset(asset_id)
        evidence = self.vision.load_evidence(asset_id)
        if evidence is None:
            raise store.StoreError("EVIDENCE_REQUIRED", "visual evidence is missing", status=409)
        by_id = {frame["frame_id"]: frame for frame in evidence["frames"]}
        manifest = (
            self.pipeline.load_manifest(asset_id, asset["sampling_fingerprint"])
            if asset.get("sampling_fingerprint")
            else None
        )
        manifest_frames = {frame["frame_id"]: frame for frame in (manifest or {}).get("frames", [])}
        selected = [manifest_frames[fid] for fid in frame_ids if fid in manifest_frames and fid in by_id]
        if len(selected) != len(frame_ids):
            raise store.StoreError("REVIEW_FRAME_UNKNOWN", "frame not in evidence", status=422)
        model = model_identity(self.vision.model_path)
        runtime = runtime_versions(self.vision.runtime_python)
        digest = hashlib.sha256(
            json.dumps(
                {
                    "frames": [[f["frame_id"], f["sha256"]] for f in selected],
                    "profile": profile_id,
                    "prompt": PROMPT_VERSION,
                    "model": model,
                    "runtime": runtime,
                    "review_version": "s04-review-1",
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        cache_dir = self.settings.data.inference / asset_id / f"review-{digest[:20]}"
        cached = cache_dir / "context_review.json"
        if cached.is_file():
            data = json.loads(cached.read_text(encoding="utf-8"))
            data["cached"] = True
            return data

        allowed, reason = GOVERNOR.can_start_heavy_worker()
        if not allowed:
            raise store.StoreError(reason, "resource governor refused a heavy worker", status=409)
        prompt = (
            VISUAL_PROMPT
            + f"\nContext preset: {profile_id}. If you see a table, decide whether it is a "
            "ritual ancestor altar, a wedding decoration table, or a banquet table. "
            "Do not claim identities, relationships, speech or clock time."
        )
        request = {
            "request_version": REQUEST_VERSION,
            "job_id": f"review-{uuid.uuid4()}",
            "asset_id": asset_id,
            "analysis_id": evidence["analysis_id"],
            "model_path": str(self.vision.model_path),
            "allowed_image_root": str(self.settings.data.sampling),
            "prompt": prompt,
            "prompt_version": PROMPT_VERSION + "+context",
            "method": "sequential_frames",
            "frames": [
                {
                    "frame_id": frame["frame_id"],
                    "scene_id": frame["scene_id"],
                    "image_path": frame["path"],
                    "normalized_pts_ms": int(frame["normalized_pts_ms"]),
                    "sha256": frame["sha256"],
                }
                for frame in selected
            ],
            "max_tokens": 320,
            "temperature": 0.0,
            "seed": 0,
        }
        try:
            HEAVY_SLOT.acquire(asset_id, timeout=600)
            try:
                final = self.vision._run_worker(request, cancel=None)
            finally:
                HEAVY_SLOT.release(asset_id)
        except VisionError as exc:
            raise store.StoreError(exc.code, str(exc), status=502) from exc
        reviewed = []
        for entry in final.get("frames", []):
            frame = by_id[entry["frame_id"]]
            synthetic = {
                "analysis_id": evidence["analysis_id"],
                "coverage_status": evidence["coverage_status"],
                "frames": [
                    {
                        "frame_id": frame["frame_id"],
                        "scene_id": frame["scene_id"],
                        "normalized_pts_ms": frame["normalized_pts_ms"],
                        "interpretation_level": "observed",
                        "observation": entry["observation"],
                    }
                ],
            }
            overlay = build_overlay(
                asset_id=asset_id,
                asset_duration_ms=int(asset["duration_ms"]),
                evidence=synthetic,
                profile_id=profile_id,
                user_labels=[],
                scene_windows=[
                    {"scene_id": frame["scene_id"],
                     "start_ms": next((seg["start_ms"] for seg in manifest.get("segments", []) if seg["scene_id"] == frame["scene_id"]), 0),
                     "end_ms": next((seg["end_ms"] for seg in manifest.get("segments", []) if seg["scene_id"] == frame["scene_id"]), int(asset["duration_ms"]))}
                ] if manifest else None,
            )
            reviewed.append(
                {
                    "frame_id": frame["frame_id"],
                    "scene_id": frame["scene_id"],
                    "normalized_pts_ms": frame["normalized_pts_ms"],
                    "observation": entry["observation"],
                    "candidates": overlay["candidates"],
                    "source": "context_review",
                }
            )
        payload = {
            "kind": "phuthanhai.context_review",
            "schema_version": "1.0.0",
            "review_id": str(uuid.uuid4()),
            "asset_id": asset_id,
            "profile": profile_id,
            "prompt_version": PROMPT_VERSION + "+context",
            "model_identity": model,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "cached": False,
            "frames": reviewed,
            "note_vi": "Đây là gợi ý xác minh chuyên sâu, không tự động xác nhận nhãn.",
        }
        cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic_write_json(cached, payload)
        return payload

    def list_report_files(self, project_id: str) -> dict[str, Any]:
        self.get_project(project_id)
        directory = self.settings.data.reports / project_id
        files = sorted(item.name for item in directory.iterdir() if item.is_file()) if directory.is_dir() else []
        return {"project_id": project_id, "files": files}

    def read_report_file(self, project_id: str, name: str) -> tuple[bytes, str]:
        self.get_project(project_id)
        allowed = {
            "video_report_ai.md": "text/markdown; charset=utf-8",
            "project_report_ai.md": "text/markdown; charset=utf-8",
            "video_report_portable.md": "text/markdown; charset=utf-8",
            "workspace_report_portable.md": "text/markdown; charset=utf-8",
            "project_bundle.json": "application/json",
            "workspace_extension.json": "application/json",
            "workspace_package.json": "application/json",
        }
        if name not in allowed:
            raise store.StoreError("REPORT_NOT_FOUND", "unknown report file", status=404)
        path = self.settings.data.reports / project_id / name
        if not path.is_file():
            raise store.StoreError("REPORT_NOT_FOUND", "report has not been built", status=404)
        return path.read_bytes(), allowed[name]

    # -- visual analysis --------------------------------------------------- #
    def start_analysis(
        self,
        asset_id: str,
        method: str = "auto",
        selection_mode: str = "full",
        user_ranges: list[list[int]] | None = None,
    ) -> dict[str, Any]:
        if method not in {"auto", "single_frame", "sequential_frames", "multi_frame"}:
            raise store.StoreError("ANALYSIS_METHOD_INVALID", f"unknown method: {method}")
        if selection_mode not in {"smart", "balanced", "full"}:
            raise store.StoreError("SELECTION_MODE_INVALID", f"unknown selection mode: {selection_mode}")
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
        future = self._executor.submit(
            self._run_analysis_job, job_id, asset, path, method, selection_mode, user_ranges, cancel_event
        )
        with self._jobs_lock:
            self._jobs[job_id] = {
                "asset_id": asset_id,
                "kind": "visual_analysis",
                "selection_mode": selection_mode,
                "future": future,
                "cancel": cancel_event,
                "started": time.monotonic(),
            }
        return self.get_job(job_id)

    def _run_analysis_job(
        self,
        job_id: str,
        asset: dict[str, Any],
        path: Path,
        method: str,
        selection_mode: str,
        user_ranges: list[list[int]] | None,
        cancel: threading.Event,
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
                asset,
                path,
                method=method,
                selection_mode=selection_mode,
                user_ranges=user_ranges,
                progress=progress,
                cancel=cancel,
                job_id=job_id,
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
        visual_coverage = evidence.get("visual_coverage") or {}
        return {
            "asset_id": asset_id,
            "sampling_frames": visual_coverage.get("sampling", len(evidence["frames"])),
            "selected_frames": visual_coverage.get("selected", len(evidence["frames"])),
            "selection_mode": visual_coverage.get("mode", "full"),
            "execution_status": evidence.get("execution_status", "complete"),
            "visual_coverage_status": visual_coverage.get("status", "complete"),
            "visual_coverage": visual_coverage,
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
