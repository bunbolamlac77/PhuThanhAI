#!/usr/bin/env python3
"""S03 gate: P0 integrity, real Qwen3-VL inference, scheduler, recovery, offline.

Writes artifacts/qa/S03/: gate.md, gate.json, device_metrics.json,
vlm_benchmark.json, worker_recovery.json.

Usage:
    PHUTHANHAI_TEST_FOOTAGE=/path/to/footage python3 tools/run_s03_gate.py
"""

from __future__ import annotations

import json
import os
import re
import resource
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "artifacts" / "qa" / "S03"
VENV_PY = REPO / ".venv" / "bin" / "python"
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

from phuthanhai.analyzer.evidence import validate_evidence  # noqa: E402
from phuthanhai.analyzer.vision_service import VisionService  # noqa: E402
from phuthanhai.analyzer.vlm_worker import (  # noqa: E402
    PROMPT_VERSION,
    VISUAL_PROMPT,
    VISUAL_SEQUENCE_PROMPT,
)
from phuthanhai.config import Settings, hardware_facts, legacy_aivideo_root, model_specs  # noqa: E402
from phuthanhai.library.service import LibraryService  # noqa: E402
from phuthanhai.runtime.resource import snapshot  # noqa: E402
from phuthanhai.runtime.scheduler import PROCESSES  # noqa: E402
from phuthanhai.utils.hashes import sha256_file  # noqa: E402
from tests.media_fixtures import get as fixture  # noqa: E402


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def step(name: str, status: str, detail: str, evidence: dict | None = None) -> dict:
    return {"step": name, "status": status, "detail": detail, "evidence": evidence or {}}


def dir_size(path: Path) -> int:
    if not path.is_dir():
        return 0
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def pytest_counts(junit: Path) -> dict:
    passed = failed = skipped = 0
    if junit.is_file():
        root = ET.parse(junit).getroot()
        suites = [root] if root.tag == "testsuite" else list(root)
        for suite in suites:
            passed += int(suite.get("tests", 0)) - int(suite.get("failures", 0)) - int(suite.get("errors", 0)) - int(suite.get("skipped", 0))
            failed += int(suite.get("failures", 0)) + int(suite.get("errors", 0))
            skipped += int(suite.get("skipped", 0))
    return {"passed": passed, "failed": failed, "skipped": skipped}


def run_pytest(label: str, selection: list[str], junit_name: str, timeout: int = 1800) -> dict:
    junit = OUT / junit_name
    argv = [str(VENV_PY), "-m", "pytest", *selection, f"--junitxml={junit}", "-o", "junit_family=xunit2"]
    completed = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False, cwd=str(REPO))
    (OUT / junit_name.replace(".xml", ".log")).write_text(completed.stdout + completed.stderr, encoding="utf-8")
    counts = pytest_counts(junit)
    return step(
        label,
        "PASS" if completed.returncode == 0 and counts["passed"] > 0 else "FAIL",
        f"{counts['passed']} passed, {counts['failed']} failed, {counts['skipped']} skipped",
        counts,
    )


def runtime_path() -> Path:
    from phuthanhai.config import mlx_runtime_candidates

    for candidate in mlx_runtime_candidates():
        if candidate.exists():
            return candidate.python
    raise RuntimeError("no MLX runtime")


def model_path() -> Path:
    for spec in model_specs():
        if spec.role == "visual_analysis_primary":
            path = legacy_aivideo_root() / "Models" / spec.relative_path
            if path.is_dir():
                return path
    raise RuntimeError("no model snapshot")


def memory_snapshot() -> dict:
    snap = snapshot()
    return {
        "level": snap.level,
        "reasons": list(snap.reasons),
        "available_bytes": snap.available_bytes,
        "swap_used_bytes": snap.swap_used_bytes,
        "swap_total_bytes": snap.swap_total_bytes,
        "pressure_free_pct": snap.pressure_free_pct,
        "disk_free_bytes": snap.disk_free_bytes,
    }


def wait_for_memory(timeout: float = 120) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if snapshot().level != "critical":
            return
        time.sleep(5)


def real_inference(data_root: Path, footage: Path) -> tuple[dict, dict]:
    settings = Settings.load()
    service = LibraryService(settings)
    try:
        project = service.create_project("S03 real", str(footage))
        service.scan(project["project_id"])
        asset = service.list_assets(project["project_id"], sort="duration", limit=1)["items"][0]
        sampling = service.pipeline.run(asset, Path(asset["path"]), profile="quick", max_edge=640)
        asset = service.get_asset(asset["asset_id"])
        before = memory_snapshot()
        state: dict = {}

        def run() -> None:
            try:
                state["outcome"] = service.vision.analyze(asset, Path(asset["path"]), method="auto")
            except Exception as exc:  # noqa: BLE001
                state["error"] = f"{type(exc).__name__}: {exc}"

        thread = threading.Thread(target=run)
        thread.start()
        time.sleep(3.0)
        note = service.create_annotation(
            project["project_id"],
            {"scope": "clip", "asset_id": asset["asset_id"], "text": "Ghi chú trong lúc AI chạy (gate)"},
        )
        notes_during = bool(note.get("annotation", {}).get("annotation_id"))
        thread.join(timeout=1200)
        if "error" in state:
            raise RuntimeError(state["error"])
        outcome = state["outcome"]
        after = memory_snapshot()
        manifest = service.pipeline.load_manifest(asset["asset_id"], asset["sampling_fingerprint"])
        evidence = service.vision.load_evidence(asset["asset_id"])
        validate_evidence(evidence, manifest["frames"])
        observed = [f for f in evidence["frames"] if f["interpretation_level"] == "observed"]

        qa = {
            "asset": asset["filename"],
            "sampling_frames": len(manifest["frames"]),
            "coverage_status": evidence["coverage_status"],
            "method": evidence["analysis_method"],
            "prompt_version": evidence["prompt_version"],
            "model_identity": evidence["model_identity"],
            "runtime_versions": evidence["runtime_versions"],
            "observations": [
                {
                    "frame_id": frame["frame_id"],
                    "scene_id": frame["scene_id"],
                    "normalized_pts_ms": frame["normalized_pts_ms"],
                    "description": frame["observation"]["description"],
                    "uncertainty": frame["observation"]["uncertainty"],
                    "image": frame["frame_id"],
                }
                for frame in observed
            ],
            "sampling_warnings": manifest.get("warnings", []),
            "notes_during_inference": notes_during,
        }
        if observed:
            source_image = next(
                frame["path"] for frame in manifest["frames"] if frame["frame_id"] == observed[0]["frame_id"]
            )
            shutil.copy(source_image, OUT / "qa_frame.jpg")
        (OUT / "qa_observation.json").write_text(json.dumps(qa, indent=2, ensure_ascii=False), encoding="utf-8")

        evidence_copy = {
            "analysis_id": evidence["analysis_id"],
            "coverage_status": evidence["coverage_status"],
            "frames": [
                {
                    "frame_id": f["frame_id"],
                    "scene_id": f["scene_id"],
                    "normalized_pts_ms": f["normalized_pts_ms"],
                    "interpretation_level": f["interpretation_level"],
                }
                for f in evidence["frames"]
            ],
            "warnings": evidence["warnings"],
        }
        detail = {
            "outcome": outcome.to_dict(),
            "before": before,
            "after": after,
            "swap_delta_bytes": (after["swap_used_bytes"] or 0) - (before["swap_used_bytes"] or 0),
            "notes_during_inference": notes_during,
            "evidence": evidence_copy,
            "sampling": {
                "frames": len(manifest["frames"]),
                "coverage_status": sampling.coverage_status,
                "observed_max_gap_ms": sampling.observed_max_gap_ms,
                "source_sha256": manifest["source"]["sha256"],
            },
        }
        return detail, qa
    finally:
        service.close()


def benchmark(data_root: Path, qa: dict, footage: Path) -> dict:
    """1/2/4-frame timing and memory; multi-image validity check."""
    from phuthanhai.library.service import LibraryService as Service
    from phuthanhai.runtime.scheduler import ProcessRegistry

    settings = Settings.load()
    service = Service(settings)
    record: dict = {"cases": [], "multi_frame_supported": False}
    try:
        project = service.create_project("S03 bench", str(footage))
        service.scan(project["project_id"])
        asset = service.list_assets(project["project_id"], sort="duration", limit=1)["items"][0]
        sampling = service.pipeline.run(asset, Path(asset["path"]), profile="quick", max_edge=640)
        frames = sampling.frames
        model = model_path()
        python = runtime_path()
        registry = ProcessRegistry()
        env = {
            **os.environ,
            "PYTHONPATH": f"{REPO / 'src'}:{REPO}",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "TOKENIZERS_PARALLELISM": "false",
        }
        sandbox = ["/usr/bin/sandbox-exec", "-p", "(version 1)(allow default)(deny network*)"]

        def request(method: str, count: int) -> dict:
            selected = frames[:count]
            return {
                "request_version": "2.0.0",
                "job_id": f"bench-{method}-{count}",
                "asset_id": asset["asset_id"],
                "analysis_id": "22222222-2222-4222-8222-222222222222",
                "model_path": str(model),
                "allowed_image_root": str(settings.data.sampling),
                "prompt": VISUAL_SEQUENCE_PROMPT if method == "multi_frame" else VISUAL_PROMPT,
                "prompt_version": PROMPT_VERSION,
                "method": method,
                "frames": [
                    {
                        "frame_id": frame["frame_id"],
                        "scene_id": frame["scene_id"],
                        "image_path": frame["path"],
                        "normalized_pts_ms": frame["normalized_pts_ms"],
                        "sha256": sha256_file(Path(frame["path"])),
                    }
                    for frame in selected
                ],
                "max_tokens": 320,
                "temperature": 0.0,
                "seed": 0,
            }

        for method, count in (
            ("single_frame", 1),
            ("sequential_frames", 2),
            ("sequential_frames", 4),
            ("multi_frame", 2),
        ):
            before = memory_snapshot()
            started = time.monotonic()
            try:
                captured = registry.run_captured(
                    sandbox + [str(python), "-m", "phuthanhai.analyzer.vlm_worker"],
                    env=env,
                    input_bytes=json.dumps(request(method, count)).encode(),
                    timeout_s=900,
                )
                events = [json.loads(line) for line in captured.stdout.splitlines() if line.strip()]
                final = next((e for e in events if e.get("type") == "result"), None)
                error = next((e for e in events if e.get("type") == "error"), None)
                valid = False
                if final:
                    try:
                        from phuthanhai.analyzer.vlm_worker import validate_observation

                        first = final["frames"][0]["observation"] if method != "multi_frame" else final["sequence"]["observation"]
                        validate_observation(first)
                        valid = True
                    except Exception:  # noqa: BLE001
                        valid = False
                after = memory_snapshot()
                duration = time.monotonic() - started
                record["cases"].append(
                    {
                        "method": method,
                        "frames_sent": count,
                        "exit_code": captured.returncode,
                        "elapsed_s": round(duration, 3),
                        "per_frame_s": round(duration / count, 3),
                        "process_tree_peak_rss_bytes": captured.peak_rss_bytes,
                        "worker_peak_rss_bytes": (final or {}).get("peak_rss_bytes"),
                        "mlx_peak_memory_bytes": (final or {}).get("mlx_peak_memory_bytes"),
                        "output_valid": valid,
                        "error": (error or {}).get("code"),
                        "error_message": ((error or {}).get("message") or "")[:300],
                        "swap_delta_bytes": (after["swap_used_bytes"] or 0) - (before["swap_used_bytes"] or 0),
                        "memory_level_after": after["level"],
                    }
                )
                if method == "multi_frame" and valid:
                    record["multi_frame_supported"] = True
            except Exception as exc:  # noqa: BLE001
                record["cases"].append(
                    {"method": method, "frames_sent": count, "error": f"{type(exc).__name__}: {exc}"}
                )
            wait_for_memory()
        record["decision"] = (
            "multi_frame"
            if record["multi_frame_supported"]
            else "sequential_frames (multi-image output failed strict JSON validation)"
        )
        return record
    finally:
        service.close()


def scheduler_and_recovery(data_root: Path) -> dict:
    """Fake worker: concurrency must be 1, crash/cancel must be reaped and resumable."""
    from tests.fake_vlm_worker import main as _unused  # noqa: F401  (prove module exists)

    settings = Settings.load()
    service = LibraryService(settings)
    result: dict = {}
    try:
        footage = data_root / "recovery_footage"
        footage.mkdir(parents=True, exist_ok=True)
        for name in ("cfr", "rotation"):
            shutil.copy(fixture(name), footage / f"{name}.mp4")
        project = service.create_project("S03 recovery", str(footage))
        service.scan(project["project_id"])
        items = service.list_assets(project["project_id"], limit=10)["items"]
        for asset in items:
            service.pipeline.run(asset, Path(asset["path"]), profile="quick")
        model = data_root / "fake_model"
        model.mkdir(exist_ok=True)
        (model / "config.json").write_text("{}", encoding="utf-8")
        (model / "model.safetensors").write_bytes(b"stub")
        log_path = OUT / "concurrency.log"
        log_path.unlink(missing_ok=True)
        os.environ["FAKE_VLM_LOG"] = str(log_path)
        vision = VisionService(
            service.db,
            settings,
            runtime_python=Path(sys.executable),
            worker_module="tests.fake_vlm_worker",
            model_path=model,
            request_timeout_s=10,
            batch_size=1,
        )
        os.environ["FAKE_VLM_MODE"] = "slow"
        outcomes: dict[str, dict] = {}

        def analyze(index: int, asset: dict) -> None:
            try:
                outcomes[asset["asset_id"]] = vision.analyze(
                    service.get_asset(asset["asset_id"]), Path(asset["path"])
                ).to_dict()
            except Exception as exc:  # noqa: BLE001
                outcomes[asset["asset_id"]] = {"error": f"{type(exc).__name__}: {exc}"}

        threads = [
            threading.Thread(target=analyze, args=(index, asset))
            for index, asset in enumerate(items[:2])
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)

        starts: list[float] = []
        ends: list[float] = []
        for line in log_path.read_text(encoding="utf-8").splitlines():
            kind, _pid, stamp = line.split()
            (starts if kind == "start" else ends).append(float(stamp))
        starts.sort()
        ends.sort()
        max_concurrent = 0
        for stamp in starts:
            active = sum(1 for start in starts if start <= stamp) - sum(1 for end in ends if end <= stamp)
            max_concurrent = max(max_concurrent, active)
        result["scheduler"] = {
            "max_concurrent_workers": max_concurrent,
            "both_completed": all(item.get("status") == "complete" for item in outcomes.values()),
            "leftover_processes": PROCESSES.active_count,
        }

        # Crash after one committed batch, then resume. Clear the inference cache
        # first so the scheduler phase cannot mask recovery with a finished checkpoint.
        cache_root = Settings.load().data.inference
        shutil.rmtree(cache_root, ignore_errors=True)
        cache_root.mkdir(parents=True, exist_ok=True)
        target = items[0]
        state_file = OUT / "crash_state.marker"
        state_file.unlink(missing_ok=True)
        os.environ["FAKE_VLM_STATE"] = str(state_file)
        os.environ["FAKE_VLM_MODE"] = "crash_after_batch"
        first = vision.analyze(service.get_asset(target["asset_id"]), Path(target["path"]))
        os.environ["FAKE_VLM_MODE"] = "ok"
        second = vision.analyze(service.get_asset(target["asset_id"]), Path(target["path"]))
        evidence = vision.load_evidence(target["asset_id"])
        ids = [frame["frame_id"] for frame in evidence["frames"]]
        result["recovery"] = {
            "first_status": first.status,
            "committed_after_crash": first.frames_observed,
            "crash_was_partial": first.status == "partial" and first.frames_observed > 0,
            "resumed_status": second.status,
            "final_frames": len(ids),
            "unique_ids": len(ids) == len(set(ids)),
            "all_observed": second.frames_unknown == 0,
            "leftover_processes": PROCESSES.active_count,
        }

        # Cancel: clear inference cache so a slow worker really has work to stop.
        shutil.rmtree(cache_root, ignore_errors=True)
        cache_root.mkdir(parents=True, exist_ok=True)
        cancel = threading.Event()
        os.environ["FAKE_VLM_MODE"] = "slow"
        state: dict = {}

        def run_cancel() -> None:
            state["outcome"] = vision.analyze(
                service.get_asset(items[1]["asset_id"]), Path(items[1]["path"]), cancel=cancel
            )

        thread = threading.Thread(target=run_cancel)
        thread.start()
        time.sleep(0.5)
        cancel.set()
        thread.join(timeout=60)
        result["cancel"] = {
            "status": state["outcome"].status,
            "leftover_processes": PROCESSES.active_count,
            "elapsed_s": round(state["outcome"].elapsed_s, 2),
        }
        result["passed"] = (
            max_concurrent <= 1
            and result["scheduler"]["both_completed"]
            and result["scheduler"]["leftover_processes"] == 0
            and result["recovery"]["unique_ids"]
            and result["recovery"]["all_observed"]
            and result["recovery"]["crash_was_partial"]
            and result["cancel"]["leftover_processes"] == 0
        )
        return result
    finally:
        service.close()


def orphans() -> list[str]:
    ps = subprocess.run(["/bin/ps", "-axo", "pid=,command="], capture_output=True, text=True, check=False)
    return [
        line.strip()
        for line in ps.stdout.splitlines()
        if re.search(r"(phuthanhai\.analyzer\.vlm_worker|tests\.fake_vlm_worker|phuthanhai\.cli serve)", line)
    ]


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    data_root = Path(tempfile.mkdtemp(prefix="pth_g3_"))
    os.environ["PHUTHANHAI_DATA_DIR"] = str(data_root / "data")
    os.environ["PHUTHANHAI_SANDBOX_WORKER"] = "1"
    footage_env = os.environ.get("PHUTHANHAI_TEST_FOOTAGE")
    footage = Path(footage_env).expanduser() if footage_env else None
    steps: list[dict] = []
    started = time.monotonic()

    steps.append(run_pytest("pytest (S01+S02+S03 regression)", [], "pytest.xml"))
    steps.append(run_pytest("P0 integrity regression", ["tests/test_s03_p0.py"], "p0.xml"))
    steps.append(run_pytest("worker protocol + error paths", ["tests/test_s03_worker_v2.py", "tests/test_s03_vision.py", "tests/test_s03_api.py"], "worker.xml"))

    if footage is None or not footage.is_dir():
        steps.append(step("real Qwen3-VL inference", "SKIP", "PHUTHANHAI_TEST_FOOTAGE not set"))
        real = {}
        qa = {}
        bench = {}
    else:
        try:
            real, qa = real_inference(data_root, footage)
            observed = sum(1 for frame in real["evidence"]["frames"] if frame["interpretation_level"] == "observed")
            steps.append(
                step(
                    "real Qwen3-VL offline inference + evidence",
                    "PASS" if observed > 0 and real["outcome"]["frames_unknown"] == 0 else "FAIL",
                    f"{observed}/{len(real['evidence']['frames'])} observed · method={real['outcome']['method']} · "
                    f"{real['outcome']['elapsed_s']}s · notes_during={real['notes_during_inference']}",
                    real,
                )
            )
            bench = benchmark(data_root, qa, footage)
            (OUT / "vlm_benchmark.json").write_text(json.dumps(bench, indent=2, ensure_ascii=False), encoding="utf-8")
            valid_cases = [case for case in bench["cases"] if case.get("output_valid")]
            steps.append(
                step(
                    "benchmark 1/2/4 frames + multi-frame validity",
                    "PASS" if bench.get("multi_frame_supported") or valid_cases else "FAIL",
                    " · ".join(
                        f"{case['method']} n={case['frames_sent']}: {case.get('elapsed_s')}s "
                        f"{'valid' if case.get('output_valid') else 'invalid'}"
                        for case in bench["cases"]
                    )
                    + f" → {bench['decision']}",
                    bench,
                )
            )
        except Exception as exc:  # noqa: BLE001
            steps.append(step("real Qwen3-VL offline inference", "FAIL", f"{type(exc).__name__}: {exc}"))
            real, qa, bench = {}, {}, {}

    recovery = scheduler_and_recovery(data_root)
    (OUT / "worker_recovery.json").write_text(json.dumps(recovery, indent=2), encoding="utf-8")
    steps.append(
        step(
            "scheduler single heavy slot",
            "PASS" if recovery["scheduler"]["max_concurrent_workers"] <= 1 and recovery["scheduler"]["both_completed"] else "FAIL",
            f"max_concurrent={recovery['scheduler']['max_concurrent_workers']} "
            f"completed={recovery['scheduler']['both_completed']}",
            recovery["scheduler"],
        )
    )
    steps.append(
        step(
            "worker crash/resume + cancel/reap",
            "PASS" if recovery["passed"] else "FAIL",
            f"crash committed={recovery['recovery']['committed_after_crash']} → final={recovery['recovery']['final_frames']} "
            f"unique={recovery['recovery']['unique_ids']} · cancel={recovery['cancel']['status']} "
            f"leftovers={recovery['cancel']['leftover_processes']}",
            recovery,
        )
    )

    leftovers = orphans()
    steps.append(step("orphans", "PASS" if not leftovers else "FAIL", f"leftovers={len(leftovers)}"))

    # UI regression: notes/playback/shortcuts still work.
    env = {**os.environ}
    if footage is not None:
        env["PHUTHANHAI_TEST_FOOTAGE"] = str(footage)
    ui = subprocess.run(
        [str(VENV_PY), str(REPO / "tools" / "run_s01_ui_flow.py")],
        capture_output=True, text=True, timeout=900, check=False, cwd=str(REPO), env=env,
    )
    (OUT / "ui_flow.log").write_text(ui.stdout + ui.stderr, encoding="utf-8")
    flow_path = REPO / "artifacts" / "qa" / "S01" / "e2e" / "ui_flow.json"
    flow = json.loads(flow_path.read_text(encoding="utf-8")) if flow_path.is_file() else {}
    steps.append(
        step(
            "UI regression while AI service is available",
            "PASS" if ui.returncode == 0 and flow.get("passed") else "FAIL",
            f"checks={flow.get('checks')} shortcuts={flow.get('shortcut_checks')}",
            flow,
        )
    )

    resources = snapshot()
    usage = resource.getrusage(resource.RUSAGE_SELF)
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    metrics = {
        "generated_at": now_iso(),
        "hardware": hardware_facts(),
        "resources": resources.to_dict(),
        "gate_elapsed_s": round(time.monotonic() - started, 2),
        "cpu_seconds_total": round(usage.ru_utime + usage.ru_stime + children.ru_utime + children.ru_stime, 2),
        "model_identity": qa.get("model_identity"),
        "runtime_versions": qa.get("runtime_versions"),
        "single_frame_benchmark": next(
            (case for case in bench.get("cases", []) if case["method"] == "single_frame"), None
        ),
        "sequential_4_benchmark": real.get("outcome"),
        "multi_frame_supported": bench.get("multi_frame_supported"),
        "memory_before": real.get("before"),
        "memory_after": real.get("after"),
        "swap_delta_bytes": real.get("swap_delta_bytes"),
        "inference_cache_bytes": dir_size(settings_inference()),
        "sampling_cache_bytes": dir_size(settings_sampling()),
        "human_qa": "pending_owner_confirmation" if qa else "not_run",
    }
    (OUT / "device_metrics.json").write_text(json.dumps(metrics, indent=2, ensure_ascii=False), encoding="utf-8")
    steps.append(
        step(
            "device metrics on M1 Pro 16 GB",
            "PASS" if real else "SKIP",
            f"swap delta {round((real.get('swap_delta_bytes') or 0)/1024**3, 2)} GiB · "
            f"worker RSS {round((real.get('outcome', {}).get('peak_worker_rss_bytes') or 0)/1024**3, 2)} GiB · "
            f"MLX peak {round((real.get('outcome', {}).get('mlx_peak_memory_bytes') or 0)/1024**3, 2)} GiB",
            metrics,
        )
    )

    verdict = "FAIL" if any(item["status"] == "FAIL" for item in steps) else (
        "PASS_WITH_WARNINGS" if any(item["status"] == "SKIP" for item in steps) else "PASS"
    )
    if verdict == "PASS" and metrics["human_qa"] == "pending_owner_confirmation":
        # Human content QA is a G3 requirement; do not claim PASS before the owner
        # confirms the real-frame observation.
        verdict = "PASS_WITH_WARNINGS"
    lines = [
        "# S03 — Qwen3-VL, ModelScheduler, ResourceGovernor, Visual Evidence — Gate G3",
        "",
        f"- Generated: {now_iso()}",
        "- Human content QA: **pending owner confirmation** (frame + description saved for review).",
        "",
        "| # | Requirement | Result | Detail |",
        "|---|---|---|---|",
    ]
    for index, item in enumerate(steps, start=1):
        lines.append(f"| {index} | {item['step']} | **{item['status']}** | {item['detail']} |")
    lines += ["", f"## Verdict: {verdict}", ""]
    (OUT / "gate.json").write_text(
        json.dumps(
            {"milestone": "S03", "generated_at": now_iso(), "verdict": verdict, "steps": steps, "metrics": metrics},
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (OUT / "gate.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    shutil.rmtree(data_root, ignore_errors=True)
    return 0 if verdict != "FAIL" else 1


def settings_inference() -> Path:
    return Settings.load().data.inference


def settings_sampling() -> Path:
    return Settings.load().data.sampling


if __name__ == "__main__":
    sys.exit(main())
