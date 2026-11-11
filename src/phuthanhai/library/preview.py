"""Thumbnails, HTTP Range byte access, on-demand proxies and bounded cache.

Rules:

* Source files are opened read-only; proxies and thumbnails are written only under
  ``~/Library/Application Support/PhuThanhAI/caches``.
* Range responses stream in bounded chunks - a 10 GB MOV is never read into RAM.
* A proxy is created only for the clip the user actually previews, never for the
  whole library.
* Proxy timecodes must map to the source: after building we probe both files and
  refuse the proxy if durations disagree beyond tolerance.
* Cache eviction is LRU by last access, bounded by a byte quota, and never touches
  user data (notes, manifests, originals).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from phuthanhai.analyzer.probe import ProbeError, find_tool, probe_source
from phuthanhai.library import store
from phuthanhai.library.db import Database

THUMBNAIL_MAX_EDGE = 320
PROXY_MAX_EDGE = 1280
PROXY_DURATION_TOLERANCE_MS = 250
RANGE_CHUNK = 256 * 1024
DEFAULT_CACHE_QUOTA_BYTES = 2 * 1024**3


class RangeNotSatisfiable(RuntimeError):
    code = "RANGE_NOT_SATISFIABLE"


class PreviewError(RuntimeError):
    code = "PROXY_FAILED"


def parse_range_header(header: str | None, size: int) -> tuple[int, int] | None:
    """Parse a single-range ``Range`` header.

    Returns ``(start, end_exclusive)`` or ``None`` when the whole file is wanted.
    Multi-range requests are refused rather than silently mishandled. An
    unsatisfiable range raises :class:`RangeNotSatisfiable` so the caller can send
    416 with ``Content-Range: bytes */<size>``.
    """

    if not header:
        return None
    value = header.strip()
    if not value.lower().startswith("bytes="):
        raise RangeNotSatisfiable("only byte ranges are supported")
    spec = value[6:].strip()
    if "," in spec:
        raise RangeNotSatisfiable("multiple ranges are not supported")
    if "-" not in spec:
        raise RangeNotSatisfiable("malformed range")
    start_text, end_text = spec.split("-", 1)
    start_text, end_text = start_text.strip(), end_text.strip()
    if size <= 0:
        raise RangeNotSatisfiable("empty file")
    if not start_text:
        if not end_text.isdigit():
            raise RangeNotSatisfiable("malformed suffix range")
        suffix = int(end_text)
        if suffix <= 0:
            raise RangeNotSatisfiable("suffix length must be positive")
        start = max(0, size - suffix)
        return start, size
    if not start_text.isdigit():
        raise RangeNotSatisfiable("malformed range start")
    start = int(start_text)
    if start >= size:
        raise RangeNotSatisfiable("range start beyond end of file")
    if not end_text:
        return start, size
    if not end_text.isdigit():
        raise RangeNotSatisfiable("malformed range end")
    end = int(end_text) + 1
    if end <= start:
        raise RangeNotSatisfiable("range end before start")
    return start, min(end, size)


def iter_file_range(path: Path, start: int, end: int, *, chunk_size: int = RANGE_CHUNK) -> Iterator[bytes]:
    """Yield ``[start, end)`` in bounded chunks, closing the handle on exit."""

    remaining = end - start
    with path.open("rb") as handle:
        handle.seek(start)
        while remaining > 0:
            block = handle.read(min(chunk_size, remaining))
            if not block:
                break
            remaining -= len(block)
            yield block


def _longest_edge_filter(max_edge: int) -> str:
    return (
        f"scale='if(gte(iw,ih),min({max_edge},iw),-2)'"
        f":'if(gte(iw,ih),-2,min({max_edge},ih))'"
    )


@dataclass
class ProxyResult:
    path: Path
    created: bool
    duration_ms: int | None
    mapping_delta_ms: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "created": self.created,
            "duration_ms": self.duration_ms,
            "mapping_delta_ms": self.mapping_delta_ms,
        }


class PreviewService:
    """Owns thumbnail/proxy generation and cache accounting for one data root."""

    def __init__(
        self,
        db: Database,
        *,
        thumbs_dir: Path,
        proxies_dir: Path,
        quota_bytes: int = DEFAULT_CACHE_QUOTA_BYTES,
        max_workers: int = 2,
    ) -> None:
        self.db = db
        self.thumbs_dir = thumbs_dir
        self.proxies_dir = proxies_dir
        self.quota_bytes = quota_bytes
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="preview")
        self._inflight: dict[str, Future[Path]] = {}
        self._lock = threading.Lock()
        self.thumbs_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.proxies_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    # -- thumbnails -------------------------------------------------------- #
    def thumbnail_path(self, asset_id: str) -> Path:
        return self.thumbs_dir / f"{asset_id}.jpg"

    def get_thumbnail(self, asset: dict[str, Any], *, timeout_s: float = 30.0) -> Path:
        path = self.thumbnail_path(asset["asset_id"])
        if path.is_file() and path.stat().st_size > 0:
            self._touch(asset["asset_id"], path, "thumb")
            return path

        with self._lock:
            future = self._inflight.get(asset["asset_id"])
            if future is None:
                future = self._pool.submit(self._render_thumbnail, asset)
                self._inflight[asset["asset_id"]] = future

        try:
            return future.result(timeout=timeout_s)
        finally:
            with self._lock:
                if self._inflight.get(asset["asset_id"]) is future:
                    self._inflight.pop(asset["asset_id"], None)

    def _render_thumbnail(self, asset: dict[str, Any]) -> Path:
        source = Path(asset["path"])
        if not source.is_file():
            raise PreviewError("source file is missing")
        duration = asset.get("duration_ms") or 0
        timestamp_ms = min(1000, max(0, duration // 10)) if duration else 0
        binary = find_tool("ffmpeg")
        if not binary:
            raise PreviewError("ffmpeg not found on PATH")
        target = self.thumbnail_path(asset["asset_id"])
        tmp = target.with_suffix(".tmp.jpg")
        completed = subprocess.run(  # noqa: S603 - argv only
            [
                binary,
                "-nostdin",
                "-v",
                "error",
                "-y",
                "-i",
                str(source),
                "-ss",
                f"{timestamp_ms / 1000.0:.3f}",
                "-frames:v",
                "1",
                "-vf",
                _longest_edge_filter(THUMBNAIL_MAX_EDGE),
                "-q:v",
                "5",
                str(tmp),
            ],
            capture_output=True,
            timeout=60,
            check=False,
        )
        if completed.returncode != 0 or not tmp.is_file() or tmp.stat().st_size == 0:
            tmp.unlink(missing_ok=True)
            detail = completed.stderr.decode("utf-8", "replace").strip().splitlines()
            raise PreviewError(detail[-1] if detail else "thumbnail extraction failed")
        os.replace(tmp, target)
        self._record(target, "thumb")
        return target

    # -- proxies ----------------------------------------------------------- #
    def proxy_path(self, asset_id: str) -> Path:
        return self.proxies_dir / f"{asset_id}.mp4"

    def get_proxy(self, asset: dict[str, Any]) -> ProxyResult:
        existing = self.proxy_path(asset["asset_id"])
        if existing.is_file() and existing.stat().st_size > 0:
            self._touch(asset["asset_id"], existing, "proxy")
            return ProxyResult(path=existing, created=False, duration_ms=None, mapping_delta_ms=None)

        source = Path(asset["path"])
        if not source.is_file():
            raise PreviewError("source file is missing")
        binary = find_tool("ffmpeg")
        if not binary:
            raise PreviewError("ffmpeg not found on PATH")

        target = self.proxy_path(asset["asset_id"])
        tmp = target.with_suffix(".tmp.mp4")
        argv = [
            binary,
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-vf",
            _longest_edge_filter(PROXY_MAX_EDGE),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            str(tmp),
        ]
        completed = subprocess.run(argv, capture_output=True, timeout=1800, check=False)  # noqa: S603
        if completed.returncode != 0 or not tmp.is_file() or tmp.stat().st_size == 0:
            tmp.unlink(missing_ok=True)
            detail = completed.stderr.decode("utf-8", "replace").strip().splitlines()
            raise PreviewError(detail[-1] if detail else "proxy transcode failed")

        mapping_delta = self._verify_mapping(source, tmp)
        os.replace(tmp, target)
        self._record(target, "proxy")
        self.enforce_quota()
        return ProxyResult(
            path=target,
            created=True,
            duration_ms=asset.get("duration_ms"),
            mapping_delta_ms=mapping_delta,
        )

    @staticmethod
    def _verify_mapping(source: Path, proxy: Path) -> int:
        """Probe both files and refuse the proxy if durations disagree."""

        try:
            source_info = probe_source(source)
            proxy_info = probe_source(proxy)
        except ProbeError as exc:
            raise PreviewError(f"proxy verification failed: {exc}") from exc
        delta = abs(proxy_info.duration_ms - source_info.duration_ms)
        tolerance = max(PROXY_DURATION_TOLERANCE_MS, int(source_info.duration_ms * 0.01))
        if delta > tolerance:
            raise PreviewError(
                f"proxy duration differs from source by {delta} ms (tolerance {tolerance} ms)"
            )
        return delta

    # -- cache ------------------------------------------------------------- #
    def _record(self, path: Path, kind: str) -> None:
        with self.db.write() as conn:
            store.record_cache_entry(
                conn,
                path=str(path),
                kind=kind,
                size_bytes=path.stat().st_size,
                atime=int(time.time()),
            )

    def _touch(self, asset_id: str, path: Path, kind: str) -> None:
        try:
            with self.db.write() as conn:
                store.touch_cache_entry(conn, str(path), int(time.time()))
        except Exception:  # noqa: BLE001 - a missing bookkeeping row must not break playback
            pass

    def enforce_quota(self) -> dict[str, int]:
        """Evict least-recently-used cache files until under quota."""

        removed = 0
        freed = 0
        with self.db.read() as conn:
            entries = store.list_cache_entries(conn)
        total = sum(entry["size_bytes"] for entry in entries)
        if total <= self.quota_bytes:
            return {"removed": 0, "freed_bytes": 0, "total_bytes": total}

        for entry in entries:
            if total <= self.quota_bytes:
                break
            if entry["pinned"]:
                continue
            path = Path(entry["path"])
            if not is_cache_path(path, self.thumbs_dir, self.proxies_dir):
                continue
            try:
                size = path.stat().st_size if path.exists() else 0
                path.unlink(missing_ok=True)
            except OSError:
                continue
            with self.db.write() as conn:
                store.delete_cache_entry(conn, entry["path"])
            total -= size
            freed += size
            removed += 1
        return {"removed": removed, "freed_bytes": freed, "total_bytes": max(total, 0)}

    def stats(self) -> dict[str, Any]:
        with self.db.read() as conn:
            by_kind = store.cache_stats(conn)
        return {"quota_bytes": self.quota_bytes, "kinds": by_kind}

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


def is_cache_path(path: Path, thumbs_dir: Path, proxies_dir: Path) -> bool:
    """True only for files inside the two cache directories (never user data)."""

    try:
        resolved = path.resolve()
    except OSError:
        return False
    for root in (thumbs_dir.resolve(), proxies_dir.resolve()):
        if resolved == root or root in resolved.parents:
            return True
    return False


def disk_free_gib(path: Path) -> float:
    usage = shutil.disk_usage(str(path))
    return usage.free / 1024**3
