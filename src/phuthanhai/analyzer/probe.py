"""FFprobe wrapper: source metadata and presentation timestamps.

``ffprobe`` is invoked with an argv list (never a shell string) so a filename
containing spaces, quotes or newlines cannot become a command. Output is parsed
as JSON and reduced to the fields the contracts need.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any


class ProbeError(RuntimeError):
    code = "CODEC_UNSUPPORTED"

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def find_tool(name: str) -> str | None:
    """Resolve ffmpeg/ffprobe from PATH; explicit argv only."""

    return shutil.which(name)


def tool_version(name: str) -> str | None:
    binary = find_tool(name)
    if not binary:
        return None
    try:
        completed = subprocess.run([binary, "-version"], capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.splitlines()[0] if completed.stdout else None


def ffprobe_json(path: Path, *, timeout_s: float = 30.0) -> dict[str, Any]:
    binary = find_tool("ffprobe")
    if not binary:
        raise ProbeError("FFPROBE_MISSING", "ffprobe not found on PATH")
    if not path.is_file():
        raise ProbeError("SOURCE_MISSING", f"source file not found: {path.name}")
    completed = subprocess.run(  # noqa: S603 - argv list, no shell
        [
            binary,
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        timeout=timeout_s,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip().splitlines()
        raise ProbeError("CODEC_UNSUPPORTED", detail[-1] if detail else "ffprobe failed")
    try:
        return json.loads(completed.stdout.decode("utf-8", "replace"))
    except json.JSONDecodeError as exc:
        raise ProbeError("CODEC_UNSUPPORTED", f"ffprobe emitted invalid JSON: {exc}") from exc


def _rate(value: str | None) -> Fraction | None:
    if not value or value in {"0/0", "N/A"}:
        return None
    try:
        return Fraction(value)
    except (ValueError, ZeroDivisionError):
        return None


def _start_time_ms(stream: dict[str, Any], container_start: float) -> int:
    for key in ("start_time", "start_pts"):
        raw = stream.get(key)
        if raw in (None, "N/A"):
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if key == "start_pts":
            time_base = _rate(stream.get("time_base"))
            if time_base is None:
                continue
            return int(round(float(value * time_base) * 1000))
        return int(round(value * 1000))
    return int(round(container_start * 1000))


def _rotation(stream: dict[str, Any]) -> int:
    raw = stream.get("tags", {}).get("rotate") if isinstance(stream.get("tags"), dict) else None
    if raw is not None:
        try:
            return int(float(raw)) % 360
        except (TypeError, ValueError):
            pass
    for side in stream.get("side_data_list", []) or []:
        if isinstance(side, dict) and "rotation" in side:
            try:
                return int(float(side["rotation"])) % 360
            except (TypeError, ValueError):
                continue
    return 0


@dataclass(frozen=True)
class SourceInfo:
    filename: str
    duration_ms: int
    width: int
    height: int
    fps_nominal: Fraction | None
    first_video_pts_ms: int
    has_audio: bool
    codec: str
    rotation: int
    vfr_hint: bool
    color_transfer: str | None
    is_hdr: bool

    def as_source_block(self) -> dict[str, Any]:
        """Projection matching ``contracts/analysis.schema.json#/$defs/source``."""

        return {
            "filename": self.filename,
            "duration_ms": self.duration_ms,
            "width": self.width,
            "height": self.height,
            "fps_nominal": None if self.fps_nominal is None else f"{self.fps_nominal.numerator}/{self.fps_nominal.denominator}",
            "first_video_pts_ms": self.first_video_pts_ms,
            "has_audio": self.has_audio,
            "vfr_detected": None,
            "time_origin": "first_presented_video_frame",
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "filename": self.filename,
            "duration_ms": self.duration_ms,
            "width": self.width,
            "height": self.height,
            "fps_nominal": None
            if self.fps_nominal is None
            else f"{self.fps_nominal.numerator}/{self.fps_nominal.denominator}",
            "first_video_pts_ms": self.first_video_pts_ms,
            "has_audio": self.has_audio,
            "codec": self.codec,
            "rotation": self.rotation,
            "vfr_hint": self.vfr_hint,
            "color_transfer": self.color_transfer,
            "is_hdr": self.is_hdr,
        }


HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}


def probe_source(path: Path) -> SourceInfo:
    data = ffprobe_json(path)
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None:
        raise ProbeError("CODEC_UNSUPPORTED", "no video stream found")

    fmt = data.get("format", {})
    container_start = 0.0
    try:
        container_start = float(fmt.get("start_time") or 0.0)
    except (TypeError, ValueError):
        container_start = 0.0

    duration_s: float | None = None
    for candidate in (fmt.get("duration"), video.get("duration")):
        try:
            duration_s = float(candidate)
            break
        except (TypeError, ValueError):
            continue
    if not duration_s or duration_s <= 0:
        raise ProbeError("CODEC_UNSUPPORTED", "could not determine a positive duration")

    avg = _rate(video.get("avg_frame_rate"))
    nominal = _rate(video.get("r_frame_rate")) or avg
    vfr_hint = bool(avg and nominal and abs(float(avg) - float(nominal)) > 0.01 * float(nominal))

    transfer = video.get("color_transfer")
    return SourceInfo(
        filename=path.name,
        duration_ms=int(round(duration_s * 1000)),
        width=int(video.get("width") or 0),
        height=int(video.get("height") or 0),
        fps_nominal=nominal,
        first_video_pts_ms=_start_time_ms(video, container_start),
        has_audio=audio is not None,
        codec=str(video.get("codec_name") or "unknown"),
        rotation=_rotation(video),
        vfr_hint=vfr_hint,
        color_transfer=str(transfer) if transfer else None,
        is_hdr=str(transfer) in HDR_TRANSFERS if transfer else False,
    )
