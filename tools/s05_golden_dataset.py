#!/usr/bin/env python3
"""S05.2 golden dataset builder (manifest + PTS frame previews + review UI).

Rules:
* only an operator-supplied root is scanned; empty/invalid paths exit 2;
* QA uses its own data root, never the user's registry;
* clips are selected across the natural order, not just the shortest ones;
* rows are manifest scenes/time ranges with concrete frame PTS previews;
* labels start `pending`; nothing from Qwen3-VL is used as ground truth;
* the split is group-aware (same folder = same shoot) and cannot be claimed as
  leak-free while all clips come from one shoot.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "artifacts" / "qa" / "S05" / "golden"
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "tools"))
from qa_paths import QAPathError, require_source_root  # noqa: E402
from phuthanhai.analyzer.sampling_pipeline import SamplingError  # noqa: E402
from phuthanhai.analyzer.timeline import (  # noqa: E402
    TimelineError,
    extract_verified_frame,
    read_timeline,
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


MAX_QA_CLIP_MS = 60_000


def select_diverse(items: list[dict], count: int) -> list[dict]:
    """Spread across natural order, but keep QA clips bounded in duration.

    Scene scanning decodes the entire clip; selecting multi-minute 4K DJI clips
    made the review build stall. Long clips are excluded, not silently downscaled.
    """

    eligible = [
        item for item in items
        if 0 < int(item.get("duration_ms") or 0) <= MAX_QA_CLIP_MS
    ]
    if len(eligible) < count:
        eligible = sorted(items, key=lambda item: int(item.get("duration_ms") or 0))[:count]
    ordered = sorted(eligible, key=lambda item: item["filename"])
    if len(ordered) <= count:
        return ordered
    step = (len(ordered) - 1) / (count - 1)
    return [ordered[round(index * step)] for index in range(count)]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=os.environ.get("PHUTHANHAI_TEST_FOOTAGE"))
    parser.add_argument("--count", type=int, default=10)
    args = parser.parse_args()
    try:
        root = require_source_root(args.root, label="--root/PHUTHANHAI_TEST_FOOTAGE")
    except QAPathError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    OUT.mkdir(parents=True, exist_ok=True)
    work = OUT / "work"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    os.environ["PHUTHANHAI_DATA_DIR"] = str(work)  # never the user's registry
    frames_dir = OUT / "frames"
    shutil.rmtree(frames_dir, ignore_errors=True)
    frames_dir.mkdir(parents=True)

    from phuthanhai.config import Settings
    from phuthanhai.library.service import LibraryService

    service = LibraryService(Settings.load())
    try:
        project = service.create_project("S05 golden", str(root))
        service.scan(project["project_id"])
        assets = select_diverse(service.list_assets(project["project_id"], limit=5000)["items"], args.count)
    finally:
        service.close()

    service = LibraryService(Settings.load())
    rows = []
    try:
        for index, asset in enumerate(assets):
            asset = service.get_asset(asset["asset_id"])
            source = Path(asset["path"])
            digest = sha256(source)
            fallback = False
            try:
                if asset.get("sampling_status") != "complete":
                    service.pipeline.run(asset, source, profile="quick", max_edge=640)
                asset = service.get_asset(asset["asset_id"])
                manifest = service.pipeline.load_manifest(asset["asset_id"], asset["sampling_fingerprint"])
            except (SamplingError, TimelineError) as exc:
                # Some operator clips fail the S02 extractor; still capture a
                # PTS-verified preview instead of aborting the whole dataset.
                fallback = True
                timeline = read_timeline(source)
                manifest = {
                    "segments": [{"scene_id": "S0001", "start_ms": 0, "end_ms": timeline.duration_ms}],
                    "frames": [],
                }
                for fraction in (0.1, 0.5, 0.9):
                    requested = int(timeline.duration_ms * fraction)
                    frame_path = work / "fallback" / f"{index:02d}_{requested}.jpg"
                    frame_path.parent.mkdir(parents=True, exist_ok=True)
                    verified = extract_verified_frame(
                        source, timeline, requested, frame_path, max_edge=640, quality=4
                    )
                    manifest["frames"].append(
                        {
                            "frame_id": f"F{len(manifest['frames']) + 1:04d}",
                            "scene_id": "S0001",
                            "normalized_pts_ms": verified.normalized_pts_ms,
                            "path": str(verified.path),
                        }
                    )
                manifest["fallback_reason"] = f"{type(exc).__name__}: {exc}"[:300]
            shoot = str(root)
            for segment in manifest.get("segments", []):
                scene_frames = [
                    frame for frame in manifest["frames"]
                    if frame["scene_id"] == segment["scene_id"]
                    and segment["start_ms"] <= frame["normalized_pts_ms"] < segment["end_ms"]
                ]
                previews = []
                for frame in scene_frames:
                    target = frames_dir / f"{index:02d}_{segment['scene_id']}_{frame['frame_id']}.jpg"
                    shutil.copy(frame["path"], target)
                    previews.append({
                        "frame_id": frame["frame_id"],
                        "pts_ms": frame["normalized_pts_ms"],
                        "path": f"frames/{target.name}",
                    })
                rows.append({
                    "row_id": f"{index:02d}-{segment['scene_id']}",
                    "asset_id": asset["asset_id"],
                    "filename": asset["filename"],
                    "source_sha256": digest,
                    "genre": "wedding_traditional",
                    "scene_id": segment["scene_id"],
                    "scene_time_range_ms": [segment["start_ms"], segment["end_ms"]],
                    "reviewer_label": None,
                    "suggested_labels": [],
                    "evidence_frames": [item["frame_id"] for item in previews],
                    "frame_previews": previews,
                    "event_scene_label": None,
                    "expected": None,
                    "label_polarity": None,
                    "reviewed_by": None,
                    "review_status": "pending",
                    "leakage_group": shoot,
                    "split": "development",
                    "notes": (
                        "PTS from fallback frame extraction; S02 pipeline failed for this clip. Awaiting human label."
                        if fallback else
                        "PTS from S02 manifest; awaiting human label. No model output used."
                    ),
                })
    finally:
        service.close()

    manifest = {
        "kind": "phuthanhai.golden_dataset",
        "schema_version": "2.0.0",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "source_root": str(root),
        "qa_data_root": str(work),
        "selection_policy": {"max_clip_duration_ms": MAX_QA_CLIP_MS, "diversity": "natural-order spread within bounded duration"},
        "targets": {"wedding_traditional": 10, "vlog_travel": 10, "event_bts": 10},
        "available": {"wedding_traditional": len(rows), "vlog_travel": 0, "event_bts": 0},
        "missing": ["vlog_travel (10 real clips)", "event_bts (10 real clips)"],
        "reviewer_confirmed": 0,
        "split": {
            "locked": False,
            "strategy": "group-aware by source root/shoot",
            "leakage_groups": sorted({row["leakage_group"] for row in rows}),
            "held_out_rows": 0,
            "reason": "All available clips belong to one wedding shoot; a held-out split would leak the same shoot.",
        },
        "quality_criteria": {
            "locked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "precision_min": 0.8, "recall_min": 0.6, "altar_false_positive_rate_max": 0.05,
            "timestamp_tolerance_ms": 150,
        },
        "held_out_evaluation": None,
        "rows": rows,
    }
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    (OUT / "criteria.json").write_text(json.dumps(manifest["quality_criteria"], indent=2), encoding="utf-8")
    _write_review_html(manifest)
    print(json.dumps({"available": manifest["available"], "missing": manifest["missing"],
                      "reviewer_confirmed": 0, "split_locked": False}, indent=2))
    return 0


def _write_review_html(manifest: dict) -> None:
    import html
    options = sorted({
        "Bàn gia tiên", "Bàn trang trí lễ cưới", "Bàn tiệc", "Mâm quả / tráp lễ",
        "Cổng cưới", "Cô dâu, chú rể chuẩn bị", "Dàn bưng quả", "Lễ gia tiên",
        "Thắp hương / dâng lễ", "Rước dâu / đón dâu", "Chụp ảnh gia đình",
        "Phỏng vấn", "Tiệc cưới", "Không xác định",
    })
    parts = ["""<!doctype html><html lang="vi"><head><meta charset="utf-8">
<title>PhuThanhAI golden labels</title>
<style>body{font:14px system-ui;margin:20px;background:#0e141a;color:#e6edf3}
.row{border:1px solid #30404d;border-radius:8px;padding:10px;margin:10px 0}
img{max-height:240px;border-radius:6px;margin:4px}label{display:block;margin:6px 0}
select,input{background:#16212b;color:#e6edf3;border:1px solid #30404d;padding:4px}</style></head><body>
<h1>Golden labels — wedding</h1>
<p>Điền nhãn, polarity, tên người duyệt rồi bấm tải JSON. Không có nhãn nào được tự xác nhận.</p>"""]
    for row in manifest["rows"]:
        safe = html.escape(row["row_id"])
        parts.append(f'<div class="row" data-row="{safe}"><strong>{safe}</strong> {html.escape(row["filename"])} · {row["scene_id"]} · {row["scene_time_range_ms"][0]}–{row["scene_time_range_ms"][1]} ms')
        for preview in row["frame_previews"]:
            parts.append(f'<figure style="display:inline-block"><img src="{preview["path"]}"><figcaption>{preview["frame_id"]} @ {preview["pts_ms"]} ms</figcaption></figure>')
        opts = "".join(f"<option>{html.escape(value)}</option>" for value in options)
        parts.append(f'<label>Nhãn <select class="label">{opts}</select></label>'
                     '<label>Polarity <select class="polarity"><option>positive</option><option>negative</option><option>unknown</option></select></label>'
                     '<label>Ghi chú <input class="note" size="50"></label></div>')
    parts.append("""<label>Người duyệt <input id="reviewer" placeholder="Tên người duyệt"></label>
<button id="download">Tải labels.json</button>
<script>
document.getElementById('download').onclick = () => {
  const reviewer = document.getElementById('reviewer').value.trim();
  if (!reviewer) { alert('Cần tên người duyệt'); return; }
  const rows = [...document.querySelectorAll('.row')].map(row => ({
    row_id: row.dataset.row,
    reviewer_label: row.querySelector('.label').value,
    label_polarity: row.querySelector('.polarity').value,
    note: row.querySelector('.note').value,
    reviewed_by: reviewer,
    review_status: 'confirmed'
  }));
  const blob = new Blob([JSON.stringify({reviewer, confirmed_at: new Date().toISOString(), rows}, null, 2)], {type: 'application/json'});
  const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = 'labels.json'; a.click();
};
</script></body></html>""")
    (OUT / "review.html").write_text("\n".join(parts), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
