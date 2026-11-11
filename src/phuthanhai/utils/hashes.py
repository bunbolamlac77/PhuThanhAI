"""Streaming SHA-256 helpers.

Source videos are large (multi-GB); hashing must never load a file into memory.
Full SHA-256 is the identity used before analysis and before render, per
``docs/03_DATA_CONTRACT.md``; size/mtime/inode are only change *signals*.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import BinaryIO

CHUNK_SIZE = 4 * 1024 * 1024


class HashError(RuntimeError):
    code = "HASH_ERROR"


def sha256_stream(stream: BinaryIO, *, chunk_size: int = CHUNK_SIZE) -> str:
    digest = hashlib.sha256()
    while True:
        block = stream.read(chunk_size)
        if not block:
            break
        digest.update(block)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path, *, chunk_size: int = CHUNK_SIZE) -> str:
    try:
        with path.open("rb") as handle:
            return sha256_stream(handle, chunk_size=chunk_size)
    except OSError as exc:
        raise HashError(f"cannot hash {path.name}: {exc.strerror or exc}") from exc


def file_size(path: Path) -> int:
    return path.stat().st_size
