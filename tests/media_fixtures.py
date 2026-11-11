"""Deterministic media fixtures for S02 tests (generated once per test process).

Every fixture is produced locally with FFmpeg; no binary media is committed.
"""

from __future__ import annotations

import atexit
import shutil
import subprocess
import tempfile
from pathlib import Path

FFMPEG = "ffmpeg"
_DIR = Path(tempfile.mkdtemp(prefix="pth_s02_fixtures_"))
atexit.register(shutil.rmtree, _DIR, ignore_errors=True)
_CACHE: dict[str, Path] = {}


def _run(argv: list[str], *, timeout: float = 300) -> None:
    completed = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"fixture ffmpeg failed: {completed.stderr[-500:]}")


def _base(path: Path, *, seconds: float = 3.0, size: str = "320x240", rate: int = 25, extra: list[str] | None = None) -> Path:
    argv = [
        FFMPEG, "-nostdin", "-v", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc=size={size}:rate={rate}:duration={seconds}",
        "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
    ]
    argv += extra or []
    argv.append(str(path))
    _run(argv)
    return path


def _concat_colors(path: Path, colors: list[tuple[str, float]], *, size: str = "320x240", rate: int = 25) -> Path:
    inputs: list[str] = []
    for color, duration in colors:
        inputs += ["-f", "lavfi", "-i", f"color=c={color}:size={size}:rate={rate}:duration={duration}"]
    filters = "".join(f"[{index}:v]" for index in range(len(colors)))
    filtergraph = f"{filters}concat=n={len(colors)}:v=1:a=0[v]"
    _run(
        [
            FFMPEG, "-nostdin", "-v", "error", "-y", *inputs,
            "-filter_complex", filtergraph, "-map", "[v]",
            "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast", str(path),
        ]
    )
    return path


def build(name: str) -> Path:
    if name in _CACHE:
        return _CACHE[name]
    path = _DIR / f"{name}.mp4"
    if name == "cfr":
        _base(path, seconds=3)
    elif name == "vfr":
        first = _DIR / "vfr_a.mp4"
        second = _DIR / "vfr_b.mp4"
        _base(first, seconds=2, rate=30)
        _base(second, seconds=2, rate=10)
        _run(
            [
                FFMPEG, "-nostdin", "-v", "error", "-y", "-i", str(first), "-i", str(second),
                "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]", "-map", "[v]",
                "-c:v", "libx264", "-preset", "ultrafast", "-fps_mode", "vfr", str(path),
            ]
        )
    elif name == "nonzero":
        temp = _DIR / "nonzero_src.mp4"
        _base(temp, seconds=3)
        _run(
            [
                FFMPEG, "-nostdin", "-v", "error", "-y", "-i", str(temp),
                "-vf", "setpts=PTS+2/TB", "-c:v", "libx264", "-preset", "ultrafast", str(path),
            ]
        )
    elif name == "bframes":
        _base(path, seconds=2, extra=["-bf", "3", "-g", "30"])
    elif name == "rotation":
        temp = _DIR / "rotation_src.mp4"
        _base(temp, seconds=2)
        _run(
            [
                FFMPEG, "-nostdin", "-v", "error", "-y",
                "-display_rotation", "90", "-i", str(temp), "-c", "copy", str(path),
            ]
        )
    elif name == "hdr":
        temp = _DIR / "hdr_src.mp4"
        _base(temp, seconds=2)
        _run(
            [
                FFMPEG, "-nostdin", "-v", "error", "-y", "-i", str(temp),
                "-c:v", "libx264", "-preset", "ultrafast",
                "-x264-params", "colorprim=bt2020:transfer=arib-std-b67:colormatrix=bt2020nc",
                "-color_primaries", "bt2020", "-color_trc", "arib-std-b67",
                "-colorspace", "bt2020nc", str(path),
            ]
        )
    elif name == "cuts":
        _concat_colors(path, [("red", 1.0), ("green", 1.0), ("blue", 1.0), ("white", 1.0)])
    elif name == "rapid":
        _concat_colors(
            path,
            [("red", 0.25), ("green", 0.25), ("blue", 0.25), ("white", 0.25), ("yellow", 0.25), ("black", 0.25)],
        )
    elif name == "long_nocut":
        _base(path, seconds=40, size="160x120", rate=15)
    elif name == "dark":
        temp = _DIR / "dark_src.mp4"
        _base(temp, seconds=3)
        _run(
            [
                FFMPEG, "-nostdin", "-v", "error", "-y", "-i", str(temp),
                "-vf", "eq=brightness=-0.85", "-c:v", "libx264", "-preset", "ultrafast", str(path),
            ]
        )
    elif name == "blurred":
        temp = _DIR / "blur_src.mp4"
        _base(temp, seconds=3)
        _run(
            [
                FFMPEG, "-nostdin", "-v", "error", "-y", "-i", str(temp),
                "-vf", "boxblur=10", "-c:v", "libx264", "-preset", "ultrafast", str(path),
            ]
        )
    elif name == "motion_short":
        # A one-second moving object inside otherwise static footage: a short
        # motion event, not a scene cut.
        _run(
            [
                FFMPEG, "-nostdin", "-v", "error", "-y",
                "-f", "lavfi", "-i", "color=c=black:size=320x240:rate=25:duration=4",
                "-f", "lavfi", "-i", "color=c=white:size=200x150:rate=25:duration=4",
                "-filter_complex",
                "[0:v][1:v]overlay=x='t*160':y=60:enable='lt(t,1)':shortest=1",
                "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast", str(path),
            ]
        )
    elif name == "prores":
        path = _DIR / "prores.mov"
        temp = _DIR / "prores_src.mp4"
        _base(temp, seconds=1)
        _run(
            [
                FFMPEG, "-nostdin", "-v", "error", "-y", "-i", str(temp),
                "-c:v", "prores_ks", "-profile:v", "3", "-pix_fmt", "yuv422p10le", str(path),
            ]
        )
    elif name == "corrupt":
        source = _DIR / "corrupt_src.mp4"
        _base(source, seconds=3)
        data = source.read_bytes()
        path.write_bytes(data[: int(len(data) * 0.4)])
    else:
        raise KeyError(f"unknown fixture: {name}")
    _CACHE[name] = path
    return path


def get(name: str) -> Path:
    return build(name)


def fixture_dir() -> Path:
    return _DIR
