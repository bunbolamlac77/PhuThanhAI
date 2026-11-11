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
from datetime import datetime, timezone
from typing import Any

from phuthanhai.analyzer import context_profiles as profiles

OVERLAY_VERSION = "1.0.0"
RITUAL_TERMS = {
    "incense",
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


def _hits(spec: profiles.LabelSpec, text: str) -> list[str]:
    found: list[str] = []
    for term in (*spec.signals, *spec.required_any):
        if _norm(term) in text and term not in found:
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
) -> dict[str, Any]:
    spec = profiles.profile(profile_id)
    raw_frames = [
        frame
        for frame in evidence.get("frames", [])
        if frame.get("interpretation_level") == "observed"
    ]
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
    dismissed = {
        label["label_key"]
        for label in labels
        if (label.get("status") or label.get("label_status")) == "dismissed"
    }
    candidates = [item for item in candidates if item["label_key"] not in dismissed]
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
    ordered = sorted(scene_frames.items(), key=lambda item: int(item[1][0]["normalized_pts_ms"]))
    for index, (scene_id, frames) in enumerate(ordered):
        start = int(frames[0]["normalized_pts_ms"])
        end = (
            int(ordered[index + 1][1][0]["normalized_pts_ms"])
            if index + 1 < len(ordered)
            else max(start + 1, int(asset_duration_ms))
        )
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
        "dismissed_label_keys": sorted(dismissed),
        "scenes": scene_cards,
    }


def confirmed_labels_by_key(overlay: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["label_key"]: item for item in overlay.get("user_labels", [])}
