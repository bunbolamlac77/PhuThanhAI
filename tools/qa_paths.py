"""Validation for operator-supplied footage paths in QA tools.

An empty or whitespace environment variable must never become the current
directory, the repository root, the home folder or Downloads.
"""

from __future__ import annotations

import os
from pathlib import Path


class QAPathError(ValueError):
    pass


def require_source_root(value: str | None, *, label: str = "PHUTHANHAI_TEST_FOOTAGE") -> Path:
    if value is None or not str(value).strip():
        raise QAPathError(f"{label} is empty; refusing to scan any directory")
    path = Path(str(value).strip()).expanduser()
    if not path.exists():
        raise QAPathError(f"{label} does not exist: {path}")
    if not path.is_dir():
        raise QAPathError(f"{label} is not a directory: {path}")
    return path.resolve()


def source_root_from_env() -> Path:
    return require_source_root(os.environ.get("PHUTHANHAI_TEST_FOOTAGE"))
