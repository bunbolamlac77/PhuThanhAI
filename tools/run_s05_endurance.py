#!/usr/bin/env python3
"""S05.1: real Qwen3-VL endurance + live-cancel on real footage.

No OOM is induced. If the governor refuses because of memory pressure, that is
recorded as protective behaviour and the run stops instead of forcing a pass.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "artifacts" / "qa" / "S05"
DATA = OUT / "endurance_data"
FOOTAGE = Path("/Users/phuthanh/Video/22.10 POV DamCuoi")
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

from phuthanhai.config import Settings  # noqa: E402
from phuthanhai.library.service import LibraryService  # noqa: E402
from phuthanhai.runtime.resource import GOVERNOR, snapshot  # noqa: E402
from phuthanhai.runtime.scheduler import HEAVY_SLOT, PROCESSES  # noqa: E402


def mem() -> dict:
    snap = snapshot()
    return {
        "level": snap.level,
        "available_bytes": snap.available_bytes,
        "swap_used_bytes": snap.swap_used_bytes,
        "swap_total_bytes": snap.swap_total_bytes,
        "pressure_free_pct": snap.pressure_free_pct,
    }


def wait_room(limit_s: float = 180) -> bool:
    deadline = time.monotonic() + limit_s
    while time.monotonic() < deadline:
        snap = snapshot()
        if snap.level != "critical" and (snap.available_bytes or 0) > 2 * 1024**3:
            return True
        time.sleep(5)
    return False


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    DATA.mkdir(parents=True, exist_ok=True)
    os.environ["PHUTHANHAI_DATA_DIR"] = str(DATA)
    os.environ["PHUTHANHAI_SANDBOX_WORKER"] = "1"
    service = LibraryService(Settings.load())
    result: dict = {"clips": [], "governor_refusals": [], "passed": False}
    try:
        project = service.create_project("S05 endurance", str(FOOTAGE))
        service.scan(project["project_id"])
        assets = service.list_assets(project["project_id"], sort="duration", limit=3)["items"]
        for asset in assets:
            if asset.get("sampling_status") != "complete":
                service.pipeline.run(asset, Path(asset["path"]), profile="quick", max_edge=640)
        for index, asset in enumerate(assets):
            asset = service.get_asset(asset["asset_id"])
            before = mem()
            allowed, reason = GOVERNOR.can_start_heavy_worker()
            if not allowed:
                result["governor_refusals"].append({"clip": asset["filename"], "code": reason, "memory": before})
                break
            if not wait_room():
                result["governor_refusals"].append({"clip": asset["filename"], "code": "WAIT_TIMEOUT", "memory": before})
                break
            started = time.monotonic()
            outcome = service.vision.analyze(asset, Path(asset["path"]), method="auto")
            elapsed = time.monotonic() - started
            after = mem()
            checkpoint = outcome.cache_dir / "analysis_checkpoint.json"
            entry = {
                "clip": asset["filename"],
                "frames": outcome.frames_total,
                "observed": outcome.frames_observed,
                "unknown": outcome.frames_unknown,
                "elapsed_s": round(elapsed, 2),
                "per_frame_s": round(elapsed / max(1, outcome.frames_total), 2),
                "worker_peak_rss_bytes": outcome.peak_worker_rss_bytes,
                "mlx_peak_memory_bytes": outcome.mlx_peak_memory_bytes,
                "memory_before": before,
                "memory_after": after,
                "swap_delta_bytes": (after["swap_used_bytes"] or 0) - (before["swap_used_bytes"] or 0),
                "checkpoint": checkpoint.is_file(),
                "evidence": service.vision.load_evidence(asset["asset_id"]) is not None,
                "heavy_slot_busy": HEAVY_SLOT.busy,
                "active_workers": PROCESSES.active_count,
            }
            entry["passed"] = (
                entry["evidence"] and entry["checkpoint"] and entry["unknown"] == 0
                and not entry["heavy_slot_busy"] and entry["active_workers"] == 0
            )
            result["clips"].append(entry)
            if not wait_room():
                result["governor_refusals"].append({"clip": "after:" + asset["filename"], "code": "WAIT_TIMEOUT", "memory": mem()})
                break

        # Live cancel on a fresh clip with one-frame batches so a checkpoint exists.
        fresh = service.list_assets(project["project_id"], sort="duration", limit=4)["items"][-1]
        if fresh.get("sampling_status") != "complete":
            service.pipeline.run(fresh, Path(fresh["path"]), profile="quick", max_edge=640)
        fresh = service.get_asset(fresh["asset_id"])
        service.vision.batch_size = 1
        job = service.start_analysis(fresh["asset_id"], "auto")
        job_id = job["job_id"]
        committed = 0
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            for checkpoint in (DATA / "caches/inference" / fresh["asset_id"]).rglob("analysis_checkpoint.json"):
                try:
                    data = json.loads(checkpoint.read_text(encoding="utf-8"))
                    committed = max(committed, len(data.get("results", {})))
                except ValueError:
                    pass
            if committed >= 1:
                break
            if service.get_job(job_id)["state"] not in {"queued", "running", "cancelling"}:
                break
            time.sleep(1)
        cancel_started = time.monotonic()
        service.cancel_job(job_id)
        while time.monotonic() < cancel_started + 60:
            state = service.get_job(job_id)
            if state["state"] not in {"queued", "running", "cancelling"}:
                break
            time.sleep(0.5)
        cancel_state = service.get_job(job_id)
        evidence_after_cancel = service.vision.load_evidence(fresh["asset_id"])
        cancel = {
            "clip": fresh["filename"],
            "committed_frames_at_cancel": committed,
            "job_state": cancel_state["state"],
            "cancel_seconds": round(time.monotonic() - cancel_started, 2),
            "active_workers": PROCESSES.active_count,
            "heavy_slot_busy": HEAVY_SLOT.busy,
            "evidence_present": evidence_after_cancel is not None,
        }
        if cancel_state["state"] == "cancelled":
            resume = service.start_analysis(fresh["asset_id"], "auto")
            resume_deadline = time.monotonic() + 180
            while time.monotonic() < resume_deadline:
                state = service.get_job(resume["job_id"])
                if state["state"] not in {"queued", "running", "cancelling"}:
                    break
                time.sleep(1)
            resumed = service.vision.load_evidence(fresh["asset_id"])
            ids = [frame["frame_id"] for frame in (resumed or {}).get("frames", [])]
            cancel["resume_state"] = service.get_job(resume["job_id"])["state"]
            cancel["resume_unique_ids"] = len(ids) == len(set(ids))
            cancel["resume_observed"] = sum(1 for f in (resumed or {}).get("frames", []) if f["interpretation_level"] == "observed")
        cancel["passed"] = (
            cancel["committed_frames_at_cancel"] >= 1
            and cancel["job_state"] == "cancelled"
            and cancel["active_workers"] == 0
            and not cancel["heavy_slot_busy"]
            and cancel["evidence_present"]
            and cancel.get("resume_state") == "complete"
            and cancel.get("resume_unique_ids") is True
        )
        result["cancel"] = cancel
        result["planned_clips"] = len(assets)
        result["completed_clips"] = len(result["clips"])
        result["deferred_by_governor"] = bool(result["governor_refusals"])
        result["passed"] = (
            len(result["clips"]) == len(assets)
            and all(item["passed"] for item in result["clips"])
            and cancel["passed"]
        )
        return 0 if result["passed"] else 1
    finally:
        service.close()
        (OUT / "endurance.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
        print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    raise SystemExit(main())
