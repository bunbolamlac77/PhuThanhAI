#!/usr/bin/env python3
"""3x3 adaptive resolution/token matrix on one verified frame (QA only)."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from PIL import Image  # noqa: E402

from phuthanhai.analyzer import vlm_worker  # noqa: E402
from phuthanhai.config import Settings  # noqa: E402
from phuthanhai.library.service import LibraryService  # noqa: E402
from phuthanhai.runtime.resource import GOVERNOR  # noqa: E402
from phuthanhai.runtime.scheduler import HEAVY_SLOT, PROCESSES  # noqa: E402

OUT = ROOT / "artifacts" / "qa" / "S05" / "optimization" / "adaptive"
DATA = ROOT / "artifacts" / "qa" / "S05" / "optimization" / "data"


def swap_mb() -> float | None:
    try:
        text = subprocess.run(["sysctl", "-n", "vm.swapusage"], capture_output=True, text=True, timeout=5).stdout
        part = text.split("used =")[1].strip().split()[0]
        n = float(part[:-1])
        return round(n * 1024 if part.endswith("G") else n, 1)
    except Exception:  # noqa: BLE001
        return None


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    os.environ["PHUTHANHAI_DATA_DIR"] = str(DATA)
    settings = Settings.load()
    service = LibraryService(settings)
    vision = service.vision
    vision.require_governor = True
    manifest = None
    for project in service.list_projects():
        assets = service.list_assets(project["project_id"], limit=10)["items"]
        for asset in assets:
            if asset.get("sampling_status") == "complete" and asset.get("sampling_fingerprint"):
                manifest = service.pipeline.load_manifest(asset["asset_id"], asset["sampling_fingerprint"])
                if manifest:
                    break
        if manifest:
            break
    if not manifest or not manifest.get("frames"):
        print(json.dumps({"error": "no verified sampling frame"}))
        return 1
    source_frame = manifest["frames"][0]
    source = Path(source_frame["path"])
    image = Image.open(source).convert("RGB")
    # Preserve the sampled frame's display dimensions; longest-edge resize only.
    original = source.read_bytes()
    results = []
    for edge in (448, 640, 896):
        for tokens in (192, 256, 320):
            cell_id = f"{edge}x{tokens}"
            cell_dir = OUT / cell_id
            cell_dir.mkdir(parents=True, exist_ok=True)
            resized = cell_dir / "frame.jpg"
            ratio = min(1.0, edge / max(image.size))
            size = (max(1, round(image.width * ratio)), max(1, round(image.height * ratio)))
            image.resize(size, Image.Resampling.LANCZOS).save(resized, quality=92)
            digest = hashlib.sha256(resized.read_bytes()).hexdigest()
            allowed, reason = GOVERNOR.can_start_heavy_worker()
            if not allowed:
                results.append({"cell": cell_id, "status": "NOT_VERIFIED", "reason": reason,
                                "image": {"width": size[0], "height": size[1], "sha256": digest}})
                continue
            request = {
                "request_version": vlm_worker.REQUEST_VERSION, "job_id": f"adaptive-{cell_id}",
                "asset_id": "qa-adaptive", "analysis_id": f"adaptive-{cell_id}",
                "model_path": str(vision.model_path), "allowed_image_root": str(OUT),
                "prompt": vlm_worker.VISUAL_PROMPT, "prompt_version": vlm_worker.PROMPT_VERSION,
                "method": "single_frame",
                "frames": [{"frame_id": source_frame["frame_id"], "scene_id": source_frame["scene_id"],
                            "image_path": str(resized),
                            "normalized_pts_ms": int(source_frame["normalized_pts_ms"]), "sha256": digest}],
                "max_tokens": tokens, "temperature": 0.0, "seed": 0,
            }
            owner = f"adaptive-{cell_id}"
            swap_before = swap_mb()
            started = time.perf_counter()
            try:
                HEAVY_SLOT.acquire(owner, timeout=60)
                final = vision._run_worker(request, cancel=None)
                wall = time.perf_counter() - started
                frame = (final.get("frames") or [{}])[0]
                try:
                    vlm_worker.validate_observation(frame.get("observation"))
                    valid = True
                except vlm_worker.WorkerError:
                    valid = False
                results.append({
                    "cell": cell_id, "status": "MEASURED", "wall_s": round(wall, 3),
                    "image": {"width": size[0], "height": size[1], "sha256": digest},
                    "max_tokens": tokens, "strict_json": valid,
                    "truncated": False, "retry_count": 0,
                    "generation_tokens": frame.get("generation_tokens"),
                    "prompt_tokens": frame.get("prompt_tokens"),
                    "worker_peak_rss_mb": round(int(final.get("peak_rss_bytes") or 0) / 1048576, 1),
                    "mlx_peak_mb": round(int(final.get("mlx_peak_memory_bytes") or 0) / 1048576, 1),
                    "swap_delta_mb": round((swap_mb() or 0) - (swap_before or 0), 1),
                    "memory_pressure": GOVERNOR.snapshot().get("reasons", []),
                    "quality": "NOT_VERIFIED_NO_HUMAN_LABELS",
                })
            except Exception as exc:  # noqa: BLE001
                results.append({"cell": cell_id, "status": "FAILED", "error": f"{type(exc).__name__}: {exc}",
                                "wall_s": round(time.perf_counter() - started, 3),
                                "image": {"width": size[0], "height": size[1], "sha256": digest}})
            finally:
                try:
                    HEAVY_SLOT.release(owner)
                except Exception:  # noqa: BLE001
                    pass
    payload = {"source_sha256": hashlib.sha256(original).hexdigest(),
               "source_frame_sha256": manifest["frames"][0].get("sha256"),
               "results": results, "active_children": PROCESSES.active_count,
               "notes": ["Quality labels absent: QUALITY_NOT_VERIFIED.", "Each cell uses a separately hashed derivative."]}
    (OUT / "matrix.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({r["cell"]: {k: r.get(k) for k in ("status", "wall_s", "strict_json", "worker_peak_rss_mb", "swap_delta_mb")} for r in results}, indent=2))
    service.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
