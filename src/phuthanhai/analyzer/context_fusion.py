"""ContextOverlay: profile-driven suggestions on top of immutable raw evidence.

S03 raw evidence is never modified. The overlay is a deterministic, versioned
projection of:

* raw visual evidence (frame ids, PTS and observations),
* the selected environment profile,
* user-confirmed / corrected / dismissed labels (stored as annotations).

Switching a profile recomputes only this overlay and reports; it never touches
frame caches or runs inference. A candidate is a **hypothesis**, never an
``observed`` fact, and user assertions are marked with their annotation id.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

from phuthanhai.analyzer import context_profiles as profiles

OVERLAY_VERSION = "1.0.0"
RITUAL_TERMS = {
    "incense",
    "figurine",
    "offerings",
    "offering",
    "worship",
    "ancestor",
    "statue",
    "candle",
    "fruit tray",
    "bàn thờ",
    "gia tiên",
    "thờ",
    "hương",
    "lễ",
}
IDENTITY_KEYS = {"bride_groom_prep", "offering_procession", "bride_procession", "family_photos"}


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def _norm(value: str) -> str:
    return value.casefold().strip()


def _frame_text(frame: dict[str, Any]) -> str:
    observation = frame.get("observation") or {}
    parts = [
        observation.get("description") or "",
        " ".join(observation.get("objects") or []),
        " ".join(observation.get("actions") or []),
        " ".join(observation.get("visible_text") or []),
        observation.get("camera") or "",
        observation.get("lighting") or "",
    ]
    return _norm(" | ".join(parts))


def raw_evidence_digest(evidence: dict[str, Any]) -> str:
    frames = [
        {
            "frame_id": frame["frame_id"],
            "scene_id": frame["scene_id"],
            "normalized_pts_ms": frame["normalized_pts_ms"],
            "interpretation_level": frame["interpretation_level"],
            "observation": frame.get("observation"),
        }
        for frame in evidence.get("frames", [])
    ]
    payload = json.dumps(frames, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


_NEGATIONS = ("no", "not", "without", "không", "chưa", "thiếu")


def _canonical(term: str) -> str:
    value = _norm(term).replace("-like", "").strip()
    if value.endswith("s") and not value.endswith("ss"):
        value = value[:-1]
    return value


def _occurs_unnegated(text: str, term: str) -> bool:
    """Phrase/boundary-aware match that ignores negated occurrences."""

    needle = _norm(term)
    if not needle:
        return False
    pattern = re.escape(needle)
    if needle.isascii():
        pattern = rf"(?<![a-z0-9]){pattern}(?:s|es)?(?![a-z0-9])"
    for match in re.finditer(pattern, text):
        prefix = text[max(0, match.start() - 32) : match.start()].strip()
        clause = re.split(r"[.;:,()]", prefix)[-1].strip()
        words = clause.split()
        if any(word in _NEGATIONS for word in words[-3:]):
            continue
        return True
    return False


def _hits(spec: profiles.LabelSpec, text: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for term in (*spec.signals, *spec.required_any):
        canonical = _canonical(term)
        if canonical in seen:
            continue
        if _occurs_unnegated(text, term):
            seen.add(canonical)
            found.append(term)
    return found


def _gate_present(spec: profiles.LabelSpec, hits: list[str]) -> bool:
    if not spec.required_any:
        return True
    return any(_norm(term) in {_norm(hit) for hit in hits} for term in spec.required_any)


def _ritual_supported(hits: list[str]) -> bool:
    lowered = {_norm(hit) for hit in hits}
    return any(_norm(term) in lowered for term in RITUAL_TERMS)


def _candidate(
    spec: profiles.LabelSpec,
    frame_hits: list[tuple[dict[str, Any], list[str]]],
) -> dict[str, Any]:
    frames = [frame for frame, _ in frame_hits]
    all_hits = sorted({hit for _, hits in frame_hits for hit in hits})
    return {
        "label_key": spec.key,
        "label_vi": spec.label_vi,
        "category": spec.category,
        "status": "candidate",
        "interpretation_level": "hypothesis",
        "provenance": "context_rules",
        "description_vi": spec.description_vi,
        "uncertainty_vi": spec.uncertainty_vi,
        "signals": all_hits,
        "evidence_frames": [frame["frame_id"] for frame in frames],
        "evidence_scenes": sorted({frame["scene_id"] for frame in frames}),
        "evidence_times_ms": [frame["normalized_pts_ms"] for frame in frames],
        "identity_claim": False,
        "identity_note_vi": (
            "Không xác định danh tính/quan hệ người chỉ từ hình ảnh."
            if spec.identity_label
            else None
        ),
    }


def _label_frame_refs(label: dict[str, Any], scene_frames: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    if label.get("frame_id"):
        return [
            frame
            for frames in scene_frames.values()
            for frame in frames
            if frame["frame_id"] == label["frame_id"]
        ]
    if label.get("scene_id"):
        return list(scene_frames.get(label["scene_id"], []))
    return [frame for frames in scene_frames.values() for frame in frames]


def build_overlay(
    *,
    asset_id: str,
    asset_duration_ms: int,
    evidence: dict[str, Any],
    profile_id: str,
    user_labels: list[dict[str, Any]] | None = None,
    scene_windows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    spec = profiles.profile(profile_id)
    raw_frames_all = [
        frame
        for frame in evidence.get("frames", [])
        if frame.get("interpretation_level") == "observed"
    ]
    windows: dict[str, tuple[int, int]] = {}
    boundary_errors: list[str] = []
    if scene_windows:
        for segment in scene_windows:
            scene_id = segment.get("scene_id")
            start = segment.get("start_ms")
            end = segment.get("end_ms")
            if not isinstance(scene_id, str) or not isinstance(start, int) or not isinstance(end, int):
                boundary_errors.append(f"invalid scene window: {segment!r}")
                continue
            if not (0 <= start < end <= int(asset_duration_ms)):
                boundary_errors.append(f"{scene_id} span {start}-{end} outside duration {asset_duration_ms}")
                continue
            windows[scene_id] = (start, end)
    scene_timecode_source = "manifest" if windows else "frame_derived"
    raw_frames = [
        frame
        for frame in raw_frames_all
        if not windows or (
            frame["scene_id"] in windows
            and windows[frame["scene_id"]][0] <= int(frame["normalized_pts_ms"]) < windows[frame["scene_id"]][1]
        )
    ]
    for frame in raw_frames_all:
        if windows and frame not in raw_frames:
            boundary_errors.append(
                f"frame {frame['frame_id']} PTS {frame['normalized_pts_ms']} outside scene {frame['scene_id']} window"
            )
    scene_frames: dict[str, list[dict[str, Any]]] = {}
    for frame in sorted(raw_frames, key=lambda item: int(item["normalized_pts_ms"])):
        scene_frames.setdefault(frame["scene_id"], []).append(frame)

    candidates: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for label_spec in spec.labels:
        matched: list[tuple[dict[str, Any], list[str]]] = []
        weak: list[tuple[dict[str, Any], list[str]]] = []
        for frame in raw_frames:
            hits = _hits(label_spec, _frame_text(frame))
            if not _gate_present(label_spec, hits):
                continue
            if len(hits) >= label_spec.min_hits:
                matched.append((frame, hits))
            elif label_spec.key == "ancestor_altar":
                weak.append((frame, hits))
        if not matched:
            if weak:
                unresolved.append(
                    {
                        "label_key": label_spec.key,
                        "label_vi": label_spec.label_vi,
                        "status": "insufficient_evidence",
                        "reason_vi": "Chỉ thấy dấu hiệu bàn thờ/shrine; thiếu hương, lễ vật hoặc bối cảnh thờ tự.",
                        "evidence_frames": [frame["frame_id"] for frame, _ in weak],
                        "evidence_scenes": sorted({frame["scene_id"] for frame, _ in weak}),
                    }
                )
            continue
        all_hits = {_norm(hit) for _, hits in matched for hit in hits}
        if label_spec.key == "ancestor_altar" and not _ritual_supported(all_hits):
            # A "shrine-like" table on decoration alone must not become an altar.
            unresolved.append(
                {
                    "label_key": label_spec.key,
                    "label_vi": label_spec.label_vi,
                    "status": "insufficient_evidence",
                    "reason_vi": "Có dấu hiệu bàn thờ/shrine nhưng thiếu hương, lễ vật hoặc bối cảnh thờ tự.",
                    "evidence_frames": [frame["frame_id"] for frame, _ in matched],
                    "evidence_scenes": sorted({frame["scene_id"] for frame, _ in matched}),
                }
            )
            continue
        candidates.append(_candidate(label_spec, matched))

    # Remove candidates that the user dismissed; replace matching ones with the
    # user's assertion while retaining the annotation reference.
    labels = [label for label in (user_labels or []) if label.get("label_key")]
    dismissed_entries: list[dict[str, Any]] = []
    for label in labels:
        if (label.get("status") or label.get("label_status")) != "dismissed":
            continue
        scene_id = label.get("scene_id") or label.get("label_scene_id")
        frame_id = label.get("frame_id") or label.get("label_frame_id")
        scope = "scene" if scene_id else ("frame" if frame_id else "clip")
        dismissed_entries.append(
            {
                "label_key": label["label_key"],
                "scope": scope,
                "scene_id": scene_id,
                "frame_id": frame_id,
                "annotation_id": label.get("annotation_id"),
            }
        )

    def _dismissed_frame(candidate_key: str, frame_id: str, scene_id: str) -> bool:
        for entry in dismissed_entries:
            if entry["label_key"] != candidate_key:
                continue
            if entry["scope"] == "clip":
                return True
            if entry["scope"] == "scene" and entry["scene_id"] == scene_id:
                return True
            if entry["scope"] == "frame" and entry["frame_id"] == frame_id:
                return True
        return False

    scoped_candidates: list[dict[str, Any]] = []
    for candidate in candidates:
        kept_frames = []
        for frame_id, scene_id, pts in zip(
            candidate["evidence_frames"], candidate["evidence_scenes"], candidate["evidence_times_ms"]
        ):
            if not _dismissed_frame(candidate["label_key"], frame_id, scene_id):
                kept_frames.append((frame_id, scene_id, pts))
        if not kept_frames:
            continue
        candidate = dict(candidate)
        candidate["evidence_frames"] = [item[0] for item in kept_frames]
        candidate["evidence_scenes"] = sorted({item[1] for item in kept_frames})
        candidate["evidence_times_ms"] = [item[2] for item in kept_frames]
        scoped_candidates.append(candidate)
    candidates = scoped_candidates
    dismissed_label_keys = sorted({entry["label_key"] for entry in dismissed_entries})
    by_key = {item["label_key"]: item for item in candidates}
    user_entries: list[dict[str, Any]] = []
    for label in labels:
        status = label.get("status") or label.get("label_status") or "confirmed"
        if status == "dismissed":
            continue
        label_vi = label.get("label_vi") or label.get("text") or label["label_key"]
        refs = _label_frame_refs(label, scene_frames)
        user_entries.append(
            {
                "label_key": label["label_key"],
                "label_vi": label_vi,
                "status": "user_confirmed" if status == "confirmed" else "user_corrected",
                "interpretation_level": "user_asserted",
                "provenance": "user_annotation",
                "annotation_id": label.get("annotation_id"),
                "revision": label.get("revision", 1),
                "scene_id": label.get("scene_id") or label.get("label_scene_id"),
                "frame_id": label.get("frame_id") or label.get("label_frame_id"),
                "scope": (
                    "scene" if (label.get("scene_id") or label.get("label_scene_id"))
                    else ("frame" if (label.get("frame_id") or label.get("label_frame_id")) else "clip")
                ),
                "evidence_frames": [frame["frame_id"] for frame in refs],
                "evidence_times_ms": [frame["normalized_pts_ms"] for frame in refs],
                "original_candidate": (
                    by_key.get(label["label_key"], {}).get("label_vi")
                    if status == "corrected"
                    else None
                ),
            }
        )

    scene_cards: list[dict[str, Any]] = []
    if windows:
        ordered = [
            (scene_id, scene_frames.get(scene_id, []), start, end)
            for scene_id, (start, end) in sorted(windows.items(), key=lambda item: item[1][0])
        ]
    else:
        ordered_pairs = sorted(scene_frames.items(), key=lambda item: int(item[1][0]["normalized_pts_ms"]))
        ordered = []
        for index, (scene_id, frames) in enumerate(ordered_pairs):
            start = int(frames[0]["normalized_pts_ms"])
            end = (
                int(ordered_pairs[index + 1][1][0]["normalized_pts_ms"])
                if index + 1 < len(ordered_pairs)
                else max(start + 1, int(asset_duration_ms))
            )
            ordered.append((scene_id, frames, start, end))
    for scene_id, frames, start, end in ordered:
        frame_ids = {frame["frame_id"] for frame in frames}
        scene_candidates = [
            item
            for item in candidates
            if frame_ids & set(item["evidence_frames"])
        ]
        scene_labels = [
            item for item in user_entries if frame_ids & set(item["evidence_frames"])
        ]
        scene_cards.append(
            {
                "scene_id": scene_id,
                "start_ms": start,
                "end_ms": end,
                "frame_ids": sorted(frame_ids),
                "candidates": scene_candidates,
                "user_labels": scene_labels,
            }
        )

    return {
        "kind": "phuthanhai.context_overlay",
        "schema_version": OVERLAY_VERSION,
        "profile_version": profiles.PROFILE_VERSION,
        "profile": profile_id,
        "profile_label_vi": spec.label_vi,
        "asset_id": asset_id,
        "analysis_id": evidence.get("analysis_id"),
        "raw_evidence_sha256": raw_evidence_digest(evidence),
        "raw_evidence_immutable": True,
        "scene_timecode_source": scene_timecode_source,
        "scene_timecode_status": "verified" if windows and not boundary_errors else (
            "invalid" if boundary_errors else "unverified"
        ),
        "scene_boundary_errors": boundary_errors,
        "raw_frames": [
            {
                "frame_id": frame["frame_id"],
                "scene_id": frame["scene_id"],
                "normalized_pts_ms": frame["normalized_pts_ms"],
                "interpretation_level": frame["interpretation_level"],
            }
            for frame in evidence.get("frames", [])
        ],
        "coverage_status": evidence.get("coverage_status"),
        "generated_at": _now_iso(),
        "guardrails": list(spec.guardrails),
        "candidates": candidates,
        "unresolved": unresolved,
        "user_labels": user_entries,
        "dismissed_label_keys": dismissed_label_keys,
        "dismissed_labels": dismissed_entries,
        "scenes": scene_cards,
    }


def confirmed_labels_by_key(overlay: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["label_key"]: item for item in overlay.get("user_labels", [])}
