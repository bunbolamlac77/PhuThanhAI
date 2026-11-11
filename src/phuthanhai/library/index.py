"""Folder scanner: read-only traversal, idempotent registry upsert.

Safety rules enforced here:

* Source files are **never** opened for writing, moved, renamed or deleted.
* Symlinked files and directories are skipped and counted, so a link pointing at
  ``/`` or a symlink loop cannot make the scan escape the project root or spin.
* Directories are tracked by ``(st_dev, st_ino)``; a loop is visited once.
* The scan only accepts paths inside the project's registered root.
* ``ffprobe`` failures are recorded per asset (``source_state=error``) instead of
  aborting the scan, and never crash the server.

Idempotence: re-scanning an unchanged folder reports ``added=0`` and leaves every
``asset_id`` untouched. Probing happens outside the write transaction so a note
autosave is never blocked behind a long ffprobe batch.
"""

from __future__ import annotations

import os
import stat
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from phuthanhai.analyzer.probe import ProbeError, SourceInfo, probe_source
from phuthanhai.library import store
from phuthanhai.library.db import Database
from phuthanhai.utils.fs import is_inside

VIDEO_SUFFIXES = frozenset({".mp4", ".mov", ".m4v", ".mkv", ".avi", ".webm"})
SKIP_DIR_NAMES = frozenset(
    {
        ".git",
        ".svn",
        ".hg",
        "node_modules",
        ".venv",
        "__pycache__",
        ".Trash",
        ".Spotlight-V100",
        ".fseventsd",
        ".TemporaryItems",
        "System Volume Information",
    }
)
FLUSH_BATCH = 40


@dataclass
class ScanStats:
    project_id: str
    root: str
    scanned_dirs: int = 0
    candidates: int = 0
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped_symlinks: int = 0
    skipped_non_video: int = 0
    errors: list[dict[str, str]] = field(default_factory=list)
    truncated: bool = False
    elapsed_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "root": self.root,
            "scanned_dirs": self.scanned_dirs,
            "candidates": self.candidates,
            "added": self.added,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "skipped_symlinks": self.skipped_symlinks,
            "skipped_non_video": self.skipped_non_video,
            "errors": list(self.errors),
            "truncated": self.truncated,
            "elapsed_s": self.elapsed_s,
        }


def _iter_video_files(
    root: Path, stats: ScanStats, *, max_depth: int
) -> Iterator[tuple[Path, os.stat_result]]:
    stack: list[tuple[Path, int]] = [(root, 0)]
    visited: set[tuple[int, int]] = set()

    while stack:
        directory, depth = stack.pop()
        try:
            directory_stat = directory.stat()
        except OSError as exc:
            stats.errors.append({"path": directory.name, "code": "DIR_UNREADABLE", "message": str(exc)})
            continue
        key = (directory_stat.st_dev, directory_stat.st_ino)
        if key in visited:
            stats.skipped_symlinks += 1
            continue
        visited.add(key)
        stats.scanned_dirs += 1

        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except PermissionError as exc:
            stats.errors.append({"path": directory.name, "code": "DIR_PERMISSION_DENIED", "message": str(exc)})
            continue
        except OSError as exc:
            stats.errors.append({"path": directory.name, "code": "DIR_UNREADABLE", "message": str(exc)})
            continue

        for entry in entries:
            if entry.is_symlink():
                stats.skipped_symlinks += 1
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    if entry.name in SKIP_DIR_NAMES or depth + 1 > max_depth:
                        continue
                    stack.append((Path(entry.path), depth + 1))
                    continue
            except OSError as exc:
                stats.errors.append({"path": entry.name, "code": "ENTRY_UNREADABLE", "message": str(exc)})
                continue

            suffix = Path(entry.name).suffix.lower()
            if suffix not in VIDEO_SUFFIXES:
                stats.skipped_non_video += 1
                continue
            try:
                entry_stat = entry.stat(follow_symlinks=False)
            except OSError as exc:
                stats.errors.append({"path": entry.name, "code": "ENTRY_UNREADABLE", "message": str(exc)})
                continue
            if not stat.S_ISREG(entry_stat.st_mode):
                continue
            yield Path(entry.path), entry_stat


def _probe_or_error(path: Path) -> tuple[SourceInfo | None, str | None, str | None]:
    try:
        return probe_source(path), None, None
    except ProbeError as exc:
        return None, exc.code, str(exc)
    except OSError as exc:
        return None, "PROBE_IO_ERROR", str(exc)


def scan_project(
    db: Database,
    project_id: str,
    directory: str | Path,
    *,
    max_files: int = 50_000,
    max_depth: int = 12,
    progress: Callable[[ScanStats], None] | None = None,
) -> ScanStats:
    """Scan ``directory`` into the registry of ``project_id``."""

    import time

    started = time.monotonic()
    with db.read() as conn:
        project = store.get_project(conn, project_id)
        project_root = Path(project["root_path"]).expanduser().resolve()

    root = Path(directory).expanduser()
    try:
        root = root.resolve(strict=True)
    except OSError as exc:
        raise store.StoreError("SOURCE_MISSING", f"directory not found: {root.name}", status=404) from exc
    if not root.is_dir():
        raise store.StoreError("SOURCE_NOT_DIRECTORY", "scan target is not a directory")
    if not is_inside(root, project_root):
        raise store.StoreError(
            "SECURITY_DENIED",
            "scan target is outside the project root registered for this project",
            status=403,
        )

    stats = ScanStats(project_id=project_id, root=str(root))
    pending: list[dict[str, Any]] = []

    def flush() -> None:
        nonlocal pending
        if not pending:
            return
        with db.write() as conn:
            for item in pending:
                store.upsert_asset(conn, **item)
        pending = []

    for path, entry_stat in _iter_video_files(root, stats, max_depth=max_depth):
        if stats.candidates >= max_files:
            stats.truncated = True
            break
        stats.candidates += 1

        resolved = str(path.resolve())
        with db.read() as conn:
            existing = store.find_asset_by_path(conn, project_id, resolved)

        asset_id = existing["asset_id"] if existing else str(uuid.uuid4())
        unchanged = bool(
            existing
            and existing["size_bytes"] == entry_stat.st_size
            and existing["mtime_ns"] == entry_stat.st_mtime_ns
            and existing["source_state"] == "available"
        )
        if unchanged:
            stats.unchanged += 1
            continue

        info, error_code, error_message = _probe_or_error(path)
        if info is None:
            stats.errors.append(
                {"path": path.name, "code": error_code or "PROBE_FAILED", "message": error_message or ""}
            )
            pending.append(
                {
                    "asset_id": asset_id,
                    "project_id": project_id,
                    "path": resolved,
                    "filename": path.name,
                    "size_bytes": entry_stat.st_size,
                    "mtime_ns": entry_stat.st_mtime_ns,
                    "dev": entry_stat.st_dev,
                    "ino": entry_stat.st_ino,
                    "duration_ms": None,
                    "width": None,
                    "height": None,
                    "rotation": 0,
                    "fps_nominal": None,
                    "codec": None,
                    "has_audio": False,
                    "vfr_hint": False,
                    "color_transfer": None,
                    "source_state": "error",
                    "preview_status": "error",
                    "error_code": error_code,
                }
            )
        else:
            pending.append(
                {
                    "asset_id": asset_id,
                    "project_id": project_id,
                    "path": resolved,
                    "filename": path.name,
                    "size_bytes": entry_stat.st_size,
                    "mtime_ns": entry_stat.st_mtime_ns,
                    "dev": entry_stat.st_dev,
                    "ino": entry_stat.st_ino,
                    "duration_ms": info.duration_ms,
                    "width": info.width,
                    "height": info.height,
                    "rotation": info.rotation,
                    "fps_nominal": None
                    if info.fps_nominal is None
                    else f"{info.fps_nominal.numerator}/{info.fps_nominal.denominator}",
                    "codec": info.codec,
                    "has_audio": info.has_audio,
                    "vfr_hint": info.vfr_hint,
                    "color_transfer": info.color_transfer,
                    "source_state": "available",
                    "preview_status": "unprobed",
                    "error_code": None,
                }
            )
        if existing:
            stats.updated += 1
        else:
            stats.added += 1
        if progress and stats.candidates % 100 == 0:
            progress(stats)
        if len(pending) >= FLUSH_BATCH:
            flush()

    flush()
    with db.write() as conn:
        store.touch_project(conn, project_id)

    stats.elapsed_s = round(time.monotonic() - started, 3)
    if progress:
        progress(stats)
    return stats


def refresh_source_states(db: Database, project_id: str) -> dict[str, int]:
    """Re-check registered files without re-probing; flags missing/changed sources."""

    counts = {"available": 0, "missing": 0, "changed": 0}
    with db.read() as conn:
        rows = conn.execute(
            "SELECT asset_id, path, size_bytes, mtime_ns FROM assets WHERE project_id = ?",
            (project_id,),
        ).fetchall()

    updates: list[tuple[str, str]] = []
    for row in rows:
        path = Path(row["path"])
        try:
            stat_result = path.stat()
        except FileNotFoundError:
            updates.append((row["asset_id"], "missing"))
            counts["missing"] += 1
            continue
        except OSError:
            updates.append((row["asset_id"], "missing"))
            counts["missing"] += 1
            continue
        if stat_result.st_size != row["size_bytes"] or stat_result.st_mtime_ns != row["mtime_ns"]:
            updates.append((row["asset_id"], "changed"))
            counts["changed"] += 1
        else:
            counts["available"] += 1

    with db.write() as conn:
        for asset_id, state in updates:
            store.set_source_state(conn, asset_id, state)
    return counts
