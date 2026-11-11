"""S02 sampling pipeline: timeline -> scene scan -> plan -> verified frames.

Guarantees:

* every frame has a decoder-verified PTS, a stable ``frame_id`` and its sampling
  reason; requested time is kept separately from actual time;
* the cache key includes source SHA-256, profile, extractor/FFmpeg/scan versions
  and extraction settings, so a note edit can never invalidate frames and a
  source edit always does;
* checkpoints are atomic and resumable; a resumed run never re-extracts a
  committed frame or duplicates an id;
* the source file is re-checked (size/mtime) while running and a mid-run change
  aborts with ``SOURCE_CHANGED`` instead of mixing two file versions;
* disk-full is surfaced as ``STORAGE_FULL``, never swallowed.
"""

from __future__ import annotations

import errno
import hashlib
import json
import shutil
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from phuthanhai.analyzer.sampling import (
    PROFILE_GAPS_MS,
    FrameTarget,
    SamplingPlan,
    SamplingPlanError,
    build_plan,
    observed_max_gap,
)
from phuthanhai.analyzer.scene_scan import SCAN_VERSION, SceneScan, ScanError, SceneSegment, scan_scenes
from phuthanhai.analyzer.timeline import (
    EXTRACTOR_VERSION,
    Timeline,
    TimelineError,
    extract_verified_frame,
    ffmpeg_version,
    read_timeline,
)
from phuthanhai.library import store
from phuthanhai.library.db import Database
from phuthanhai.runtime.checkpoint import Checkpoint, CheckpointError, read_checkpoint, write_checkpoint
from phuthanhai.utils.fs import FsError, atomic_write_json
from phuthanhai.utils.hashes import sha256_file

PIPELINE_VERSION = "s02-pipeline-1"
DEFAULT_MAX_EDGE = 896
DEFAULT_QUALITY = 4
CHECKPOINT_EVERY = 5
SOURCE_CHECK_EVERY = 10


class SamplingError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class SamplingOutcome:
    asset_id: str
    status: str  # complete | cancelled
    fingerprint: str
    cache_dir: Path
    profile: str
    timeline: Timeline
    segments: list[SceneSegment]
    plan: SamplingPlan
    frames: list[dict[str, Any]]
    extracted: int
    cache_hits: int
    elapsed_s: float
    observed_max_gap_ms: int
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "status": self.status,
            "fingerprint": self.fingerprint,
            "profile": self.profile,
            "extracted": self.extracted,
            "cache_hits": self.cache_hits,
            "frame_count": len(self.frames),
            "elapsed_s": round(self.elapsed_s, 3),
            "observed_max_gap_ms": self.observed_max_gap_ms,
            "target_max_gap_ms": self.plan.max_gap_ms,
            "scene_count": len(self.segments),
            "warnings": self.warnings,
        }


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def sampling_fingerprint(
    *,
    source_sha256: str,
    profile: str,
    max_edge: int,
    quality: int,
    max_scene_ms: int,
) -> str:
    payload = {
        "pipeline_version": PIPELINE_VERSION,
        "source_sha256": source_sha256,
        "profile": profile,
        "profile_gap_ms": PROFILE_GAPS_MS.get(profile),
        "extractor_version": EXTRACTOR_VERSION,
        "scan_version": SCAN_VERSION,
        "ffmpeg_version": ffmpeg_version(),
        "max_edge": max_edge,
        "quality": quality,
        "max_scene_ms": max_scene_ms,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _is_disk_full(exc: OSError) -> bool:
    return exc.errno in (errno.ENOSPC, getattr(errno, "EDQUOT", -1))


def _source_stat(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


class SamplingPipeline:
    def __init__(self, db: Database, cache_root: Path) -> None:
        self.db = db
        self.cache_root = cache_root
        self.cache_root.mkdir(parents=True, exist_ok=True, mode=0o700)

    # -- helpers ----------------------------------------------------------- #
    def _asset_sha(self, asset: dict[str, Any], path: Path) -> str:
        """Return the content hash, recomputing when the file no longer matches.

        A cached SHA-256 is only valid while size/mtime still match the registry
        row; otherwise a replaced file would silently reuse the old fingerprint,
        cache and scan of different footage.
        """

        asset_id = asset["asset_id"]
        stat = path.stat()
        matches_registry = (
            int(asset.get("size_bytes", -1)) == stat.st_size
            and int(asset.get("mtime_ns", -1)) == stat.st_mtime_ns
        )
        with self.db.read() as conn:
            row = conn.execute("SELECT sha256 FROM assets WHERE asset_id = ?", (asset_id,)).fetchone()
        if row is not None and row["sha256"] and matches_registry:
            return str(row["sha256"])
        digest = sha256_file(path)
        with self.db.write() as conn:
            store.set_asset_hash(conn, asset_id, digest)
        return digest

    def _cache_dir(self, asset_id: str, fingerprint: str) -> Path:
        return self.cache_root / asset_id / fingerprint[:20]

    def _load_plan(self, path: Path) -> SamplingPlan | None:
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            targets = [
                FrameTarget(
                    frame_id=item["frame_id"],
                    scene_id=item["scene_id"],
                    requested_ms=int(item["requested_ms"]),
                    reason=item["reason"],
                    kind=item["kind"],
                )
                for item in data["targets"]
            ]
            return SamplingPlan(
                profile=data["profile"],
                max_gap_ms=int(data["max_gap_ms"]),
                targets=targets,
                expected_max_gap_ms=int(data["expected_max_gap_ms"]),
                notes=list(data.get("notes", [])),
            )
        except (OSError, ValueError, KeyError):
            return None

    def _frame_is_reusable(self, record: dict[str, Any]) -> bool:
        image = Path(record.get("path", ""))
        if not image.is_file() or image.stat().st_size == 0:
            return False
        return True

    # -- main -------------------------------------------------------------- #
    def run(
        self,
        asset: dict[str, Any],
        source_path: Path,
        *,
        profile: str,
        max_edge: int = DEFAULT_MAX_EDGE,
        quality: int = DEFAULT_QUALITY,
        max_scene_ms: int = 30_000,
        progress: Callable[[dict[str, Any]], None] | None = None,
        cancel: threading.Event | None = None,
    ) -> SamplingOutcome:
        started = time.monotonic()
        asset_id = asset["asset_id"]

        def emit(stage: str, done: int = 0, total: int = 0) -> None:
            if progress is not None:
                progress(
                    {
                        "stage": stage,
                        "done": done,
                        "total": total,
                        "fraction": (done / total) if total else 0.0,
                    }
                )

        emit("hashing")
        source_sha = self._asset_sha(asset, source_path)
        initial_stat = _source_stat(source_path)

        emit("timeline")
        try:
            timeline = read_timeline(source_path)
        except TimelineError as exc:
            raise SamplingError(exc.code, str(exc)) from exc
        fingerprint = sampling_fingerprint(
            source_sha256=source_sha,
            profile=profile,
            max_edge=max_edge,
            quality=quality,
            max_scene_ms=max_scene_ms,
        )
        cache_dir = self._cache_dir(asset_id, fingerprint)
        cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        frames_dir = cache_dir / "frames"
        frames_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

        fingerprint_payload = {
            "source_sha256": source_sha,
            "profile": profile,
            "pipeline_version": PIPELINE_VERSION,
            "extractor_version": EXTRACTOR_VERSION,
            "ffmpeg_version": ffmpeg_version(),
            "max_edge": max_edge,
            "quality": quality,
        }
        checkpoint_path = cache_dir / "checkpoint.json"
        committed: dict[str, dict[str, Any]] = {}
        try:
            checkpoint = read_checkpoint(checkpoint_path, expected_fingerprint=fingerprint_payload)
            for record in checkpoint.payload.get("frames", []):
                committed[record["frame_id"]] = record
        except (CheckpointError, FsError, OSError, ValueError):
            committed = {}

        emit("scanning")
        try:
            scan = scan_scenes(
                source_path, timeline, cache_path=cache_dir / "scan.json", max_scene_ms=max_scene_ms
            )
        except ScanError as exc:
            raise SamplingError(exc.code, str(exc)) from exc
        if cancel is not None and cancel.is_set():
            return self._finish_cancelled(asset, fingerprint, cache_dir, profile, timeline, scan, committed, started)

        emit("planning")
        plan = self._load_plan(cache_dir / "plan.json")
        if plan is None:
            try:
                plan = build_plan(timeline, scan.segments, profile, metrics=scan.frame_metrics)
            except SamplingPlanError as exc:
                raise SamplingError(exc.code, str(exc)) from exc
            atomic_write_json(cache_dir / "plan.json", plan.to_dict())

        # A committed frame must still exist on disk; otherwise re-extract it.
        committed = {key: value for key, value in committed.items() if self._frame_is_reusable(value)}
        targets = plan.targets
        extracted = 0
        cache_hits = 0
        frames: list[dict[str, Any]] = []

        for index, target in enumerate(targets, start=1):
            if cancel is not None and cancel.is_set():
                write_checkpoint(
                    checkpoint_path,
                    Checkpoint(
                        job_id=asset_id,
                        stage="extracting",
                        fingerprint=fingerprint_payload,
                        committed_frame_ids=sorted(committed),
                        payload={"frames": [committed[key] for key in sorted(committed)]},
                    ),
                )
                return self._finish_cancelled(
                    asset, fingerprint, cache_dir, profile, timeline, scan, committed, started
                )

            existing = committed.get(target.frame_id)
            if existing is not None and self._frame_is_reusable(existing):
                frames.append(existing)
                cache_hits += 1
                emit("extracting", index, len(targets))
                continue

            if index % SOURCE_CHECK_EVERY == 0:
                if _source_stat(source_path) != initial_stat:
                    raise SamplingError(
                        "SOURCE_CHANGED",
                        "the source file changed while sampling; results were not committed",
                    )

            image_path = frames_dir / f"{target.frame_id}.jpg"
            try:
                verified = extract_verified_frame(
                    source_path,
                    timeline,
                    target.requested_ms,
                    image_path,
                    max_edge=max_edge,
                    quality=quality,
                )
            except TimelineError as exc:
                raise SamplingError(exc.code, str(exc)) from exc
            except OSError as exc:
                if _is_disk_full(exc):
                    raise SamplingError("STORAGE_FULL", "no space left while writing frame cache") from exc
                raise

            record = {
                **target.to_dict(),
                **verified.to_dict(),
                "stderr_tail": verified.stderr_tail[-1:],
            }
            committed[target.frame_id] = record
            frames.append(record)
            extracted += 1
            emit("extracting", index, len(targets))

            if extracted % CHECKPOINT_EVERY == 0:
                write_checkpoint(
                    checkpoint_path,
                    Checkpoint(
                        job_id=asset_id,
                        stage="extracting",
                        fingerprint=fingerprint_payload,
                        committed_frame_ids=sorted(committed),
                        payload={"frames": [committed[key] for key in sorted(committed)]},
                    ),
                )

        if _source_stat(source_path) != initial_stat:
            raise SamplingError("SOURCE_CHANGED", "the source file changed during sampling")

        frames.sort(key=lambda item: item["normalized_pts_ms"])
        write_checkpoint(
            checkpoint_path,
            Checkpoint(
                job_id=asset_id,
                stage="complete",
                fingerprint=fingerprint_payload,
                committed_frame_ids=sorted(committed),
                payload={"frames": [committed[key] for key in sorted(committed)]},
            ),
        )

        observed_gap = observed_max_gap([frame["normalized_pts_ms"] for frame in frames], timeline.duration_ms)
        covered_scenes = {frame["scene_id"] for frame in frames}
        uncovered = [segment.scene_id for segment in scan.segments if segment.scene_id not in covered_scenes]
        mismatches = [frame["frame_id"] for frame in frames if frame.get("pts_mismatch")]
        warnings: list[str] = []
        if mismatches:
            warnings.append(f"{len(mismatches)} frame(s) decoded with a PTS offset above tolerance")
        if uncovered:
            warnings.append(f"scenes without a sampled frame: {', '.join(uncovered)}")
        if observed_gap > plan.max_gap_ms:
            warnings.append(
                f"observed max gap {observed_gap} ms exceeds target {plan.max_gap_ms} ms"
            )

        manifest = {
            "manifest_version": "s02-sampling-1.0.0",
            "asset_id": asset_id,
            "project_id": asset["project_id"],
            "fingerprint": fingerprint,
            "source": {
                "filename": source_path.name,
                "sha256": source_sha,
                "size_bytes": initial_stat[0],
                "mtime_ns": initial_stat[1],
            },
            "profile": profile,
            "timeline": timeline.to_dict(),
            "segments": [segment.to_dict() for segment in scan.segments],
            "plan": plan.to_dict(),
            "frames": frames,
            "observed_max_gap_ms": observed_gap,
            "expected_max_gap_ms": plan.expected_max_gap_ms,
            "coverage": {
                "scene_count": len(scan.segments),
                "scenes_without_frames": uncovered,
                "pts_mismatch_frames": mismatches,
            },
            "settings": {"max_edge": max_edge, "quality": quality, "max_scene_ms": max_scene_ms},
            "created_at": _now_iso(),
            "warnings": warnings,
        }
        atomic_write_json(cache_dir / "manifest.json", manifest)

        # Old fingerprints for this asset are stale: remove them only after the
        # new run has fully committed, so a crash never destroys the last good cache.
        self._drop_stale_cache_dirs(asset_id, keep=cache_dir)

        with self.db.write() as conn:
            store.set_sampling_state(
                conn,
                asset_id,
                status="complete",
                profile=profile,
                fingerprint=fingerprint,
                sampled_at=manifest["created_at"],
            )

        return SamplingOutcome(
            asset_id=asset_id,
            status="complete",
            fingerprint=fingerprint,
            cache_dir=cache_dir,
            profile=profile,
            timeline=timeline,
            segments=scan.segments,
            plan=plan,
            frames=frames,
            extracted=extracted,
            cache_hits=cache_hits,
            elapsed_s=time.monotonic() - started,
            observed_max_gap_ms=observed_gap,
            warnings=warnings,
        )

    def _finish_cancelled(
        self,
        asset: dict[str, Any],
        fingerprint: str,
        cache_dir: Path,
        profile: str,
        timeline: Timeline,
        scan: SceneScan,
        committed: dict[str, dict[str, Any]],
        started: float,
    ) -> SamplingOutcome:
        with self.db.write() as conn:
            store.set_sampling_state(
                conn,
                asset["asset_id"],
                status="cancelled",
                profile=profile,
                fingerprint=fingerprint,
                sampled_at=None,
            )
        frames = [committed[key] for key in sorted(committed)]
        plan = self._load_plan(cache_dir / "plan.json") or SamplingPlan(
            profile=profile,
            max_gap_ms=PROFILE_GAPS_MS.get(profile, 6000),
            targets=[],
            expected_max_gap_ms=timeline.duration_ms,
        )
        return SamplingOutcome(
            asset_id=asset["asset_id"],
            status="cancelled",
            fingerprint=fingerprint,
            cache_dir=cache_dir,
            profile=profile,
            timeline=timeline,
            segments=scan.segments,
            plan=plan,
            frames=frames,
            extracted=0,
            cache_hits=len(frames),
            elapsed_s=time.monotonic() - started,
            observed_max_gap_ms=observed_max_gap(
                [frame["normalized_pts_ms"] for frame in frames], timeline.duration_ms
            ),
            warnings=["cancelled by operator; committed frames are resumable"],
        )

    def _drop_stale_cache_dirs(self, asset_id: str, *, keep: Path) -> None:
        asset_root = self.cache_root / asset_id
        if not asset_root.is_dir():
            return
        for candidate in asset_root.iterdir():
            if candidate.is_dir() and candidate.resolve() != keep.resolve():
                shutil.rmtree(candidate, ignore_errors=True)

    # -- read side --------------------------------------------------------- #
    def manifest_path(self, asset_id: str, fingerprint: str) -> Path:
        return self._cache_dir(asset_id, fingerprint) / "manifest.json"

    def load_manifest(self, asset_id: str, fingerprint: str) -> dict[str, Any] | None:
        path = self.manifest_path(asset_id, fingerprint)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
