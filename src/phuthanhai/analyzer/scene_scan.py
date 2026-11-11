"""Full-timeline scene scan using FFmpeg's cheap per-frame metrics.

One decode pass at 160 px produces, for every frame: luma/chroma averages,
temporal difference (``YDIF``), a blur score and FFmpeg's scene score. From those
we derive:

* **hard cuts** - a deterministic delta over luma+chroma averages (FFmpeg's own
  ``scdet`` misses the first cut on short fixtures and is used only as extra
  evidence);
* **dark / blurred / high-motion flags** - aggregated per segment;
* **segments** - ``[start_ms, end_ms)``, sorted, non-overlapping and covering the
  whole timeline, with long scenes subdivided.

A motion score is a *technical cue*: it says "pixels changed a lot", never "a
person boarded a ferry".
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from phuthanhai.analyzer.probe import find_tool
from phuthanhai.analyzer.timeline import EXTRACTOR_VERSION, Timeline, ffmpeg_version
from phuthanhai.utils.fs import FsError, atomic_write_json, read_json_strict

SCAN_VERSION = "s02-scan-1"
# Apple VideoToolbox is opt-out for this offline macOS app; unsupported hosts fall
# back to software decode inside scan_scenes(). Quality parity is covered by tests.
SCAN_HWACCEL_DEFAULT = os.environ.get("PHUTHANHAI_SCAN_HWACCEL", "1") != "0"
SCAN_WIDTH = 160
CUT_DELTA_THRESHOLD = 30.0
SCD_SCORE_THRESHOLD = 15.0
MIN_CUT_GAP_MS = 100
DARK_LUMA_THRESHOLD = 40.0
BLUR_THRESHOLD = 12.0

_FRAME_RE = re.compile(r"^frame:\s*(\d+)\s+pts:\s*(-?\d+)\s+pts_time:(-?[0-9.]+)")
_KEY_RE = re.compile(r"^(lavfi\.[A-Za-z0-9.]+)=(-?[0-9.]+)$")


class ScanError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class FrameMetric:
    index: int
    pts_ms: int
    yavg: float
    uavg: float
    vavg: float
    ydif: float
    blur: float
    scd_score: float


@dataclass
class SceneSegment:
    scene_id: str
    start_ms: int
    end_ms: int
    cut_in: bool
    dark_ratio: float
    blur_ratio: float
    mean_luma: float
    max_motion: float
    motion_peak_ms: int | None
    frame_count: int

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms

    def to_dict(self) -> dict[str, Any]:
        return {
            "scene_id": self.scene_id,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "duration_ms": self.duration_ms,
            "cut_in": self.cut_in,
            "dark_ratio": round(self.dark_ratio, 4),
            "blur_ratio": round(self.blur_ratio, 4),
            "mean_luma": round(self.mean_luma, 2),
            "max_motion": round(self.max_motion, 3),
            "motion_peak_ms": self.motion_peak_ms,
            "frame_count": self.frame_count,
        }


@dataclass
class SceneScan:
    timeline: Timeline
    segments: list[SceneSegment]
    cut_times_ms: list[int]
    frame_metrics: list[FrameMetric]
    elapsed_s: float
    command: list[str]
    config: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {
            "scan_version": SCAN_VERSION,
            "extractor_version": EXTRACTOR_VERSION,
            "ffmpeg_version": ffmpeg_version(),
            "elapsed_s": round(self.elapsed_s, 3),
            "frames_analyzed": len(self.frame_metrics),
            "cuts": len(self.cut_times_ms),
            "cut_times_ms": self.cut_times_ms[:200],
            "segments": [segment.to_dict() for segment in self.segments],
            "frame_metrics": [asdict(metric) for metric in self.frame_metrics],
            "config": self.config,
            "command": self.command,
        }


def _run_scan(path: Path, timeline: Timeline, *, timeout_s: float,
              hwaccel: bool = False) -> tuple[list[FrameMetric], str]:
    binary = find_tool("ffmpeg")
    if not binary:
        raise ScanError("FFMPEG_MISSING", "ffmpeg not found on PATH")
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as handle:
        metadata_path = Path(handle.name)
    metadata_path.unlink(missing_ok=True)
    command = [
        binary,
        "-nostdin",
        "-hide_banner",
        "-v",
        "error",
        "-y",
    ]
    if hwaccel:
        command += ["-hwaccel", "videotoolbox"]
    command += [
        "-i",
        str(path),
        "-an",
        "-vf",
        (
            f"scale={SCAN_WIDTH}:-2,"
            f"signalstats,scdet=threshold={SCD_SCORE_THRESHOLD:g},blurdetect,"
            f"metadata=mode=print:file={metadata_path}"
        ),
        "-f",
        "null",
        "-",
    ]
    try:
        try:
            completed = subprocess.run(  # noqa: S603 - argv list, no shell
                command, capture_output=True, timeout=timeout_s, check=False
            )
        except subprocess.TimeoutExpired as exc:
            raise ScanError("FFMPEG_TIMEOUT", f"scene scan exceeded {timeout_s}s") from exc
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", "replace").strip().splitlines()
            raise ScanError("FFMPEG_FAILED", detail[-1] if detail else "scene scan failed")
        if not metadata_path.is_file():
            raise ScanError("SCAN_EMPTY", "ffmpeg produced no scan metadata")
        metrics = _parse_metrics(metadata_path.read_text(encoding="utf-8", errors="replace"))
    finally:
        metadata_path.unlink(missing_ok=True)

    if not metrics:
        raise ScanError("SCAN_EMPTY", "scene scan produced zero frames")
    base = metrics[0].pts_ms
    if base != 0:
        metrics = [
            FrameMetric(m.index, m.pts_ms - base, m.yavg, m.uavg, m.vavg, m.ydif, m.blur, m.scd_score)
            for m in metrics
        ]
    return metrics, command


def _parse_metrics(text: str) -> list[FrameMetric]:
    metrics: list[FrameMetric] = []
    current: dict[str, Any] | None = None
    for line in text.splitlines():
        stripped = line.strip()
        match = _FRAME_RE.match(stripped)
        if match:
            if current is not None:
                metrics.append(_build_metric(current))
            current = {
                "index": int(match.group(1)),
                "pts_ms": int(round(float(match.group(3)) * 1000)),
                "yavg": 0.0,
                "uavg": 0.0,
                "vavg": 0.0,
                "ydif": 0.0,
                "blur": 0.0,
                "scd_score": 0.0,
            }
            continue
        if current is None:
            continue
        key_match = _KEY_RE.match(stripped)
        if not key_match:
            continue
        key, value = key_match.group(1), float(key_match.group(2))
        if key == "lavfi.signalstats.YAVG":
            current["yavg"] = value
        elif key == "lavfi.signalstats.UAVG":
            current["uavg"] = value
        elif key == "lavfi.signalstats.VAVG":
            current["vavg"] = value
        elif key == "lavfi.signalstats.YDIF":
            current["ydif"] = value
        elif key == "lavfi.blur":
            current["blur"] = value
        elif key == "lavfi.scd.score":
            current["scd_score"] = value
    if current is not None:
        metrics.append(_build_metric(current))
    return metrics


def _build_metric(raw: dict[str, Any]) -> FrameMetric:
    return FrameMetric(
        index=int(raw["index"]),
        pts_ms=int(raw["pts_ms"]),
        yavg=float(raw["yavg"]),
        uavg=float(raw["uavg"]),
        vavg=float(raw["vavg"]),
        ydif=float(raw["ydif"]),
        blur=float(raw["blur"]),
        scd_score=float(raw["scd_score"]),
    )


def detect_cuts(metrics: list[FrameMetric], duration_ms: int) -> list[int]:
    """Cut = large luma/chroma jump between consecutive frames.

    The first presented frame is never a cut. Cuts closer than ``MIN_CUT_GAP_MS``
    are merged (rapid 250 ms cuts are still kept).
    """

    cuts: list[int] = []
    previous: FrameMetric | None = None
    for metric in metrics:
        if previous is not None:
            delta = (
                abs(metric.yavg - previous.yavg)
                + abs(metric.uavg - previous.uavg)
                + abs(metric.vavg - previous.vavg)
            )
            if delta >= CUT_DELTA_THRESHOLD or metric.scd_score >= SCD_SCORE_THRESHOLD:
                if not cuts or metric.pts_ms - cuts[-1] >= MIN_CUT_GAP_MS:
                    if 0 < metric.pts_ms < duration_ms:
                        cuts.append(metric.pts_ms)
        previous = metric
    return cuts


def build_segments(
    timeline: Timeline,
    metrics: list[FrameMetric],
    cuts: list[int],
    *,
    max_scene_ms: int = 30_000,
) -> list[SceneSegment]:
    duration = timeline.duration_ms
    boundaries = [0] + [cut for cut in cuts if 0 < cut < duration] + [duration]
    windows: list[tuple[int, int, bool]] = []
    for index in range(len(boundaries) - 1):
        start, end = boundaries[index], boundaries[index + 1]
        if end <= start:
            continue
        cut_in = index > 0
        length = end - start
        if length <= max_scene_ms:
            windows.append((start, end, cut_in))
        else:
            parts = (length + max_scene_ms - 1) // max_scene_ms
            step = length / parts
            for part in range(parts):
                part_start = start + int(round(part * step))
                part_end = end if part == parts - 1 else start + int(round((part + 1) * step))
                windows.append((part_start, part_end, cut_in and part == 0))

    segments: list[SceneSegment] = []
    for index, (start, end, cut_in) in enumerate(windows, start=1):
        bucket = [m for m in metrics if start <= m.pts_ms < end]
        if not bucket:
            continue
        dark = sum(1 for m in bucket if m.yavg < DARK_LUMA_THRESHOLD) / len(bucket)
        blurred = sum(1 for m in bucket if m.blur > BLUR_THRESHOLD) / len(bucket)
        peak = max(bucket, key=lambda m: m.ydif)
        segments.append(
            SceneSegment(
                scene_id=f"S{index:04d}",
                start_ms=start,
                end_ms=end,
                cut_in=cut_in,
                dark_ratio=dark,
                blur_ratio=blurred,
                mean_luma=sum(m.yavg for m in bucket) / len(bucket),
                max_motion=peak.ydif,
                motion_peak_ms=peak.pts_ms,
                frame_count=len(bucket),
            )
        )
    if not segments:
        raise ScanError("SCAN_EMPTY", "no segments could be built")
    # Windows shorter than one frame can contain no metrics; close the resulting
    # gaps by extending neighbours so coverage stays [0, duration) with no holes.
    for index in range(len(segments) - 1):
        segments[index].end_ms = segments[index + 1].start_ms
    segments[-1].end_ms = duration
    segments[0] = SceneSegment(
        scene_id="S0001",
        start_ms=0,
        end_ms=segments[0].end_ms,
        cut_in=False,
        dark_ratio=segments[0].dark_ratio,
        blur_ratio=segments[0].blur_ratio,
        mean_luma=segments[0].mean_luma,
        max_motion=segments[0].max_motion,
        motion_peak_ms=segments[0].motion_peak_ms,
        frame_count=segments[0].frame_count,
    )
    _validate_coverage(segments, duration)
    return segments


def _validate_coverage(segments: list[SceneSegment], duration: int) -> None:
    if segments[0].start_ms != 0:
        raise ScanError("SCENE_COVERAGE_INVALID", "first segment must start at 0")
    if segments[-1].end_ms != duration:
        raise ScanError("SCENE_COVERAGE_INVALID", "last segment must end at the source duration")
    for previous, current in zip(segments, segments[1:]):
        if previous.end_ms != current.start_ms:
            raise ScanError(
                "SCENE_COVERAGE_INVALID",
                f"gap/overlap between {previous.scene_id} and {current.scene_id}",
            )


def scan_scenes(
    path: Path,
    timeline: Timeline,
    *,
    cache_path: Path | None = None,
    max_scene_ms: int = 30_000,
    timeout_s: float = 1800.0,
    hwaccel: bool = False,
) -> SceneScan:
    """Scan the full timeline, or reuse a cached scan for the same fingerprint."""

    config = {
        "scan_version": SCAN_VERSION,
        "width": SCAN_WIDTH,
        "cut_delta_threshold": CUT_DELTA_THRESHOLD,
        "scd_score_threshold": SCD_SCORE_THRESHOLD,
        "dark_luma_threshold": DARK_LUMA_THRESHOLD,
        "blur_threshold": BLUR_THRESHOLD,
        "max_scene_ms": max_scene_ms,
        "hwaccel": hwaccel,
    }
    if cache_path is not None and cache_path.is_file():
        try:
            cached = read_json_strict(cache_path)
            if cached.get("config") == config and cached.get("ffmpeg_version") == ffmpeg_version():
                segments = [
                    SceneSegment(
                        scene_id=item["scene_id"],
                        start_ms=int(item["start_ms"]),
                        end_ms=int(item["end_ms"]),
                        cut_in=bool(item["cut_in"]),
                        dark_ratio=float(item["dark_ratio"]),
                        blur_ratio=float(item["blur_ratio"]),
                        mean_luma=float(item["mean_luma"]),
                        max_motion=float(item["max_motion"]),
                        motion_peak_ms=item.get("motion_peak_ms"),
                        frame_count=int(item["frame_count"]),
                    )
                    for item in cached["segments"]
                ]
                _validate_coverage(segments, timeline.duration_ms)
                raw_metrics = cached.get("frame_metrics")
                expected = int(cached.get("frames_analyzed") or 0)
                if not isinstance(raw_metrics, list) or len(raw_metrics) != expected:
                    raise ValueError("cached scan lacks frame metrics")
                metrics = [FrameMetric(
                    index=int(item["index"]), pts_ms=int(item["pts_ms"]),
                    yavg=float(item["yavg"]), uavg=float(item["uavg"]), vavg=float(item["vavg"]),
                    ydif=float(item["ydif"]), blur=float(item["blur"]), scd_score=float(item["scd_score"]),
                ) for item in raw_metrics]
                return SceneScan(
                    timeline=timeline,
                    segments=segments,
                    cut_times_ms=[int(v) for v in cached.get("cut_times_ms", [])],
                    frame_metrics=metrics,
                    elapsed_s=0.0,
                    command=["cached"],
                    config=config,
                )
        except (OSError, ValueError, KeyError, FsError):
            pass  # corrupted cache is rebuilt, never trusted

    started = time.monotonic()
    try:
        metrics, command = _run_scan(path, timeline, timeout_s=timeout_s, hwaccel=hwaccel)
    except ScanError:
        if not hwaccel:
            raise
        metrics, command = _run_scan(path, timeline, timeout_s=timeout_s, hwaccel=False)
    cuts = detect_cuts(metrics, timeline.duration_ms)
    segments = build_segments(timeline, metrics, cuts, max_scene_ms=max_scene_ms)
    scan = SceneScan(
        timeline=timeline,
        segments=segments,
        cut_times_ms=cuts,
        frame_metrics=metrics,
        elapsed_s=time.monotonic() - started,
        command=command,
        config=config,
    )
    if cache_path is not None:
        atomic_write_json(cache_path, {**scan.summary(), "ffmpeg_version": ffmpeg_version()})
    return scan
