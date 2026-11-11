"""Frame extraction through FFmpeg argv.

The sampled image is the *only* evidence the VLM ever sees, so its timestamp and
its orientation must both be correct.

Orientation rule (learned the hard way on real DJI footage): FFmpeg applies the
container display matrix itself (autorotate) before user filters run. Therefore
the scale filter must be expressed in terms of ``iw``/``ih`` - the *displayed*
frame - and never computed from ``ffprobe``'s stored ``width``/``height``. A DJI
clip stored as 3840x2160 with a -90 display matrix is really a 2160x3840 portrait
video; sizing it from the stored dimensions produces a squashed landscape image
that the model then describes as fact.

Seek uses output-side ``-ss`` (after ``-i``) because input-side seek snaps to the
nearest keyframe and would silently report a different PTS than requested.

Scene detection and adaptive sampling are S02; this module only extracts the
single frame that the gate smoke test needs.
"""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from phuthanhai.analyzer.probe import find_tool, probe_source


class FrameExtractionError(RuntimeError):
    code = "FRAME_EXTRACTION_FAILED"


@dataclass(frozen=True)
class ExtractedFrame:
    path: Path
    time_ms: int
    width: int
    height: int
    size_bytes: int
    sha256: str
    source_rotation: int

    def to_dict(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "time_ms": self.time_ms,
            "width": self.width,
            "height": self.height,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "source_rotation": self.source_rotation,
        }


def longest_edge_scale_filter(max_edge: int) -> str:
    """Scale to a longest edge of ``max_edge`` without upscaling, preserving aspect.

    The expression is evaluated on the post-autorotate frame, so it is correct for
    portrait and landscape sources alike. Single quotes are consumed by FFmpeg's
    filter parser (nothing here goes through a shell).
    """

    if max_edge < 16:
        raise FrameExtractionError("max_edge must be at least 16 pixels")
    return (
        f"scale='if(gte(iw,ih),min({max_edge},iw),-2)'"
        f":'if(gte(iw,ih),-2,min({max_edge},ih))'"
    )


def extract_frame(
    source: Path,
    time_ms: int,
    output: Path,
    *,
    max_edge: int = 896,
    quality: int = 4,
    timeout_s: float = 60.0,
) -> ExtractedFrame:
    """Extract one frame at ``time_ms`` of the source timeline.

    ``time_ms`` is measured from the first presented frame, matching the contract
    time origin; the caller is responsible for having probed the source first.
    """

    binary = find_tool("ffmpeg")
    if not binary:
        raise FrameExtractionError("ffmpeg not found on PATH")
    if not source.is_file():
        raise FrameExtractionError(f"source not found: {source.name}")
    if time_ms < 0:
        raise FrameExtractionError("time_ms must be >= 0")

    info = probe_source(source)
    if time_ms >= info.duration_ms:
        raise FrameExtractionError(
            f"time_ms {time_ms} is outside the source duration {info.duration_ms}"
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    seconds = f"{time_ms / 1000.0:.3f}"
    completed = subprocess.run(  # noqa: S603 - argv list, no shell, fixed binary
        [
            binary,
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-i",
            str(source),
            "-ss",
            seconds,
            "-frames:v",
            "1",
            "-vf",
            longest_edge_scale_filter(max_edge),
            "-q:v",
            str(quality),
            str(output),
        ],
        capture_output=True,
        timeout=timeout_s,
        check=False,
    )
    if completed.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
        detail = completed.stderr.decode("utf-8", "replace").strip().splitlines()
        raise FrameExtractionError(detail[-1] if detail else "ffmpeg produced no frame")

    with Image.open(output) as image:
        width, height = image.size
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    return ExtractedFrame(
        path=output,
        time_ms=time_ms,
        width=width,
        height=height,
        size_bytes=output.stat().st_size,
        sha256=digest,
        source_rotation=info.rotation,
    )
