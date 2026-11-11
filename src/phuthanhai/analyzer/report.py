"""S04 report assembly: AI-friendly Vietnamese reports + frozen V2/V3 packages.

Separation is explicit in every artifact:

* raw visual evidence (observed, immutable),
* context candidates (hypothesis, profile-derived),
* user-confirmed / corrected labels,
* unresolved/unknown,
* corrections.

Portable objects are validated with the shipped contract validators before they
are written; no field is added to the frozen schemas.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from phuthanhai.analyzer import context_profiles as profiles
from phuthanhai.contracts.validators import (
    ContractError,
    canonical_json_bytes,
    load_schemas,
    render_portable_workspace_md,
    semantic_validate_package,
    validate_document,
)

REPORT_VERSION = "s04-report-1.0.0"


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def _uuid() -> str:
    return str(uuid.uuid4())


def _fps(value: str | None) -> str | None:
    if not value:
        return None
    if "/" in value:
        return value
    try:
        number = float(value)
    except ValueError:
        return None
    if number <= 0:
        return None
    return f"{round(number * 1000)}/1000"


def _analysis(asset: dict[str, Any], manifest: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any] | None:
    observed = [
        frame for frame in evidence["frames"] if frame["interpretation_level"] == "observed"
    ]
    if not observed:
        return None
    duration = int(manifest["timeline"]["duration_ms"])
    scenes: list[dict[str, Any]] = []
    grouped: dict[str, list[dict[str, Any]]] = {}
    for frame in observed:
        grouped.setdefault(frame["scene_id"], []).append(frame)
    ordered = sorted(grouped.items(), key=lambda item: int(item[1][0]["normalized_pts_ms"]))
    previous_end = 0
    for index, (scene_id, frames) in enumerate(ordered):
        start = previous_end
        next_start = int(ordered[index + 1][1][0]["normalized_pts_ms"]) if index + 1 < len(ordered) else duration
        end = min(duration, max(start + 1, next_start))
        if end <= start:
            end = min(duration, start + 1)
        previous_end = end
        observations = []
        for frame in frames:
            obs = frame["observation"]
            time_ms = int(frame["normalized_pts_ms"])
            time_ms = min(max(time_ms, start), max(start, end - 1))
            observations.append(
                {
                    "id": frame["frame_id"],
                    "time_ms": time_ms,
                    "description": obs["description"],
                    "objects": list(obs.get("objects") or []),
                    "actions": list(obs.get("actions") or []),
                    "camera": obs.get("camera"),
                    "lighting": obs.get("lighting"),
                    "visible_text": list(obs.get("visible_text") or []),
                    "uncertainty": list(obs.get("uncertainty") or []),
                }
            )
        descriptions = [item["description"] for item in observations]
        summary = (descriptions[0] if descriptions else "Không có mô tả.")[:600]
        facts = sorted({obj for item in observations for obj in item["objects"]})[:12]
        uncertainties = sorted({item for obs in observations for item in obs["uncertainty"]})[:8]
        scenes.append(
            {
                "id": scene_id,
                "start_ms": start,
                "end_ms": end,
                "coverage_status": "sampled",
                "summary": summary,
                "visual_facts": facts,
                "uncertainties": uncertainties,
                "observations": observations,
            }
        )
    # Scenes must tile the whole timeline exactly; the last one ends at duration.
    if scenes:
        scenes[-1]["end_ms"] = duration
        scenes[-1]["observations"] = [
            {**observation, "time_ms": min(observation["time_ms"], duration - 1)}
            for observation in scenes[-1]["observations"]
        ]
    if not scenes:
        return None

    expected_gap = int(manifest.get("expected_max_gap_ms") or manifest.get("plan", {}).get("max_gap_ms") or 1)
    observed_gap = int(manifest.get("observed_max_gap_ms") or 0)
    times = sorted(int(frame["normalized_pts_ms"]) for frame in observed)
    intervals = []
    for left, right in zip(times, times[1:]):
        if right - left > expected_gap:
            intervals.append([left, max(left + 1, right)])

    source = manifest.get("timeline") or {}
    profile = manifest.get("profile", "quick")
    return {
        "schema_version": "1.0.0",
        "kind": "phuthanhai.video_analysis",
        "analysis_id": evidence["analysis_id"],
        "created_at": _now_iso(),
        "status": (
            "complete"
            if evidence.get("coverage_status") == "sufficient" and observed_gap <= expected_gap
            else "partial"
        ),
        "source": {
            "filename": source.get("filename") or asset["filename"],
            "sha256": source.get("sha256") or evidence.get("source_sha256") or "",
            "duration_ms": duration,
            "width": int(source.get("width") or asset.get("width") or 1),
            "height": int(source.get("height") or asset.get("height") or 1),
            "fps_nominal": _fps(source.get("fps_nominal") or asset.get("fps_nominal")),
            "vfr_detected": bool(source.get("vfr_detected", False)),
            "has_audio": bool(source.get("has_audio", asset.get("has_audio", False))),
            "first_video_pts_ms": int(source.get("first_video_pts_ms") or 0),
            "time_origin": "first_presented_video_frame",
        },
        "analysis": {
            "model_id": str(evidence.get("model_identity", {}).get("name") or "qwen3-vl"),
            "profile": profile if profile in {"quick", "detailed", "thorough"} else "quick",
            "sampling_mode": "adaptive_keyframes",
            "frame_count": len(observed),
            "vision_only": True,
        },
        "scenes": scenes,
        "coverage": {
            "method": "adaptive_keyframes",
            "timeline_start_ms": 0,
            "timeline_end_ms": duration,
            "observed_frame_count": len(observed),
            "max_gap_ms": expected_gap,
            "max_observed_gap_ms": observed_gap,
            "intervals_without_visual_observation": intervals,
            "is_every_frame_analyzed": False,
        },
        "warnings": sorted(
            set(evidence.get("warnings") or [])
            | {"sampled_frames_not_all_frames"}
            | ({"audio_not_transcribed"} if bool(source.get("has_audio") or asset.get("has_audio")) else set())
        ),
    }


def _annotation_out(note: dict[str, Any]) -> dict[str, Any]:
    wall = None
    if note.get("wall_time_of_day"):
        wall = {
            "time_of_day": note["wall_time_of_day"],
            "date": note.get("wall_date"),
            "timezone": note.get("wall_timezone") or "UTC",
            "precision": note.get("wall_precision") or "minute",
        }
    note_type = "context" if note.get("note_type") == "context_label" else note.get("note_type", "context")
    if note_type not in {"event", "context", "location", "clock_time", "person", "other"}:
        note_type = "other"
    return {
        "annotation_id": note["annotation_id"],
        "scope": note.get("scope") or "clip",
        "asset_id": note.get("asset_id"),
        "start_ms": note.get("start_ms"),
        "end_ms": note.get("end_ms"),
        "text": note.get("text", ""),
        "note_type": note_type,
        "source": "user_annotation",
        "assertion_status": note.get("assertion_status") or "user_asserted",
        "wall_clock": wall,
        "created_at": note.get("created_at"),
        "updated_at": note.get("updated_at"),
        "revision": int(note.get("revision") or 1),
    }


def _review_out(asset: dict[str, Any], notes: list[dict[str, Any]]) -> dict[str, Any]:
    review = asset.get("review") or {}
    rating = review.get("rating")
    return {
        "review_id": _uuid(),
        "asset_id": asset["asset_id"],
        "status": "reviewed" if rating else "unreviewed",
        "rating": int(rating) if rating else None,
        "favorite": bool(review.get("favorite")),
        "labels": list(review.get("tags") or []),
        "selected_ranges": [],
        "last_playhead_ms": None,
        "note_ids": [note["annotation_id"] for note in notes if note.get("asset_id") == asset["asset_id"]],
    }


def assemble_documents(
    *,
    project: dict[str, Any],
    assets: list[dict[str, Any]],
    annotations: list[dict[str, Any]],
    evidence_by_asset: dict[str, dict[str, Any]],
    overlays: dict[str, dict[str, Any]],
    manifests: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    analyses: list[dict[str, Any]] = []
    for asset in assets:
        evidence = evidence_by_asset.get(asset["asset_id"])
        manifest = manifests.get(asset["asset_id"])
        if not evidence or not manifest:
            continue
        built = _analysis(asset, manifest, evidence)
        if built is not None:
            analyses.append(built)
    analysis_ids = {analysis["analysis_id"] for analysis in analyses}

    manifest_assets = []
    for asset in assets:
        analysis_id = evidence_by_asset.get(asset["asset_id"], {}).get("analysis_id")
        review = asset.get("review") or {}
        manifest_assets.append(
            {
                "asset_id": asset["asset_id"],
                "display_filename": asset["filename"],
                "source_sha256": evidence_by_asset.get(asset["asset_id"], {}).get("source_sha256"),
                "analysis_id": analysis_id if analysis_id in analysis_ids else None,
                "duration_ms": int(asset.get("duration_ms") or 1),
                "review_state": "reviewed" if review.get("rating") else "unreviewed",
                "tags": list(review.get("tags") or []),
                "capture_time": None,
                "capture_time_source": "unknown",
            }
        )

    bundle = {
        "kind": "phuthanhai.project_bundle",
        "schema_version": "1.0.0",
        "project_id": project["project_id"],
        "created_at": _now_iso(),
        "manifest": {
            "kind": "phuthanhai.project_manifest",
            "schema_version": "1.0.0",
            "project_id": project["project_id"],
            "name": project["name"],
            "analysis_profile": project.get("analysis_profile", profiles.DEFAULT_PROFILE),
            "created_at": project.get("created_at") or _now_iso(),
            "assets": manifest_assets,
        },
        "annotation_set": {
            "kind": "phuthanhai.annotation_set",
            "schema_version": "1.0.0",
            "project_id": project["project_id"],
            "revision": int(project.get("revision") or 0),
            "annotations": [_annotation_out(note) for note in annotations],
        },
        "analyses": analyses,
        "temporal_events": [],
        "warnings": [] if analyses else ["no analysed asset"],
        "report_profile": project.get("analysis_profile", profiles.DEFAULT_PROFILE),
    }

    evidence_rows: list[dict[str, Any]] = []
    corrections: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    for asset in assets:
        overlay = overlays.get(asset["asset_id"])
        evidence = evidence_by_asset.get(asset["asset_id"])
        if not overlay or not evidence:
            continue
        analysis_id = evidence["analysis_id"]
        frames = {frame["frame_id"]: frame for frame in evidence["frames"]}
        for candidate in overlay.get("candidates", []):
            frame_ids = [fid for fid in candidate["evidence_frames"] if fid in frames]
            if not frame_ids:
                continue
            times = [int(frames[fid]["normalized_pts_ms"]) for fid in frame_ids]
            start = min(times)
            end = min(int(asset["duration_ms"]), max(times) + 1)
            if end <= start:
                end = start + 1
            evidence_rows.append(
                {
                    "event_id": _uuid(),
                    "asset_id": asset["asset_id"],
                    "source_analysis_id": analysis_id,
                    "start_ms": start,
                    "end_ms": end,
                    "frame_ids": frame_ids,
                    "method": "sequential_frames",
                    "description": f"{candidate['label_vi']}: {candidate.get('description_vi') or ''}".strip(),
                    "interpretation_level": "hypothesis",
                    "uncertainties": [candidate.get("uncertainty_vi")] if candidate.get("uncertainty_vi") else [],
                }
            )
        for label in overlay.get("user_labels", []):
            scene_refs = label.get("evidence_scenes") or []
            scene_id = label.get("scene_id") or (scene_refs[0] if scene_refs else None)
            scene_frames = [
                frame["frame_id"]
                for frame in evidence["frames"]
                if frame["scene_id"] == scene_id
            ]
            frame_ids = [fid for fid in label.get("evidence_frames", []) if fid in scene_frames] or scene_frames[:1]
            if not scene_id or not frame_ids:
                continue
            corrections.append(
                {
                    "correction_id": label.get("annotation_id") or _uuid(),
                    "asset_id": asset["asset_id"],
                    "source_analysis_id": analysis_id,
                    "scene_id": scene_id,
                    "original_text": (label.get("original_candidate") or "")[:4000],
                    "corrected_text": label["label_vi"][:4000],
                    "frame_ids": frame_ids,
                    "author": "user",
                    "created_at": _now_iso(),
                }
            )

    extension = {
        "kind": "phuthanhai.workspace_extension",
        "schema_version": "1.0.0",
        "project_id": project["project_id"],
        "revision": int(project.get("revision") or 0) + int(project.get("profile_revision") or 1),
        "created_at": _now_iso(),
        "reviews": [
            _review_out(asset, annotations)
            for asset in assets
            if asset["asset_id"] in evidence_by_asset
        ],
        "evidence": evidence_rows,
        "corrections": corrections,
        "conflicts": conflicts,
    }
    package = {
        "kind": "phuthanhai.workspace_package",
        "schema_version": "1.0.0",
        "package_id": _uuid(),
        "created_at": _now_iso(),
        "project_id": project["project_id"],
        "legacy_bundle": bundle,
        "extension": extension,
        "warnings": [],
    }
    return {"bundle": bundle, "extension": extension, "package": package, "analyses": analyses}


def validate_documents(documents: dict[str, Any], contracts_dir: Path) -> None:
    schemas = load_schemas(contracts_dir)
    for analysis in documents["analyses"]:
        validate_document(schemas, analysis, semantic=True)
    validate_document(schemas, documents["package"], semantic=True)
    semantic_validate_package(documents["package"])


def _video_markdown(asset: dict[str, Any], analysis: dict[str, Any], overlay: dict[str, Any]) -> str:
    lines = [
        f"## {asset['filename']}",
        "",
        f"- Asset: `{asset['asset_id']}`",
        f"- Thời lượng: {analysis['source']['duration_ms']} ms",
        f"- Môi trường: `{overlay.get('profile')}` ({overlay.get('profile_label_vi')})",
        f"- Trạng thái coverage: {overlay.get('coverage_status')}",
        "- Phân tích chỉ từ hình ảnh; **không nghe, không phiên âm âm thanh**.",
        "",
        "### 1. AI quan sát (raw evidence, bất biến)",
    ]
    for scene in analysis["scenes"]:
        lines.append(f"#### {scene['id']} — {scene['start_ms']}–{scene['end_ms']} ms")
        for observation in scene["observations"]:
            lines.append(
                f"- `{observation['id']}` @ {observation['time_ms']} ms: {observation['description']}"
            )
        if scene["uncertainties"]:
            lines.append(f"- Chưa chắc chắn: {'; '.join(scene['uncertainties'])}")
    lines += ["", "### 2. AI gợi ý theo ngữ cảnh (hypothesis)"]
    if overlay.get("candidates"):
        for candidate in overlay["candidates"]:
            lines.append(
                f"- **{candidate['label_vi']}** — bằng chứng: {', '.join(candidate['evidence_frames'])}; "
                f"tín hiệu: {', '.join(candidate['signals'][:8])}. {candidate.get('uncertainty_vi') or ''}"
            )
    else:
        lines.append("- Không có gợi ý đủ bằng chứng.")
    if overlay.get("unresolved"):
        lines.append("")
        lines.append("Chưa xác định:")
        for item in overlay["unresolved"]:
            lines.append(f"- {item['label_vi']}: {item['reason_vi']} ({', '.join(item['evidence_frames'])})")
    lines += ["", "### 3. Người dùng xác nhận / chỉnh sửa"]
    if overlay.get("user_labels"):
        for label in overlay["user_labels"]:
            suffix = f" (sửa từ: {label['original_candidate']})" if label.get("original_candidate") else ""
            lines.append(
                f"- **{label['label_vi']}**{suffix} — annotation `{label.get('annotation_id')}`, "
                f"frame {', '.join(label['evidence_frames'])}"
            )
    else:
        lines.append("- Chưa có nhãn người dùng xác nhận.")
    if overlay.get("dismissed_label_keys"):
        lines.append(f"- Đã bỏ nhãn gợi ý: {', '.join(overlay['dismissed_label_keys'])}")
    lines += ["", "### 4. Không xác định / thiếu bằng chứng"]
    unknowns = [
        frame for frame in overlay.get("raw_frames", []) if frame.get("interpretation_level") == "unknown"
    ]
    if unknowns:
        for frame in unknowns:
            lines.append(f"- `{frame['frame_id']}`: chưa có quan sát hợp lệ.")
    elif overlay.get("unresolved"):
        lines.append("- Có nhãn gợi ý chưa đủ bằng chứng (xem mục 2).")
    else:
        lines.append("- Không có frame chưa xác định trong mẫu đã phân tích.")
    return "\n".join(lines) + "\n"


def build_markdown(
    documents: dict[str, Any],
    assets: list[dict[str, Any]],
    overlays: dict[str, dict[str, Any]],
    evidence_by_asset: dict[str, dict[str, Any]],
) -> tuple[str, str, str]:
    project_id = documents["bundle"]["project_id"]
    profile_id = documents["bundle"]["report_profile"]
    analyses = {analysis["analysis_id"]: analysis for analysis in documents["analyses"]}
    sections: list[str] = []
    for asset in assets:
        evidence = evidence_by_asset.get(asset["asset_id"])
        if not evidence:
            continue
        analysis = analyses.get(evidence["analysis_id"])
        overlay = overlays.get(asset["asset_id"])
        if not analysis or not overlay:
            continue
        sections.append(_video_markdown(asset, analysis, overlay))
    video = "\n".join(sections) if sections else "Không có video nào đã được phân tích.\n"
    project_lines = [
        f"# Báo cáo dự án — {documents['bundle']['manifest']['name']}",
        "",
        f"- Project: `{project_id}`",
        f"- Môi trường phân tích: `{profile_id}` ({profiles.profile(profile_id).label_vi})",
        f"- Số video có evidence: {len(documents['analyses'])}",
        f"- Số annotation: {len(documents['bundle']['annotation_set']['annotations'])}",
        f"- Số nhãn người dùng: {sum(len(overlay.get('user_labels', [])) for overlay in overlays.values())}",
        "- Phân tích hình ảnh; không có nhận định về âm thanh/lời nói.",
        "",
        "## Tổng hợp video",
    ]
    for asset in assets:
        evidence = evidence_by_asset.get(asset["asset_id"])
        if not evidence:
            continue
        overlay = overlays.get(asset["asset_id"], {})
        project_lines.append(
            f"- {asset['filename']}: {len(evidence['frames'])} frame, coverage={evidence.get('coverage_status')}, "
            f"gợi ý={len(overlay.get('candidates', []))}, xác nhận={len(overlay.get('user_labels', []))}"
        )
    project = "\n".join(project_lines) + "\n"
    portable = render_portable_workspace_md(documents["package"])
    return video, project, portable


def write_reports(
    *,
    documents: dict[str, Any],
    assets: list[dict[str, Any]],
    overlays: dict[str, dict[str, Any]],
    evidence_by_asset: dict[str, dict[str, Any]],
    out_dir: Path,
) -> dict[str, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    video_md, project_md, portable_md = build_markdown(documents, assets, overlays, evidence_by_asset)
    files = {
        "video_report_ai.md": video_md,
        "project_report_ai.md": project_md,
        "video_report_portable.md": portable_md,
        "workspace_report_portable.md": portable_md,
    }
    for name, content in files.items():
        (out_dir / name).write_text(content, encoding="utf-8")
    for name, key in (
        ("project_bundle.json", "bundle"),
        ("workspace_extension.json", "extension"),
        ("workspace_package.json", "package"),
    ):
        (out_dir / name).write_bytes(canonical_json_bytes(documents[key]))
    return {
        "dir": str(out_dir),
        "files": sorted([*files, "project_bundle.json", "workspace_extension.json", "workspace_package.json"]),
    }
