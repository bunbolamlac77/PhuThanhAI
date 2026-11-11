"""Shared test helpers.

Tests are written as ``unittest.TestCase`` so they run with the Python standard
library (``python -m unittest``) as well as with pytest, and require no extra
runner dependency to prove the gate.
"""

from __future__ import annotations

import json
import os
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from phuthanhai.config import Settings


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def fixtures_dir() -> Path:
    return repo_root() / "tests" / "fixtures"


def contracts_dir() -> Path:
    return repo_root() / "contracts"


def load_fixture(name: str) -> dict:
    return json.loads((fixtures_dir() / name).read_text(encoding="utf-8"))


@contextmanager
def isolated_settings(tmpdir: Path) -> Iterator[Settings]:
    """Point the data root at a temporary directory for the duration of a test."""

    previous = os.environ.get("PHUTHANHAI_DATA_DIR")
    os.environ["PHUTHANHAI_DATA_DIR"] = str(tmpdir)
    try:
        yield Settings.load()
    finally:
        if previous is None:
            os.environ.pop("PHUTHANHAI_DATA_DIR", None)
        else:
            os.environ["PHUTHANHAI_DATA_DIR"] = previous


def make_test_video(
    path: Path,
    *,
    seconds: float = 1.0,
    size: str = "320x240",
    portrait: bool = False,
) -> Path:
    """Create a real H.264 video with ffmpeg (lavfi source) for media tests.

    ``portrait=True`` rotates the picture 90 degrees, producing a genuinely
    portrait frame (240x320 for a 320x240 source) so orientation handling can be
    verified without depending on a private media file.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    argv = [
        "ffmpeg",
        "-nostdin",
        "-v",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"testsrc=size={size}:rate=25:duration={seconds}",
    ]
    if portrait:
        argv += ["-vf", "transpose=1"]
    argv += [
        "-pix_fmt",
        "yuv420p",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        str(path),
    ]
    completed = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if completed.returncode != 0 or not path.is_file():
        raise RuntimeError(f"ffmpeg could not create test video: {completed.stderr[-400:]}")
    return path
