"""Filesystem helpers with atomic, crash-safe semantics.

Rationale: contract-bearing JSON (checkpoints, portable manifests, notes) must
never be observed half-written. Every write goes through a sibling ``.tmp`` file,
is fsynced, re-read, then ``os.replace``d into place, and the directory entry is
fsynced as well so the rename survives a power loss.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


class FsError(RuntimeError):
    """Filesystem operation failed; carries a stable ``code`` for the API/CLI."""

    code = "FS_ERROR"

    def __init__(self, message: str, *, path: Path | str | None = None) -> None:
        super().__init__(message)
        self.path = str(path) if path is not None else None


def _reject_constant(value: str) -> Any:
    raise ValueError(f"non-finite JSON constant is not allowed: {value}")


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key is not allowed: {key}")
        result[key] = value
    return result


def strict_json_loads(text: str) -> dict[str, Any]:
    """Decode JSON rejecting duplicate keys, NaN and Infinity.

    Contract files are untrusted input (they may arrive from a portable Markdown
    report). ``json.loads`` silently accepts duplicate keys and non-finite
    numbers, both of which would let two readers disagree about the same bytes.
    """

    result = json.loads(text, object_pairs_hook=_reject_duplicates, parse_constant=_reject_constant)
    if not isinstance(result, dict):
        raise ValueError("expected a JSON object at the top level")
    return result


def read_json_strict(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise FsError(f"file not found: {path.name}", path=path) from exc
    except UnicodeDecodeError as exc:
        raise FsError(f"file is not valid UTF-8: {path.name}", path=path) from exc
    try:
        return strict_json_loads(raw)
    except ValueError as exc:
        raise FsError(f"invalid JSON in {path.name}: {exc}", path=path) from exc


def ensure_dir(path: Path, *, mode: int = 0o700) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=mode)
    return path


def _fsync_dir(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_bytes(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    """Write ``data`` to ``path`` atomically, verifying the result before rename."""

    ensure_dir(path.parent)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        # Verify what actually landed on disk before publishing it.
        with open(tmp_path, "rb") as handle:
            if handle.read() != data:
                raise FsError(f"atomic write verification failed for {path.name}", path=path)
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
        _fsync_dir(path.parent)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def atomic_write_text(path: Path, text: str, *, mode: int = 0o600) -> None:
    atomic_write_bytes(path, text.encode("utf-8"), mode=mode)


def atomic_write_json(path: Path, payload: Any, *, mode: int = 0o600) -> None:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    atomic_write_text(path, text, mode=mode)


def is_inside(child: Path, parent: Path) -> bool:
    """True when ``child`` resolves inside ``parent`` (symlink-aware, no traversal)."""

    try:
        child_r = child.resolve(strict=False)
        parent_r = parent.resolve(strict=False)
    except OSError:
        return False
    return child_r == parent_r or parent_r in child_r.parents
