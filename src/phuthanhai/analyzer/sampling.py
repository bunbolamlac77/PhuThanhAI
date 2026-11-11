"""Deterministic adaptive sampling plans.

The plan is a pure function of (timeline, segments, profile): the same inputs
always produce the same frame ids and timestamps. It never looks at a model and
never claims an action happened; ``reason`` values describe *why a frame was
chosen*, not what is in it.

Coverage is enforced against the profile's target gap with anchors at scene
boundaries, gap filling, and a small, capped refinement around motion peaks.
After extraction the pipeline recomputes the **observed** gap from real decoded
PTS values - the planned gap alone is not evidence.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable

from phuthanhai.analyzer.scene_scan import FrameMetric, SceneSegment
from phuthanhai.analyzer.timeline import Timeline

PROFILE_GAPS_MS: dict[str, int] = {
    "quick": 12_000,
    "detailed": 6_000,
    "thorough": 3_000,
}
MOTION_REFINE_THRESHOLD = 8.0
ANCHOR_EDGE_MS = 150
REFINE_MAX_EXTRA = 2
MIN_TARGET_SEPARATION_MS = 50


class SamplingPlanError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class FrameTarget:
    frame_id: str
    scene_id: str
    requested_ms: int
    reason: str
    kind: str  # anchor | gap | refine

    def to_dict(self) -> dict[str, Any]:
        return {
            "frame_id": self.frame_id,
            "scene_id": self.scene_id,
            "requested_ms": self.requested_ms,
            "reason": self.reason,
            "kind": self.kind,
        }


@dataclass
class SamplingPlan:
    profile: str
    max_gap_ms: int
    targets: list[FrameTarget]
    expected_max_gap_ms: int
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "max_gap_ms": self.max_gap_ms,
            "frame_count": len(self.targets),
            "expected_max_gap_ms": self.expected_max_gap_ms,
            "targets": [target.to_dict() for target in self.targets],
            "notes": self.notes,
        }


def _quality_score(metric: FrameMetric, distance_ms: int, radius_ms: int) -> float:
    blur = max(0.0, metric.blur - 8.0) / 12.0
    dark = max(0.0, 40.0 - metric.yavg) / 40.0
    distance = distance_ms / max(1, radius_ms)
    return blur * 2.0 + dark * 2.0 + distance


def _nearest_acceptable(
    metrics: list[FrameMetric],
    target_ms: int,
    radius_ms: int,
) -> FrameMetric | None:
    window = [m for m in metrics if abs(m.pts_ms - target_ms) <= radius_ms]
    if not window:
        return None
    return min(window, key=lambda m: (_quality_score(m, abs(m.pts_ms - target_ms), radius_ms), m.pts_ms))


def _segment_for(ms: int, segments: list[SceneSegment]) -> str:
    for segment in segments:
        if segment.start_ms <= ms < segment.end_ms:
            return segment.scene_id
    return segments[-1].scene_id


def _insert_gap_fill(targets: list[tuple[int, str, str]], duration_ms: int, max_gap_ms: int) -> None:
    gaps: list[tuple[int, str, str]] = []
    previous = 0
    for time_ms, kind, reason in sorted(targets):
        while time_ms - previous > max_gap_ms:
            previous = previous + max_gap_ms
            gaps.append((previous, "gap", "gap_fill"))
        previous = max(previous, time_ms)
    while duration_ms - previous > max_gap_ms:
        previous = previous + max_gap_ms
        gaps.append((previous, "gap", "gap_fill"))
    targets.extend(gaps)


def build_plan(
    timeline: Timeline,
    segments: list[SceneSegment],
    profile: str,
    *,
    metrics: Iterable[FrameMetric] | None = None,
) -> SamplingPlan:
    if profile not in PROFILE_GAPS_MS:
        raise SamplingPlanError("SAMPLING_PROFILE_INVALID", f"unknown profile: {profile}")
    max_gap = PROFILE_GAPS_MS[profile]
    metrics_list = list(metrics or [])
    duration = timeline.duration_ms
    targets: list[tuple[int, str, str]] = []

    for segment in segments:
        length = segment.end_ms - segment.start_ms
        edge = min(ANCHOR_EDGE_MS, max(1, length // 6))
        anchors: list[tuple[str, int]] = []
        if length <= max(3 * timeline.frame_duration_ms, 120):
            anchors.append(("anchor_mid", (segment.start_ms + segment.end_ms) // 2))
        else:
            anchors = [
                ("anchor_start", segment.start_ms + edge),
                ("anchor_mid", (segment.start_ms + segment.end_ms) // 2),
                ("anchor_end", segment.end_ms - edge),
            ]
        radius = max(timeline.frame_duration_ms, min(2000, length // 4))
        for reason, anchor_ms in anchors:
            chosen = _nearest_acceptable(metrics_list, anchor_ms, radius) if metrics_list else None
            time_ms = chosen.pts_ms if chosen is not None else anchor_ms
            time_ms = min(max(0, time_ms), duration - 1)
            shifted = abs(time_ms - anchor_ms) > timeline.frame_duration_ms
            targets.append((time_ms, "anchor", f"{reason}_shifted" if shifted else reason))

        # Event refinement: a motion cue or a cut justifies 1-2 sequential frames.
        if segment.cut_in or segment.max_motion >= MOTION_REFINE_THRESHOLD:
            peak = segment.motion_peak_ms if segment.motion_peak_ms is not None else (segment.start_ms + segment.end_ms) // 2
            offsets = [0, timeline.frame_duration_ms][: REFINE_MAX_EXTRA + 1]
            added = 0
            for offset in offsets:
                candidate = peak + offset
                if not (segment.start_ms <= candidate < segment.end_ms):
                    continue
                if any(abs(candidate - existing[0]) < MIN_TARGET_SEPARATION_MS for existing in targets):
                    continue
                targets.append((candidate, "refine", "motion_refine"))
                added += 1
                if added >= REFINE_MAX_EXTRA:
                    break

    _insert_gap_fill(targets, duration, max_gap)

    # Deduplicate by time (keep first insertion order: anchors beat gap fill).
    ordered: list[tuple[int, str, str]] = []
    for time_ms, kind, reason in sorted(targets, key=lambda item: (item[0], item[1] != "anchor")):
        if ordered and abs(time_ms - ordered[-1][0]) < MIN_TARGET_SEPARATION_MS:
            continue
        ordered.append((time_ms, kind, reason))

    if not ordered:
        raise SamplingPlanError("SAMPLING_PLAN_EMPTY", "no frames were planned")

    # Deduplication can nudge a gap just above the target; repair deterministically
    # by bisecting the largest gap until the plan satisfies its own budget.
    for _ in range(64):
        points = [0] + [item[0] for item in ordered] + [duration]
        gaps = [(b - a, a, b) for a, b in zip(points, points[1:])]
        largest, start, end = max(gaps)
        if largest <= max_gap:
            break
        candidate = start + (end - start) // 2
        if any(abs(candidate - item[0]) < MIN_TARGET_SEPARATION_MS for item in ordered):
            break
        ordered.append((candidate, "gap", "gap_fill_repair"))
        ordered.sort(key=lambda item: (item[0], item[1] != "anchor"))

    budget = math.ceil(duration / max_gap) + 4 * len(segments) + 20
    if len(ordered) > budget:
        raise SamplingPlanError(
            "SAMPLING_BUDGET_EXCEEDED",
            f"plan needs {len(ordered)} frames but the budget is {budget}; use a lighter profile",
        )

    frames = [
        FrameTarget(
            frame_id=f"F{index:04d}",
            scene_id=_segment_for(time_ms, segments),
            requested_ms=time_ms,
            reason=reason,
            kind=kind,
        )
        for index, (time_ms, kind, reason) in enumerate(ordered, start=1)
    ]
    expected_max = observed_max_gap([frame.requested_ms for frame in frames], duration)
    plan = SamplingPlan(
        profile=profile,
        max_gap_ms=max_gap,
        targets=frames,
        expected_max_gap_ms=expected_max,
        notes=[
            "coverage is enforced from planned timestamps; observed gaps are recomputed from decoded PTS",
            "motion refinement is a technical cue, not an action claim",
        ],
    )
    if expected_max > max_gap:
        plan.notes.append(
            f"WARNING: planned max gap {expected_max} ms exceeds target {max_gap} ms"
        )
    return plan


def observed_max_gap(times_ms: list[int], duration_ms: int) -> int:
    """Max gap between consecutive sampled times, including both boundaries."""

    if not times_ms:
        return duration_ms
    points = sorted({0, duration_ms, *[max(0, min(duration_ms, int(t))) for t in times_ms]})
    return max(b - a for a, b in zip(points, points[1:]))
