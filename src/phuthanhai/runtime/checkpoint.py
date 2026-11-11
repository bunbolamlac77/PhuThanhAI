"""Crash-safe checkpoints and append-only job events.

A checkpoint is only reusable when *every* fingerprint that affects the result
matches: source hash, sampling/extraction config, model snapshot, prompt version.
Reusing a checkpoint across a changed source or model would mix two runs, which
the data contract forbids.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from phuthanhai.utils.fs import atomic_write_json, read_json_strict

CHECKPOINT_SCHEMA_VERSION = "1.0.0"


class CheckpointError(RuntimeError):
    code = "CHECKPOINT_INVALID"


class CheckpointMismatch(CheckpointError):
    code = "CHECKPOINT_FINGERPRINT_MISMATCH"


@dataclass
class Checkpoint:
    job_id: str
    stage: str
    fingerprint: dict[str, Any]
    committed_frame_ids: list[str] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)
    schema_version: str = CHECKPOINT_SCHEMA_VERSION
    created_at: str = field(
        default_factory=lambda: datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "job_id": self.job_id,
            "stage": self.stage,
            "fingerprint": self.fingerprint,
            "committed_frame_ids": self.committed_frame_ids,
            "payload": self.payload,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Checkpoint":
        required = {"schema_version", "job_id", "stage", "fingerprint", "committed_frame_ids", "payload"}
        missing = required - set(data)
        if missing:
            raise CheckpointError(f"checkpoint missing keys: {sorted(missing)}")
        if data["schema_version"] != CHECKPOINT_SCHEMA_VERSION:
            raise CheckpointError(
                f"unsupported checkpoint schema {data['schema_version']!r}; expected {CHECKPOINT_SCHEMA_VERSION}"
            )
        if not isinstance(data["fingerprint"], dict):
            raise CheckpointError("checkpoint fingerprint must be an object")
        return cls(
            job_id=str(data["job_id"]),
            stage=str(data["stage"]),
            fingerprint=dict(data["fingerprint"]),
            committed_frame_ids=list(data["committed_frame_ids"]),
            payload=dict(data["payload"]),
            created_at=str(data.get("created_at", "")),
        )


def fingerprint_differences(stored: dict[str, Any], expected: dict[str, Any]) -> list[str]:
    """Keys that differ or are missing on either side."""

    diffs: list[str] = []
    for key in sorted(set(stored) | set(expected)):
        if stored.get(key) != expected.get(key):
            diffs.append(key)
    return diffs


def write_checkpoint(path: Path, checkpoint: Checkpoint) -> None:
    atomic_write_json(path, checkpoint.to_dict())


def read_checkpoint(path: Path, *, expected_fingerprint: dict[str, Any] | None = None) -> Checkpoint:
    data = read_json_strict(path)
    checkpoint = Checkpoint.from_dict(data)
    if expected_fingerprint is not None:
        diffs = fingerprint_differences(checkpoint.fingerprint, expected_fingerprint)
        if diffs:
            raise CheckpointMismatch(
                "checkpoint fingerprints differ on: " + ", ".join(diffs)
            )
    return checkpoint


def append_events(path: Path, events: Iterable[dict[str, Any]], *, dedup_seen: set[str] | None = None) -> int:
    """Append JSONL events atomically-per-line; returns how many were written.

    ``dedup_seen`` is an in-memory set of keys already committed in this run, so a
    resumed job cannot append the same stage twice.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with path.open("a", encoding="utf-8") as handle:
        for event in events:
            key = event.get("dedup_key")
            if dedup_seen is not None and key is not None:
                if key in dedup_seen:
                    continue
                dedup_seen.add(key)
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
            written += 1
        handle.flush()
        import os

        os.fsync(handle.fileno())
    return written
