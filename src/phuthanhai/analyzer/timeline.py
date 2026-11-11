"""Timeline and verified frame extraction.

Two rules drive this module:

1. **The timestamp comes from the decoder, not from the seek request.** FFmpeg is
   invoked with ``-copyts`` and ``showinfo``; the PTS printed for the frame that
   actually passed the filter is recorded as the frame's source PTS. The requested
   time is kept separately so a mismatch is visible instead of being papered over.
2. **The contract timeline starts at the first presented video frame.** Real
   files can start at 2.0 s (edit lists, concatenation) or at a negative PTS
   (transport streams). ``normalize_ms`` subtracts that origin.

The display rotation is taken from the container matrix; extraction still lets
FFmpeg autorotate and scales by the *displayed* frame, which is the only way a
4K landscape-stored portrait clip keeps its orientation.
"""

from __future__ import annotations

import hashlib
import json
import re
import statistics
import subprocess
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any

from PIL import Image

from phuthanhai.analyzer.probe import find_tool, tool_version

EXTRACTOR_VERSION = "s02-extractor-1"
HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}

_PTS_TIME = re.compile(r"pts_time:(-?[0-9.]+)")


class TimelineError(RuntimeError):
    code = "PTS_UNVERIFIED"

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Timeline:
    filename: str
    duration_ms: int
    first_video_pts_ms: int
    time_base: str | None
    fps_nominal: Fraction | None
    avg_frame_rate: Fraction | None
    vfr_detected: bool | None
    vfr_evidence: dict[str, Any]
    width: int
    height: int
    rotation: int
    display_width: int
    display_height: int
    sample_aspect_ratio: str | None
    display_aspect_ratio: str | None
    codec: str
    has_audio: bool
    color_transfer: str | None
    color_primaries: str | None
    color_space: str | None
    pix_fmt: str | None
    is_hdr: bool

    @property
    def frame_duration_ms(self) -> int:
        if not self.fps_nominal:
            return 40
        return max(1, int(round(1000 / float(self.fps_nominal))))

    def normalize_ms(self, source_pts_ms: int) -> int:
        """Map an absolute source PTS onto the contract timeline (origin = 0)."""

        return source_pts_ms - self.first_video_pts_ms

    def denormalize_ms(self, normalized_ms: int) -> int:
        return normalized_ms + self.first_video_pts_ms

    def to_dict(self) -> dict[str, Any]:
        return {
            "filename": self.filename,
            "duration_ms": self.duration_ms,
            "first_video_pts_ms": self.first_video_pts_ms,
            "time_base": self.time_base,
            "fps_nominal": None if self.fps_nominal is None else f"{self.fps_nominal.numerator}/{self.fps_nominal.denominator}",
            "avg_frame_rate": None
            if self.avg_frame_rate is None
            else f"{self.avg_frame_rate.numerator}/{self.avg_frame_rate.denominator}",
            "vfr_detected": self.vfr_detected,
            "vfr_evidence": self.vfr_evidence,
            "width": self.width,
            "height": self.height,
            "rotation": self.rotation,
            "display_width": self.display_width,
            "display_height": self.display_height,
            "sample_aspect_ratio": self.sample_aspect_ratio,
            "display_aspect_ratio": self.display_aspect_ratio,
            "codec": self.codec,
            "has_audio": self.has_audio,
            "color_transfer": self.color_transfer,
            "color_primaries": self.color_primaries,
            "color_space": self.color_space,
            "pix_fmt": self.pix_fmt,
            "is_hdr": self.is_hdr,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Timeline":
        def frac(value: str | None) -> Fraction | None:
            if not value:
                return None
            try:
                return Fraction(value)
            except (ValueError, ZeroDivisionError):
                return None

        return cls(
            filename=data["filename"],
            duration_ms=int(data["duration_ms"]),
            first_video_pts_ms=int(data["first_video_pts_ms"]),
            time_base=data.get("time_base"),
            fps_nominal=frac(data.get("fps_nominal")),
            avg_frame_rate=frac(data.get("avg_frame_rate")),
            vfr_detected=data.get("vfr_detected"),
            vfr_evidence=data.get("vfr_evidence") or {},
            width=int(data["width"]),
            height=int(data["height"]),
            rotation=int(data.get("rotation", 0)),
            display_width=int(data.get("display_width", data["width"])),
            display_height=int(data.get("display_height", data["height"])),
            sample_aspect_ratio=data.get("sample_aspect_ratio"),
            display_aspect_ratio=data.get("display_aspect_ratio"),
            codec=data.get("codec", "unknown"),
            has_audio=bool(data.get("has_audio")),
            color_transfer=data.get("color_transfer"),
            color_primaries=data.get("color_primaries"),
            color_space=data.get("color_space"),
            pix_fmt=data.get("pix_fmt"),
            is_hdr=bool(data.get("is_hdr")),
        )


def ffmpeg_version() -> str:
    version = tool_version("ffmpeg")
    if not version:
        raise TimelineError("FFMPEG_MISSING", "ffmpeg not found on PATH")
    match = re.search(r"ffmpeg version ([0-9][^ ]*)", version)
    return match.group(1) if match else version[:40]


def _rate(value: str | None) -> Fraction | None:
    if not value or value in {"0/0", "N/A"}:
        return None
    try:
        return Fraction(value)
    except (ValueError, ZeroDivisionError):
        return None


def _probe(path: Path) -> dict[str, Any]:
    binary = find_tool("ffprobe")
    if not binary:
        raise TimelineError("FFPROBE_MISSING", "ffprobe not found on PATH")
    try:
        completed = subprocess.run(  # noqa: S603 - argv list
            [
                binary, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path),
            ],
            capture_output=True,
            timeout=60,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimelineError("FFMPEG_TIMEOUT", "ffprobe timed out") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip().splitlines()
        raise TimelineError("CODEC_UNSUPPORTED", detail[-1] if detail else "ffprobe failed")
    return json.loads(completed.stdout.decode("utf-8", "replace"))


def _rotation(stream: dict[str, Any]) -> int:
    for side in stream.get("side_data_list", []) or []:
        if isinstance(side, dict) and "rotation" in side:
            try:
                return int(float(side["rotation"])) % 360
            except (TypeError, ValueError):
                continue
    tags = stream.get("tags") or {}
    raw = tags.get("rotate")
    if raw is not None:
        try:
            return int(float(raw)) % 360
        except (TypeError, ValueError):
            return 0
    return 0


def _vfr_evidence(path: Path, fps_nominal: Fraction | None, avg: Fraction | None) -> tuple[bool | None, dict[str, Any]]:
    """Sample packet deltas; VFR needs *evidence*, not a guess."""

    binary = find_tool("ffprobe")
    if not binary:
        return None, {"error": "ffprobe missing"}
    completed = subprocess.run(  # noqa: S603
        [
            binary, "-v", "error", "-select_streams", "v:0",
            "-show_entries", "packet=pts_time", "-read_intervals", "%+3",
            "-of", "csv=p=0", str(path),
        ],
        capture_output=True,
        timeout=60,
        check=False,
    )
    evidence: dict[str, Any] = {"rate_mismatch": None, "packet_count": 0}
    if completed.returncode == 0:
        times: list[float] = []
        for line in completed.stdout.decode("utf-8", "replace").splitlines():
            text = line.strip().strip(",")
            if not text or text == "N/A":
                continue
            try:
                times.append(float(text))
            except ValueError:
                continue
        times = sorted(times)[:300]
        deltas = [round(b - a, 6) for a, b in zip(times, times[1:]) if b > a]
        evidence["packet_count"] = len(deltas)
        if len(deltas) >= 5:
            median = statistics.median(deltas)
            evidence.update(
                {
                    "median_delta_s": round(median, 6),
                    "min_delta_s": round(min(deltas), 6),
                    "max_delta_s": round(max(deltas), 6),
                    "stdev_s": round(statistics.pstdev(deltas), 6),
                }
            )
            if median > 0:
                spread = max(deltas) / median
                evidence["max_over_median"] = round(spread, 3)
                if spread > 1.25 or (statistics.pstdev(deltas) / median) > 0.02:
                    return True, evidence

    if fps_nominal and avg and float(fps_nominal) > 0:
        evidence["rate_mismatch"] = round(abs(float(avg) - float(fps_nominal)) / float(fps_nominal), 4)
    if evidence["packet_count"] >= 5:
        # Regular packet deltas win: avg vs r_frame_rate can differ by ~1% because
        # of encoder metadata on a genuinely CFR file.
        return False, evidence
    if evidence.get("rate_mismatch") is not None and evidence["rate_mismatch"] > 0.01:
        return True, evidence
    return None, evidence


def read_timeline(path: Path) -> Timeline:
    data = _probe(path)
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise TimelineError("CODEC_UNSUPPORTED", "no video stream")
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fmt = data.get("format") or {}

    container_start = 0.0
    try:
        container_start = float(fmt.get("start_time") or 0.0)
    except (TypeError, ValueError):
        container_start = 0.0
    stream_start = video.get("start_time")
    try:
        start_seconds = float(stream_start) if stream_start not in (None, "N/A") else container_start
    except (TypeError, ValueError):
        start_seconds = container_start

    duration = None
    for candidate in (fmt.get("duration"), video.get("duration")):
        try:
            duration = float(candidate)
            break
        except (TypeError, ValueError):
            continue
    if not duration or duration <= 0:
        raise TimelineError("CODEC_UNSUPPORTED", "could not determine a positive duration")

    fps_nominal = _rate(video.get("r_frame_rate"))
    avg = _rate(video.get("avg_frame_rate"))
    vfr, evidence = _vfr_evidence(path, fps_nominal, avg)

    rotation = _rotation(video)
    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)
    swapped = rotation in (90, 270)
    display_width, display_height = (height, width) if swapped else (width, height)

    transfer = video.get("color_transfer")
    return Timeline(
        filename=path.name,
        duration_ms=int(round(duration * 1000)),
        first_video_pts_ms=int(round(start_seconds * 1000)),
        time_base=video.get("time_base"),
        fps_nominal=fps_nominal,
        avg_frame_rate=avg,
        vfr_detected=vfr,
        vfr_evidence=evidence,
        width=width,
        height=height,
        rotation=rotation,
        display_width=display_width,
        display_height=display_height,
        sample_aspect_ratio=video.get("sample_aspect_ratio"),
        display_aspect_ratio=video.get("display_aspect_ratio"),
        codec=str(video.get("codec_name") or "unknown"),
        has_audio=audio is not None,
        color_transfer=str(transfer) if transfer else None,
        color_primaries=video.get("color_primaries"),
        color_space=video.get("color_space"),
        pix_fmt=video.get("pix_fmt"),
        is_hdr=bool(transfer in HDR_TRANSFERS),
    )


# --------------------------------------------------------------------------- #
# Verified frame extraction                                                    #
# --------------------------------------------------------------------------- #


def longest_edge_filter(max_edge: int) -> str:
    if max_edge < 16:
        raise TimelineError("EXTRACTION_CONFIG_INVALID", "max_edge must be >= 16")
    return (
        f"scale='if(gte(iw,ih),min({max_edge},iw),-2)'"
        f":'if(gte(iw,ih),-2,min({max_edge},ih))'"
    )


@dataclass
class VerifiedFrame:
    path: Path
    requested_ms: int
    decoded_pts_ms: int
    normalized_pts_ms: int
    offset_ms: int
    pts_mismatch: bool
    width: int
    height: int
    rotation: int
    size_bytes: int
    sha256: str
    extractor_version: str = EXTRACTOR_VERSION
    ffmpeg_version: str = ""
    max_edge: int = 896
    quality: int = 4
    stderr_tail: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "requested_ms": self.requested_ms,
            "decoded_pts_ms": self.decoded_pts_ms,
            "normalized_pts_ms": self.normalized_pts_ms,
            "offset_ms": self.offset_ms,
            "pts_mismatch": self.pts_mismatch,
            "width": self.width,
            "height": self.height,
            "rotation": self.rotation,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "extractor_version": self.extractor_version,
            "ffmpeg_version": self.ffmpeg_version,
            "max_edge": self.max_edge,
            "quality": self.quality,
        }


def extract_verified_frame(
    source: Path,
    timeline: Timeline,
    requested_ms: int,
    output: Path,
    *,
    max_edge: int = 896,
    quality: int = 4,
    timeout_s: float = 120.0,
) -> VerifiedFrame:
    """Extract one frame and record the PTS the decoder actually produced."""

    binary = find_tool("ffmpeg")
    if not binary:
        raise TimelineError("FFMPEG_MISSING", "ffmpeg not found on PATH")
    if requested_ms < 0 or requested_ms >= timeline.duration_ms:
        raise TimelineError(
            "SAMPLING_PLAN_INVALID",
            f"requested {requested_ms} ms is outside the timeline (0..{timeline.duration_ms})",
        )
    # FFmpeg's input-side -ss is measured from the start of playback, not from
    # the absolute stream PTS. Seeking `T` therefore lands `first_pts + T`, which
    # normalizes back to `T`. (Empirically verified on the non-zero-start fixture.)
    source_seek_ms = max(0, requested_ms)
    output.parent.mkdir(parents=True, exist_ok=True)
    # Keep the real extension: FFmpeg picks the muxer from it.
    tmp = output.with_name(f"{output.stem}.tmp{output.suffix}")
    try:
        completed = subprocess.run(  # noqa: S603 - argv list, no shell
            [
                binary,
                "-nostdin",
                "-hide_banner",
                "-v",
                "info",
                "-copyts",
                "-ss",
                f"{source_seek_ms / 1000.0:.3f}",
                "-i",
                str(source),
                "-frames:v",
                "1",
                "-vf",
                f"showinfo,{longest_edge_filter(max_edge)}",
                "-q:v",
                str(quality),
                "-y",
                str(tmp),
            ],
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        tmp.unlink(missing_ok=True)
        raise TimelineError("FFMPEG_TIMEOUT", f"frame extraction exceeded {timeout_s}s") from exc
    stderr = completed.stderr.decode("utf-8", "replace")
    stderr_lines = [line for line in stderr.splitlines() if "showinfo" in line or "error" in line.lower()]
    if completed.returncode != 0 or not tmp.is_file() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        raise TimelineError(
            "FFMPEG_FAILED",
            stderr_lines[-1] if stderr_lines else "ffmpeg produced no frame",
        )

    pts_match = _PTS_TIME.search(stderr)
    if not pts_match:
        tmp.unlink(missing_ok=True)
        raise TimelineError("PTS_UNVERIFIED", "decoder did not report a frame PTS (showinfo missing)")
    decoded_pts_ms = int(round(float(pts_match.group(1)) * 1000))
    normalized = timeline.normalize_ms(decoded_pts_ms)
    offset = normalized - requested_ms
    tolerance = max(250, timeline.frame_duration_ms * 3)
    mismatch = abs(offset) > tolerance

    with Image.open(tmp) as image:
        width, height = image.size
    digest = hashlib.sha256(tmp.read_bytes()).hexdigest()
    tmp.replace(output)
    return VerifiedFrame(
        path=output,
        requested_ms=requested_ms,
        decoded_pts_ms=decoded_pts_ms,
        normalized_pts_ms=normalized,
        offset_ms=offset,
        pts_mismatch=mismatch,
        width=width,
        height=height,
        rotation=timeline.rotation,
        size_bytes=output.stat().st_size,
        sha256=digest,
        ffmpeg_version=ffmpeg_version(),
        max_edge=max_edge,
        quality=quality,
        stderr_tail=stderr_lines[-3:],
    )
