#!/usr/bin/env python3
"""S05.1: golden dataset manager (local, read-only source footage).

Only the operator-authorized folder is scanned. Labels are NOT generated from the
model: each row starts as `pending` with an empty reviewer_label until a human
writes one. Synthetic fixtures are not counted as a genre.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "artifacts" / "qa" / "S05" / "golden"
sys.path.insert(0, str(REPO / "src"))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="/Users/phuthanh/Video/22.10 POV DamCuoi")
    parser.add_argument("--count", type=int, default=10)
    args = parser.parse_args()
    root = Path(args.root).expanduser()
    if not root.is_dir():
        print(f"source folder missing: {root}", file=sys.stderr)
        return 2
    OUT.mkdir(parents=True, exist_ok=True)
    frames = OUT / "frames"
    frames.mkdir(exist_ok=True)
    from phuthanhai.config import Settings
    from phuthanhai.library.service import LibraryService

    service = LibraryService(Settings.load())
    try:
        project = service.create_project("S05 golden", str(root))
        service.scan(project["project_id"])
        assets = service.list_assets(project["project_id"], sort="duration", limit=args.count)["items"]
    finally:
        service.close()
    rows = []
    for index, asset in enumerate(assets):
        source = Path(asset["path"])
        digest = sha256(source)
        frame = frames / f"{index:02d}_{asset['filename'].rsplit('.', 1)[0]}.jpg"
        subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", "0.5", "-i", str(source),
             "-frames:v", "1", "-vf", "scale=640:-2", str(frame)],
            check=True,
        )
        rows.append({
            "asset_id": asset["asset_id"],
            "filename": asset["filename"],
            "source_sha256": digest,
            "genre": "wedding_traditional",
            "scene_time_range_ms": [0, int(asset.get("duration_ms") or 1)],
            "reviewer_label": None,
            "suggested_labels": [],
            "evidence_frames": [],
            "event_scene_label": None,
            "expected": None,
            "reviewed_by": None,
            "review_status": "pending",
            "split": "held_out" if int(digest[:2], 16) % 2 else "development",
            "frame_path": str(frame),
            "notes": "Awaiting human label; no model output used as ground truth.",
        })
    manifest = {
        "kind": "phuthanhai.golden_dataset",
        "schema_version": "1.0.0",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "source_root": str(root),
        "targets": {"wedding_traditional": 10, "vlog_travel": 10, "event_bts": 10},
        "available": {"wedding_traditional": len(rows), "vlog_travel": 0, "event_bts": 0},
        "missing": ["vlog_travel (10 real clips)", "event_bts (10 real clips)"],
        "reviewer_confirmed": 0,
        "rows": rows,
        "held_out_evaluation": None,
        "note": "No held-out metrics: labels are pending human review and two genres are absent.",
    }
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    sheet = ["# S05.1 Golden dataset — review sheet", "",
             "Nguồn: " + str(root), "",
             "| # | Clip | Genre | Split | Frame | Reviewer label |",
             "|---|---|---|---|---|---|"]
    for index, row in enumerate(rows):
        sheet.append(f"| {index} | {row['filename']} | wedding_traditional | {row['split']} | {Path(row['frame_path']).name} | _pending_ |")
    (OUT / "review_sheet.md").write_text("\n".join(sheet) + "\n", encoding="utf-8")
    print(json.dumps({k: manifest[k] for k in ("available", "missing", "reviewer_confirmed", "held_out_evaluation")}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
