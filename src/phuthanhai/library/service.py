"""Service layer: one place that composes the database, scanner, preview and notes.

The API routes and the tests both call this class, so there is a single
implementation of "open a project", "scan a folder", "save a note" and "resolve an
asset to a file on disk". No route touches SQL or the filesystem directly.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

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

    # -- lifecycle --------------------------------------------------------- #
    def close(self) -> None:
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

    # -- diagnostics ------------------------------------------------------- #
    def stats(self) -> dict[str, Any]:
        with self.db.read() as conn:
            projects = len(store.list_projects(conn))
        return {
            "projects": projects,
            "schema_version": self.db.schema_version(),
            "cache": self.cache_stats(),
        }
