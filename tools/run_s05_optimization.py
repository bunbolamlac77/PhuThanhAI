#!/usr/bin/env python3
"""S05.4 baseline/optimized benchmark on real footage (never synthetic-only)."""

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

from phuthanhai.analyzer.probe import probe_source  # noqa: E402
from phuthanhai.analyzer.sampling import build_plan  # noqa: E402
from phuthanhai.analyzer.scene_scan import scan_scenes  # noqa: E402
from phuthanhai.library.service import LibraryService  # noqa: E402
from phuthanhai.utils.hashes import sha256_file  # noqa: E402

OUT = ROOT / "artifacts" / "qa" / "S05" / "optimization"


def swap_used_mb() -> float | None:
    try:
        text = subprocess.run(["sysctl", "-n", "vm.swapusage"], capture_output=True, text=True, timeout=5).stdout
        # used = 123.45M
        marker = text.split("used =")
        if len(marker) < 2:
            return None
        value = marker[1].strip().split()[0]
        number = float(value[:-1])
        return number * 1024 if value.endswith("G") else number
    except Exception:  # noqa: BLE001
        return None


def rss_mb(pid: int) -> float:
    try:
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True, timeout=5).stdout
        return round(int(out.strip()) / 1024, 1)
    except Exception:  # noqa: BLE001
        return 0.0


def clean_caches(service) -> None:
    inference = service.settings.data.inference
    if inference.exists():
        shutil.rmtree(inference)
    (inference / "frames").mkdir(parents=True, exist_ok=True)


def clear_job_caches(service) -> None:
    """Keep the per-frame cache, drop only per-job checkpoints/evidence."""
    inference = service.settings.data.inference
    for child in inference.iterdir() if inference.exists() else []:
        if child.name != "frames":
            shutil.rmtree(child) if child.is_dir() else child.unlink()


def run() -> int:
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
    service = LibraryService(load_settings())
    project = service.create_project("S05.4 benchmark", str(footage))
    service.scan(project["project_id"])
    asset = service.list_assets(project["project_id"], limit=5)["items"][0]

    profile: dict[str, float] = {}
    started = time.perf_counter()
    source_sha = sha256_file(Path(asset["path"]))
    profile["sha256_s"] = round(time.perf_counter() - started, 3)
    started = time.perf_counter()
    info = probe_source(Path(asset["path"]))
    profile["ffprobe_s"] = round(time.perf_counter() - started, 3)
    from phuthanhai.analyzer.timeline import read_timeline
    timeline = read_timeline(Path(asset["path"]))
    started = time.perf_counter()
    scan = scan_scenes(Path(asset["path"]), timeline)
    profile["scene_scan_s"] = round(time.perf_counter() - started, 3)
    started = time.perf_counter()
    plan = build_plan(timeline, scan.segments, args.profile, metrics=scan.frame_metrics)
    profile["sampling_plan_s"] = round(time.perf_counter() - started, 3)
    profile["planned_frames"] = len(plan.targets)
    started = time.perf_counter()
    service.pipeline.run(asset, Path(asset["path"]), profile=args.profile)
    profile["sampling_pipeline_total_s"] = round(time.perf_counter() - started, 3)
    profile["frame_extraction_derived_s"] = round(
        max(0.0, profile["sampling_pipeline_total_s"] - profile["sha256_s"] - profile["ffprobe_s"]
            - profile["scene_scan_s"] - profile["sampling_plan_s"]), 3)
    profile["source_sha"] = source_sha
    profile["scenes"] = len(scan.segments)
    asset = service.get_asset(asset["asset_id"])
    manifest = service.pipeline.load_manifest(asset["asset_id"], asset["sampling_fingerprint"])
    profile["sampling_frames"] = len(manifest["frames"])
    profile["sampling_coverage"] = manifest.get("coverage_status")
    profile["clip"] = f"{info.width}x{info.height} {info.codec} {info.duration_ms}ms"

    service.vision.require_governor = True
    modes = [mode.strip() for mode in args.modes.split(",") if mode.strip()]
    results: dict[str, dict] = {}
    for mode in modes:
        clean_caches(service)
        first_observation: list[float] = []
        begin = time.perf_counter()
        swap_before = swap_used_mb()

        def progress(payload: dict) -> None:
            if payload.get("stage") == "inferring" and payload.get("done", 0) > 0 and not first_observation:
                first_observation.append(time.perf_counter() - begin)

        try:
            outcome = service.vision.analyze(
                asset, Path(asset["path"]), selection_mode=mode, progress=progress
            )
        except Exception as exc:  # noqa: BLE001
            results[mode] = {"status": "failed", "error": f"{type(exc).__name__}: {exc}",
                             "elapsed_s": round(time.perf_counter() - begin, 3)}
            continue
        swap_after = swap_used_mb()
        evidence = service.vision.load_evidence(asset["asset_id"]) or {}
        results[mode] = {
            "status": outcome.status,
            "elapsed_s": round(time.perf_counter() - begin, 3),
            "time_to_first_validated_observation_s": round(first_observation[0], 3) if first_observation else None,
            "sampling_frames": outcome.sampling_frames,
            "selected_frames": outcome.selected_frames,
            "frame_reduction_pct": round(
                100 * (1 - outcome.selected_frames / outcome.sampling_frames), 1
            ) if outcome.sampling_frames else 0.0,
            "inferences": outcome.inferences,
            "worker_loads": outcome.worker_loads,
            "model_load_s": outcome.model_load_s,
            "avg_s_per_inferred_frame": round(outcome.elapsed_s / outcome.inferences, 2) if outcome.inferences else None,
            "json_validity_rate": 1.0 if outcome.selected_frames == outcome.frames_total else round(
                outcome.frames_total / max(1, outcome.selected_frames), 3),
            "frame_cache_hits": outcome.frame_cache_hits,
            "cache_hit_rate": round(outcome.frame_cache_hits / max(1, outcome.selected_frames), 3),
            "peak_worker_rss_mb": round(outcome.peak_worker_rss_bytes / (1024 * 1024), 1),
            "mlx_peak_mb": round(outcome.mlx_peak_memory_bytes / (1024 * 1024), 1),
            "swap_used_before_mb": swap_before,
            "swap_used_after_mb": swap_after,
            "swap_delta_mb": round((swap_after or 0) - (swap_before or 0), 1),
            "warnings": outcome.warnings,
            "evidence_observed": sum(1 for f in evidence.get("frames", [])
                                     if f.get("interpretation_level") == "observed"),
            "scenes_covered": len({f.get("scene_id") for f in evidence.get("frames", [])}),
            "visual_coverage": evidence.get("visual_coverage"),
            "observations": [
                {"frame_id": f.get("frame_id"), "pts_ms": f.get("normalized_pts_ms"),
                 "scene_id": f.get("scene_id"), "description": (f.get("observation") or {}).get("description")}
                for f in evidence.get("frames", [])
            ],
            "important_event_recall": "not measured: no human-labelled event set for this clip",
        }

    # Cache-hit rerun (Smart only), without clearing the per-frame cache.
    cache_probe = None
    if "smart" in results and results["smart"].get("status") in {"complete", "partial"}:
        clear_job_caches(service)
        begin = time.perf_counter()
        try:
            rerun = service.vision.analyze(asset, Path(asset["path"]), selection_mode="smart")
            cache_probe = {
                "elapsed_s": round(time.perf_counter() - begin, 3),
                "inferences": rerun.inferences,
                "frame_cache_hits": rerun.frame_cache_hits,
                "cache_hit_rate": round(rerun.frame_cache_hits / max(1, rerun.selected_frames), 3),
            }
        except Exception as exc:  # noqa: BLE001
            cache_probe = {"error": f"{type(exc).__name__}: {exc}"}

    baseline = results.get("full")
    optimized = results.get("smart")
    summary = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "clip": profile.pop("clip", None),
        "profile_seconds": profile,
        "baseline": baseline,
        "balanced": results.get("balanced"),
        "smart": optimized,
        "cache_probe": cache_probe,
        "notes": [
            "Full Evidence is the pre-optimization behavior (all verified sampling frames, worker per batch).",
            "Each mode ran after clearing the per-frame cache and job checkpoints.",
            "No human event labels for this clip: important-event recall is NOT VERIFIED, not PASS.",
            "Worker-per-batch remains the default; persistent worker was not enabled or benchmarked.",
            "Adaptive 448/640/896 resolution was not enabled or benchmarked.",
        ],
    }
    (OUT / "optimization.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "profile.json").write_text(json.dumps({"clip": summary["clip"], **profile}, indent=2), encoding="utf-8")
    (OUT / "baseline.json").write_text(json.dumps(baseline, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "optimized.json").write_text(json.dumps({
        "smart": optimized, "balanced": results.get("balanced"), "cache_probe": cache_probe,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    service.close()
    return 0


def load_settings():
    from phuthanhai.config import Settings
    return Settings.load()


if __name__ == "__main__":
    raise SystemExit(run())
