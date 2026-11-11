"""S05.4 smart visual-evidence planning and per-frame inference cache.

The planner never invents frames or timestamps: it only selects from the verified
S02 sampling manifest. Every decision carries a reason for QA. The cache stores one
validated observation per frame and is keyed independently of batch size/order.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from PIL import Image

from phuthanhai.analyzer.vlm_worker import WorkerError, validate_observation
from phuthanhai.utils.fs import atomic_write_json

SELECTION_MODES = ("smart", "balanced", "full")
BUDGETS = {"smart": 6, "balanced": 12, "full": None}


def budget_for(mode: str) -> int | None:
    """Optional budget override for QA/benchmarks, without changing defaults."""
    env = {"smart": "PHUTHANHAI_SMART_FRAME_BUDGET", "balanced": "PHUTHANHAI_BALANCED_FRAME_BUDGET"}.get(mode)
    if env and os.environ.get(env):
        try:
            return max(1, int(os.environ[env]))
        except ValueError:
            return BUDGETS[mode]
    return BUDGETS[mode]
CACHE_VERSION = "s05.4-frame-cache-1"
PHASH_DISTANCE = 4
PHASH_MIN_GAP_MS = 1000


@dataclass(frozen=True)
class Selected:
    frame: dict[str, Any]
    reason: str
    score: float
    priority: bool = False


def _signature(path: str | Path) -> tuple[int, int] | None:
    """aHash + mean luma; the pair avoids collapsing flat low-detail frames."""
    try:
        with Image.open(path) as image:
            small = image.convert("L").resize((8, 8), Image.Resampling.LANCZOS)
            pixels = list(small.get_flattened_data() if hasattr(small, "get_flattened_data") else small.getdata())
        mean = sum(pixels) / len(pixels)
        value = 0
        for pixel in pixels:
            value = (value << 1) | (1 if pixel >= mean else 0)
        return value, round(mean)
    except Exception:  # noqa: BLE001 - unreadable image is not a selection obstacle
        return None


def _near_duplicate(a: tuple[int, int] | None, b: tuple[int, int] | None) -> bool:
    if a is None or b is None:
        return False
    return _distance(a[0], b[0]) <= PHASH_DISTANCE and abs(a[1] - b[1]) <= 8


def _distance(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def _in_ranges(ms: int, ranges: Iterable[list[int]] | None) -> bool:
    if not ranges:
        return False
    return any(int(start) <= ms < int(end) for start, end in ranges if int(end) > int(start))


def _segment_quality(segment: dict[str, Any]) -> float:
    score = 0.0
    if float(segment.get("blur_ratio") or 0) > 0.6:
        score -= 2.0
    if float(segment.get("dark_ratio") or 0) > 0.6:
        score -= 1.5
    motion = float(segment.get("max_motion") or 0)
    if motion >= 8:
        score += min(2.0, motion / 20.0)
    return score


def _frame_score(frame: dict[str, Any], segment: dict[str, Any], ranges: list[list[int]] | None) -> tuple[float, bool, str]:
    pts = int(frame["normalized_pts_ms"])
    kind = frame.get("kind") or ""
    score = _segment_quality(segment)
    reason = "fill"
    if kind in {"anchor_start", "anchor_end"}:
        score += 2.0
        reason = "scene_boundary"
    elif kind == "anchor_mid":
        score += 1.0
        reason = "scene_anchor"
    elif kind == "refine":
        score += 1.1
        reason = "motion_peak"
    elif kind == "gap":
        score += 0.4
    if _in_ranges(pts, ranges):
        score += 3.0
        reason = "user_range"
        return score, True, reason
    return score, False, reason


def build_selection(
    manifest: dict[str, Any],
    mode: str = "full",
    *,
    user_ranges: list[list[int]] | None = None,
) -> dict[str, Any]:
    """Select verified frames under a mode's budget, with explicit reasons."""

    if mode not in SELECTION_MODES:
        raise ValueError(f"unknown selection mode: {mode!r}")
    frames = list(manifest.get("frames") or [])
    segments = {segment["scene_id"]: segment for segment in manifest.get("segments", [])}
    sampling_total = len(frames)
    if mode == "full":
        selected = [
            Selected(frame=frame, reason="full_evidence", score=0.0, priority=False)
            for frame in frames
        ]
        return _result(mode, manifest, sampling_total, selected, [], requested_budget=None)

    requested_budget = budget_for(mode)
    budget = requested_budget if requested_budget is not None else sampling_total
    if budget <= 0:
        return _result(mode, manifest, sampling_total, [], [
            {
                "frame_id": frame["frame_id"], "scene_id": frame["scene_id"],
                "normalized_pts_ms": frame["normalized_pts_ms"], "reason": "no_budget",
            }
            for frame in frames
        ], requested_budget=requested_budget)
    candidates: list[tuple[float, Selected]] = []
    for frame in frames:
        segment = segments.get(frame["scene_id"], {})
        score, priority, reason = _frame_score(frame, segment, user_ranges)
        candidates.append((score, Selected(frame=frame, reason=reason, score=score, priority=priority)))
    candidates.sort(key=lambda item: (-item[0], int(item[1].frame["normalized_pts_ms"])))

    selected: list[Selected] = []
    used_scenes: set[str] = set()
    kept_signatures: dict[str, list[tuple[tuple[int, int], int]]] = {}

    # Phase 1: one best frame per scene, but never beyond the hard budget.
    # When scene_count > budget, the highest-scoring scenes win.
    for _, choice in candidates:
        if len(selected) >= budget:
            break
        scene = choice.frame["scene_id"]
        if scene in used_scenes:
            continue
        selected.append(Selected(
            frame=choice.frame, reason="scene_anchor" if choice.reason == "fill" else choice.reason,
            score=choice.score, priority=choice.priority,
        ))
        used_scenes.add(scene)
        signature = _signature(choice.frame["path"]) if choice.frame.get("path") else None
        if signature is not None:
            kept_signatures.setdefault(scene, []).append((signature, int(choice.frame["normalized_pts_ms"])))

    # Phase 2: fill remaining budget by score, avoiding near-duplicates.
    for _, choice in candidates:
        if len(selected) >= budget:
            break
        frame = choice.frame
        if any(item.frame["frame_id"] == frame["frame_id"] for item in selected):
            continue
        signature = _signature(frame["path"]) if frame.get("path") else None
        previous = kept_signatures.get(frame["scene_id"], [])
        if not choice.priority and any(
            _near_duplicate(signature, item[0])
            and abs(int(frame["normalized_pts_ms"]) - item[1]) < PHASH_MIN_GAP_MS
            for item in previous
        ):
            continue
        selected.append(Selected(frame=frame, reason=choice.reason, score=choice.score, priority=choice.priority))
        if signature is not None:
            kept_signatures.setdefault(frame["scene_id"], []).append((signature, int(frame["normalized_pts_ms"])))
    selected.sort(key=lambda item: int(item.frame["normalized_pts_ms"]))
    selected_ids = {item.frame["frame_id"] for item in selected}
    dropped = [
        {
            "frame_id": frame["frame_id"],
            "scene_id": frame["scene_id"],
            "normalized_pts_ms": frame["normalized_pts_ms"],
            "reason": "duplicate_or_budget",
        }
        for frame in frames
        if frame["frame_id"] not in selected_ids
    ]
    return _result(mode, manifest, sampling_total, selected, dropped,
                   requested_budget=requested_budget)


def _result(mode: str, manifest: dict[str, Any], sampling_total: int,
            selected: list[Selected], dropped: list[dict[str, Any]],
            *, requested_budget: int | None = None) -> dict[str, Any]:
    scene_count = len(manifest.get("segments") or [])
    covered = sorted({item.frame["scene_id"] for item in selected})
    missing = sorted(set(seg["scene_id"] for seg in manifest.get("segments") or []) - set(covered))
    return {
        "mode": mode,
        "requested_budget": requested_budget,
        "budget": len(selected) if requested_budget is not None and len(selected) > requested_budget else (requested_budget if requested_budget is not None else sampling_total),
        "hard_budget": requested_budget is not None,
        "sampling_frames": sampling_total,
        "selected_frames": len(selected),
        "fraction": round(len(selected) / sampling_total, 3) if sampling_total else 0.0,
        "partial": len(selected) < sampling_total,
        "frame_ids": [item.frame["frame_id"] for item in selected],
        "selected": [
            {
                "frame_id": item.frame["frame_id"],
                "scene_id": item.frame["scene_id"],
                "normalized_pts_ms": item.frame["normalized_pts_ms"],
                "reason": item.reason,
                "score": round(item.score, 3),
            }
            for item in selected
        ],
        "dropped": dropped,
        "scenes_total": scene_count,
        "scenes_covered": covered,
        "scenes_missing": missing,
        "visual_coverage": {
            "selected": len(selected),
            "sampling": sampling_total,
            "fraction": round(len(selected) / sampling_total, 3) if sampling_total else 0.0,
            "partial": len(selected) < sampling_total,
            "mode": mode,
            "scenes_missing": missing,
        },
    }


class FrameResultCache:
    """Validated per-frame observations independent of batch size or frame order."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    @staticmethod
    def key(*, source_sha: str, frame_sha: str, model: dict[str, Any], runtime: dict[str, Any],
            prompt_version: str, max_tokens: int, temperature: float) -> str:
        payload = {
            "cache_version": CACHE_VERSION,
            "source_sha": source_sha,
            "frame_sha": frame_sha,
            "model": model,
            "runtime": runtime,
            "prompt_version": prompt_version,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def _path(self, key: str) -> Path:
        return self.root / key[:2] / f"{key}.json"

    def get(self, key: str) -> dict[str, Any] | None:
        path = self._path(key)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("cache_version") != CACHE_VERSION or data.get("key") != key:
                return None
            observation = validate_observation(data.get("observation"))
            return {"observation": observation, "method": data.get("method", "sequential_frames")}
        except (OSError, ValueError, WorkerError):
            path.unlink(missing_ok=True)
            return None

    def put(self, key: str, observation: dict[str, Any], *, method: str) -> None:
        validate_observation(observation)
        atomic_write_json(self._path(key), {
            "cache_version": CACHE_VERSION,
            "key": key,
            "observation": observation,
            "method": method,
        })
