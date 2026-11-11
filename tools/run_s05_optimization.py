#!/usr/bin/env python3
"""S05.4R end-to-end benchmark: one real sampling run, one warm run, then modes."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from phuthanhai.library.service import LibraryService  # noqa: E402
from phuthanhai.config import Settings  # noqa: E402
from phuthanhai.runtime.resource import GOVERNOR  # noqa: E402

OUT = ROOT / "artifacts" / "qa" / "S05" / "optimization"


def swap_used_mb() -> float | None:
    try:
        text = subprocess.run(["sysctl", "-n", "vm.swapusage"], capture_output=True, text=True, timeout=5).stdout
        part = text.split("used =")[1].strip().split()[0]
        number = float(part[:-1])
        return round(number * 1024 if part.endswith("G") else number, 1)
    except Exception:  # noqa: BLE001
        return None


def memory_state() -> dict:
    try:
        return GOVERNOR.snapshot()
    except Exception:  # noqa: BLE001
        return {}


def clear_inference(service: LibraryService, *, keep_frames: bool) -> None:
    root = service.settings.data.inference
    if not root.exists():
        (root / "frames").mkdir(parents=True, exist_ok=True)
        return
    for child in root.iterdir():
        if keep_frames and child.name == "frames":
            continue
        shutil.rmtree(child) if child.is_dir() else child.unlink()
    (root / "frames").mkdir(parents=True, exist_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--clip", required=True)
    parser.add_argument("--profile", default="detailed", choices=["quick", "detailed", "thorough"])
    parser.add_argument("--modes", default="full,balanced,smart")
    args = parser.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    data = OUT / "data"
    if data.exists():
        shutil.rmtree(data)
    footage = OUT / "footage"
    footage.mkdir(parents=True, exist_ok=True)
    clip = footage / "bench.mp4"
    shutil.copy(Path(args.clip), clip)
    os.environ["PHUTHANHAI_DATA_DIR"] = str(data)
    os.environ.setdefault("PHUTHANHAI_SMART_FRAME_BUDGET", "2")
    os.environ.setdefault("PHUTHANHAI_BALANCED_FRAME_BUDGET", "3")

    service = LibraryService(Settings.load())
    project = service.create_project("S05.4R benchmark", str(footage))
    service.scan(project["project_id"])
    asset = service.list_assets(project["project_id"], limit=5)["items"][0]

    # One cold sampling run, one warm-cache run. No separate scene scan.
    t0 = time.perf_counter()
    cold = service.pipeline.run(asset, Path(asset["path"]), profile=args.profile)
    cold_wall = time.perf_counter() - t0
    cold_manifest = service.pipeline.load_manifest(asset["asset_id"], cold.fingerprint) or {}
    t1 = time.perf_counter()
    warm = service.pipeline.run(asset, Path(asset["path"]), profile=args.profile)
    warm_wall = time.perf_counter() - t1
    asset = service.get_asset(asset["asset_id"])
    manifest = service.pipeline.load_manifest(asset["asset_id"], asset["sampling_fingerprint"])
    timings = cold_manifest.get("stage_timings", {})
    source = manifest["source"]
    timeline = manifest["timeline"]

    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    results: dict[str, dict] = {}
    for mode in modes:
        clear_inference(service, keep_frames=False)
        first: list[float] = []
        begin = time.perf_counter()
        swap_before = swap_used_mb()
        pressure_before = memory_state()

        def progress(payload: dict) -> None:
            if payload.get("stage") == "inferring" and payload.get("done", 0) > 0 and not first:
                first.append(time.perf_counter() - begin)

        try:
            allowed, reason = GOVERNOR.can_start_heavy_worker()
            if not allowed:
                raise RuntimeError(f"GOVERNOR_REFUSED:{reason}")
            outcome = service.vision.analyze(
                asset, Path(asset["path"]), selection_mode=mode, progress=progress
            )
        except Exception as exc:  # noqa: BLE001
            results[mode] = {
                "status": "failed", "error": f"{type(exc).__name__}: {exc}",
                "vision_wall_s": round(time.perf_counter() - begin, 3),
                "memory_pressure_before": pressure_before,
            }
            continue
        vision_wall = time.perf_counter() - begin
        evidence = service.vision.load_evidence(asset["asset_id"]) or {}
        results[mode] = {
            "status": outcome.status,
            "execution_status": "completed_selected_frames" if outcome.status in {"complete", "partial"} else outcome.status,
            "vision_wall_s": round(vision_wall, 3),
            "end_to_end_wall_s": round(cold_wall + vision_wall, 3),
            "cold_sampling_wall_s": round(cold_wall, 3),
            "warm_sampling_wall_s": round(warm_wall, 3),
            "time_to_first_observation_s": round(cold_wall + first[0], 3) if first else None,
            "frames_selected": outcome.selected_frames,
            "selected_frames": outcome.selected_frames,
            "sampling_frames": outcome.sampling_frames,
            "frames_observed": outcome.frames_observed,
            "frames_unknown": outcome.frames_unknown,
            "frame_reduction_pct": round(100 * (1 - outcome.selected_frames / outcome.sampling_frames), 1)
            if outcome.sampling_frames else 0.0,
            "model_load_count": outcome.worker_loads,
            "worker_loads": outcome.worker_loads,
            "model_load_s": round(outcome.model_load_s, 3),
            "generation_tokens": outcome.generation_tokens,
            "prompt_tokens": outcome.prompt_tokens,
            "json_validity_rate": round(outcome.frames_observed / max(1, outcome.frames_total), 3),
            "worker_peak_rss_mb": round(outcome.peak_worker_rss_bytes / 1048576, 1),
            "mlx_peak_memory_mb": round(outcome.mlx_peak_memory_bytes / 1048576, 1),
            "swap_delta_mb": round((swap_used_mb() or 0) - (swap_before or 0), 1),
            "memory_pressure_before": pressure_before,
            "memory_pressure_after": memory_state(),
            "warnings": outcome.warnings,
            "visual_coverage": evidence.get("visual_coverage"),
        }

    cache_probe = None
    if "smart" in results and results["smart"].get("status") in {"complete", "partial"}:
        clear_inference(service, keep_frames=True)
        begin = time.perf_counter()
        try:
            rerun = service.vision.analyze(asset, Path(asset["path"]), selection_mode="smart")
            cache_probe = {
                "vision_wall_s": round(time.perf_counter() - begin, 3),
                "model_load_count": rerun.worker_loads,
                "frame_cache_hits": rerun.frame_cache_hits,
                "cache_hit_rate": round(rerun.frame_cache_hits / max(1, rerun.selected_frames), 3),
                "inferences": rerun.inferences,
            }
        except Exception as exc:  # noqa: BLE001
            cache_probe = {"error": f"{type(exc).__name__}: {exc}"}

    summary = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "source_sha256": source["sha256"],
        "clip": {
            "duration_ms": timeline.get("duration_ms"),
            "width": timeline.get("width"), "height": timeline.get("height"),
            "codec": timeline.get("codec"), "rotation": timeline.get("rotation"),
        },
        "profile": args.profile,
        "settings": manifest.get("settings"),
        "cold_sampling_wall_s": round(cold_wall, 3),
        "warm_sampling_wall_s": round(warm_wall, 3),
        "sampling_cache_hits": warm.cache_hits,
        "sampling_stage_timings": timings,
        "sampling_frames": len(manifest["frames"]),
        "modes": results,
        "cache_probe": cache_probe,
        "governor_allowed": GOVERNOR.can_start_heavy_worker(),
        "memory_pressure": memory_state(),
        "notes": [
            "Cold end-to-end = cold sampling + vision; warm sampling is reported separately.",
            "No separate scene scan: all sampling stages come from one pipeline run.",
            "Per-frame cache is cleared before each cold mode; cache_probe keeps frames only.",
            "Important-event recall is not measured without human labels.",
            "Persistent worker and adaptive resolution/token matrix are not enabled.",
        ],
    }
    (OUT / "optimization.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "baseline.json").write_text(json.dumps(results.get("full"), ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "optimized.json").write_text(json.dumps({
        "balanced": results.get("balanced"), "smart": results.get("smart"), "cache_probe": cache_probe,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    service.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
