#!/usr/bin/env python3
"""S02 gate evidence collector.

Runs the S02 pipeline across the fixture matrix and (optionally) real footage,
re-checks cache/resume behaviour, collects CPU/RAM/cache metrics and runs the S01
browser E2E as regression. Writes ``artifacts/qa/S02/``.

Usage:
    PHUTHANHAI_TEST_FOOTAGE=/path/to/footage python3 tools/run_s02_gate.py
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
import time
import xml.etree.ElementTree as ET
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "artifacts" / "qa" / "S02"
VENV_PY = REPO / ".venv" / "bin" / "python"
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "tools"))
sys.path.insert(0, str(REPO))

from phuthanhai.analyzer.scene_scan import scan_scenes  # noqa: E402
from phuthanhai.analyzer.timeline import read_timeline  # noqa: E402
from phuthanhai.config import Settings, hardware_facts  # noqa: E402
from phuthanhai.library.service import LibraryService  # noqa: E402
from phuthanhai.runtime.resource import snapshot  # noqa: E402
from tests.media_fixtures import get as fixture  # noqa: E402


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def step(name: str, status: str, detail: str, evidence: dict | None = None) -> dict:
    return {"step": name, "status": status, "detail": detail, "evidence": evidence or {}}


def dir_size(path: Path) -> int:
    total = 0
    if path.is_dir():
        for item in path.rglob("*"):
            if item.is_file():
                total += item.stat().st_size
    return total


def peak_rss_bytes() -> int:
    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)


def run_tool(argv: list[str], log: Path, timeout: float = 900, env: dict | None = None) -> dict:
    completed = subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout, check=False, cwd=str(REPO),
        env={**os.environ, **(env or {})},
    )
    log.write_text(completed.stdout + completed.stderr, encoding="utf-8")
    return {"exit_code": completed.returncode, "stdout_tail": completed.stdout[-2000:]}


def step_pytest() -> dict:
    junit = OUT / "pytest.xml"
    completed = subprocess.run(
        [str(VENV_PY), "-m", "pytest", f"--junitxml={junit}", "-o", "junit_family=xunit2", "-q"],
        capture_output=True, text=True, timeout=1800, check=False, cwd=str(REPO),
    )
    (OUT / "pytest.log").write_text(completed.stdout + completed.stderr, encoding="utf-8")
    passed = failed = skipped = 0
    if junit.is_file():
        root = ET.parse(junit).getroot()
        suites = [root] if root.tag == "testsuite" else list(root)
        for suite in suites:
            passed += int(suite.get("tests", 0)) - int(suite.get("failures", 0)) - int(suite.get("errors", 0)) - int(suite.get("skipped", 0))
            failed += int(suite.get("failures", 0)) + int(suite.get("errors", 0))
            skipped += int(suite.get("skipped", 0))
    payload = {"exit_code": completed.returncode, "passed": passed, "failed": failed, "skipped": skipped}
    (OUT / "tests.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return step(
        "pytest (S01 regression + S02)",
        "PASS" if completed.returncode == 0 and passed > 0 else "FAIL",
        f"{passed} passed, {failed} failed, {skipped} skipped",
        payload,
    )


FIXTURE_EXPECTATIONS = {
    "cfr": {"vfr": False, "codec": "h264"},
    "vfr": {"vfr": True},
    "nonzero": {"first_pts_gt": 1000},
    "bframes": {},
    "rotation": {"rotation": 90, "portrait": True},
    "hdr": {"hdr": True},
    "cuts": {"cuts": 3},
    "rapid": {"cuts_min": 5},
    "long_nocut": {"cuts": 0, "segments_min": 2},
    "dark": {"dark_ratio_min": 0.9},
    "blurred": {"blur_ratio_min": 0.9},
    "motion_short": {"motion_min": 8.0},
    "prores": {"codec": "prores"},
}


def check_fixture(name: str, result: dict) -> list[str]:
    problems: list[str] = []
    expected = FIXTURE_EXPECTATIONS.get(name, {})
    if "vfr" in expected and result["timeline"]["vfr_detected"] != expected["vfr"]:
        problems.append(f"vfr={result['timeline']['vfr_detected']}")
    if "codec" in expected and result["timeline"]["codec"] != expected["codec"]:
        problems.append(f"codec={result['timeline']['codec']}")
    if "first_pts_gt" in expected and result["timeline"]["first_video_pts_ms"] <= expected["first_pts_gt"]:
        problems.append(f"first_pts={result['timeline']['first_video_pts_ms']}")
    if "rotation" in expected and result["timeline"]["rotation"] != expected["rotation"]:
        problems.append(f"rotation={result['timeline']['rotation']}")
    if expected.get("portrait") and result["timeline"]["display_height"] <= result["timeline"]["display_width"]:
        problems.append("display not portrait")
    if "hdr" in expected and result["timeline"]["is_hdr"] != expected["hdr"]:
        problems.append("hdr flag")
    if "cuts" in expected and result["scan"]["cuts"] != expected["cuts"]:
        problems.append(f"cuts={result['scan']['cuts']}")
    if "cuts_min" in expected and result["scan"]["cuts"] < expected["cuts_min"]:
        problems.append(f"cuts={result['scan']['cuts']}")
    if "segments_min" in expected and result["scan"]["segments"] < expected["segments_min"]:
        problems.append("segments too few")
    if expected.get("dark_ratio_min") and result["scan"]["dark_ratio"] < expected["dark_ratio_min"]:
        problems.append("dark ratio")
    if expected.get("blur_ratio_min") and result["scan"]["blur_ratio"] < expected["blur_ratio_min"]:
        problems.append("blur ratio")
    if expected.get("motion_min") and result["scan"]["max_motion"] < expected["motion_min"]:
        problems.append("motion cue")
    if result["frames"] == 0:
        problems.append("no frames")
    if result["pts_mismatch"]:
        problems.append("pts mismatch")
    if result["observed_gap_ms"] > result["target_gap_ms"]:
        problems.append("observed gap over target")
    return problems


def fixture_matrix(data_root: Path) -> dict:
    settings = Settings.load()
    service = LibraryService(settings)
    footage = data_root / "fixtures"
    footage.mkdir(parents=True, exist_ok=True)
    results: dict[str, dict] = {}
    try:
        names = list(FIXTURE_EXPECTATIONS)
        for name in names:
            shutil.copy(fixture(name), footage / f"{name}{Path(fixture(name)).suffix}")
        project = service.create_project("S02 fixtures", str(footage))
        service.scan(project["project_id"])
        assets = {
            item["filename"].rsplit(".", 1)[0]: item
            for item in service.list_assets(project["project_id"], limit=100)["items"]
        }
        for name in names:
            asset = assets[name]
            started = time.monotonic()
            outcome = service.pipeline.run(asset, Path(asset["path"]), profile="quick")
            elapsed = time.monotonic() - started
            self_scan = scan_scenes(Path(asset["path"]), outcome.timeline)
            result = {
                "timeline": outcome.timeline.to_dict(),
                "scan": {
                    "cuts": len(self_scan.cut_times_ms),
                    "segments": len(outcome.segments),
                    "max_motion": max((s.max_motion for s in outcome.segments), default=0.0),
                    "dark_ratio": outcome.segments[0].dark_ratio if outcome.segments else 0.0,
                    "blur_ratio": outcome.segments[0].blur_ratio if outcome.segments else 0.0,
                    "cut_times": self_scan.cut_times_ms[:10],
                },
                "frames": len(outcome.frames),
                "extracted": outcome.extracted,
                "cache_hits": outcome.cache_hits,
                "observed_gap_ms": outcome.observed_max_gap_ms,
                "target_gap_ms": outcome.plan.max_gap_ms,
                "pts_mismatch": any(bool(frame.get("pts_mismatch")) for frame in outcome.frames),
                "max_abs_offset_ms": max((abs(int(frame.get("offset_ms", 0))) for frame in outcome.frames), default=0),
                "elapsed_s": round(elapsed, 3),
            }
            result["problems"] = check_fixture(name, result)
            results[name] = result
        # Resume check on one fixture: a second run must not re-extract.
        asset = assets["cuts"]
        again = service.pipeline.run(asset, Path(asset["path"]), profile="quick")
        results["_resume"] = {
            "extracted": again.extracted,
            "cache_hits": again.cache_hits,
            "frames": len(again.frames),
        }
        cache_bytes = dir_size(settings.data.sampling)
    finally:
        service.close()
    return {"fixtures": results, "cache_bytes": cache_bytes}


def cache_invalidation_check(data_root: Path) -> dict:
    """Changing source bytes must create a new fingerprint and re-extract; notes stay."""

    settings = Settings.load()
    service = LibraryService(settings)
    footage = data_root / "mutate_footage"
    footage.mkdir(parents=True, exist_ok=True)
    target = footage / "clip.mp4"
    shutil.copy(fixture("cfr"), target)
    try:
        project = service.create_project("mutation", str(footage))
        service.scan(project["project_id"])
        asset = next(
            item for item in service.list_assets(project["project_id"], limit=10)["items"]
        )
        first = service.pipeline.run(asset, target, profile="quick")
        note = service.create_annotation(
            project["project_id"],
            {"scope": "clip", "asset_id": asset["asset_id"], "text": "Ghi chú phải còn sau khi nguồn đổi"},
        )
        shutil.copy(fixture("dark"), target)
        second = service.pipeline.run(asset, target, profile="quick")
        notes = service.list_annotations(project["project_id"])
        asset_root = settings.data.sampling / asset["asset_id"]
        dirs = [child for child in asset_root.iterdir() if child.is_dir()]
        return {
            "fingerprint_changed": first.fingerprint != second.fingerprint,
            "first_frames": len(first.frames),
            "second_frames": len(second.frames),
            "second_extracted": second.extracted,
            "cache_dir_count": len(dirs),
            "note_preserved": len(notes) == 1 and notes[0]["annotation_id"] == note["annotation"]["annotation_id"],
            "passed": (
                first.fingerprint != second.fingerprint
                and second.extracted == len(second.frames)
                and len(dirs) == 1
                and len(notes) == 1
            ),
        }
    finally:
        service.close()


def kill_resume_check() -> dict:
    completed = subprocess.run(
        [str(VENV_PY), str(REPO / "tools" / "run_s02_kill_resume.py")],
        capture_output=True, text=True, timeout=600, check=False, cwd=str(REPO),
    )
    (OUT / "kill_resume.log").write_text(completed.stdout + completed.stderr, encoding="utf-8")
    result: dict = {"exit_code": completed.returncode}
    try:
        result.update(json.loads(completed.stdout[completed.stdout.find("{"):]))
    except (ValueError, json.JSONDecodeError):
        result["error"] = "could not parse kill/resume output"
    (OUT / "kill_resume.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def error_paths() -> dict:
    junit = OUT / "error_paths.xml"
    completed = subprocess.run(
        [
            str(VENV_PY), "-m", "pytest",
            "-k", "disk_full or timeout or source_change or corrupted or cancel or duplicate",
            f"--junitxml={junit}", "-o", "junit_family=xunit2",
        ],
        capture_output=True, text=True, timeout=900, check=False, cwd=str(REPO),
    )
    (OUT / "error_paths.log").write_text(completed.stdout + completed.stderr, encoding="utf-8")
    passed = failed = skipped = 0
    if junit.is_file():
        root = ET.parse(junit).getroot()
        suites = [root] if root.tag == "testsuite" else list(root)
        for suite in suites:
            passed += int(suite.get("tests", 0)) - int(suite.get("failures", 0)) - int(suite.get("errors", 0)) - int(suite.get("skipped", 0))
            failed += int(suite.get("failures", 0)) + int(suite.get("errors", 0))
            skipped += int(suite.get("skipped", 0))
    summary = f"{passed} error-path tests passed" + (f", {failed} failed" if failed else "")
    return {"exit_code": completed.returncode, "summary": summary, "passed": passed, "failed": failed, "skipped": skipped}


def real_footage(data_root: Path, footage: Path) -> dict:
    settings = Settings.load()
    service = LibraryService(settings)
    try:
        project = service.create_project("Real footage S02", str(footage))
        scan_started = time.monotonic()
        stats = service.scan(project["project_id"])
        scan_seconds = time.monotonic() - scan_started
        shortest = service.list_assets(project["project_id"], sort="duration", limit=3)["items"]
        samples = []
        for asset in shortest:
            started = time.monotonic()
            outcome = service.pipeline.run(asset, Path(asset["path"]), profile="quick")
            elapsed = time.monotonic() - started
            samples.append(
                {
                    "filename": asset["filename"],
                    "codec": outcome.timeline.codec,
                    "stored": [outcome.timeline.width, outcome.timeline.height],
                    "display": [outcome.timeline.display_width, outcome.timeline.display_height],
                    "rotation": outcome.timeline.rotation,
                    "duration_ms": outcome.timeline.duration_ms,
                    "vfr": outcome.timeline.vfr_detected,
                    "hdr": outcome.timeline.is_hdr,
                    "scenes": len(outcome.segments),
                    "frames": len(outcome.frames),
                    "observed_gap_ms": outcome.observed_max_gap_ms,
                    "target_gap_ms": outcome.plan.max_gap_ms,
                    "max_abs_offset_ms": max((abs(int(f["offset_ms"])) for f in outcome.frames), default=0),
                    "pts_mismatch": any(bool(f.get("pts_mismatch")) for f in outcome.frames),
                    "elapsed_s": round(elapsed, 3),
                }
            )
        return {
            "added": stats.added,
            "errors": stats.errors,
            "scan_seconds": round(scan_seconds, 3),
            "samples": samples,
            "cache_bytes": dir_size(settings.data.sampling),
        }
    finally:
        service.close()


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    data_root = Path(tempfile.mkdtemp(prefix="pth_g2_"))
    os.environ["PHUTHANHAI_DATA_DIR"] = str(data_root / "data")
    footage_env = os.environ.get("PHUTHANHAI_TEST_FOOTAGE")
    footage = Path(footage_env).expanduser() if footage_env else None
    steps: list[dict] = []
    started = time.monotonic()

    steps.append(step_pytest())

    matrix = fixture_matrix(data_root)
    failing = {name: data["problems"] for name, data in matrix["fixtures"].items() if data.get("problems")}
    steps.append(
        step(
            "fixture matrix (PTS/VFR/rotation/HDR/cuts/dark/blur/motion/ProRes)",
            "PASS" if not failing else "FAIL",
            f"{len(matrix['fixtures'])} fixtures · problems={failing or 'none'}",
            matrix,
        )
    )

    invalidation = cache_invalidation_check(data_root)
    steps.append(
        step(
            "cache invalidation on source change + notes preserved",
            "PASS" if invalidation.get("passed") else "FAIL",
            f"fingerprint_changed={invalidation.get('fingerprint_changed')} "
            f"re-extracted={invalidation.get('second_extracted')}/{invalidation.get('second_frames')} "
            f"cache_dirs={invalidation.get('cache_dir_count')} "
            f"note_preserved={invalidation.get('note_preserved')}",
            invalidation,
        )
    )

    kill = kill_resume_check()
    steps.append(
        step(
            "hard SIGKILL at extract + scan stages, resume without duplicates",
            "PASS" if kill.get("passed") else "FAIL",
            " · ".join(
                f"{stage['stage']}: committed={stage.get('committed_frames_at_kill', '—')} "
                f"final={stage.get('final_frames')} unique={stage.get('unique_ids')}"
                for stage in kill.get("stages", [])
            ),
            kill,
        )
    )

    errors = error_paths()
    steps.append(
        step(
            "error paths (disk full, timeout, source change, corrupt cache, cancel)",
            "PASS" if errors["exit_code"] == 0 else "FAIL",
            errors.get("summary", "pytest -k selection"),
            errors,
        )
    )

    resume = matrix["fixtures"].get("_resume", {})
    steps.append(
        step(
            "checkpoint/resume: second run must not re-extract",
            "PASS" if resume.get("extracted") == 0 and resume.get("cache_hits", 0) > 0 else "FAIL",
            f"extracted={resume.get('extracted')} cache_hits={resume.get('cache_hits')}",
            resume,
        )
    )

    if footage is not None and footage.is_dir():
        real = real_footage(data_root, footage)
        bad = [
            sample
            for sample in real["samples"]
            if sample["pts_mismatch"] or sample["frames"] == 0 or sample["observed_gap_ms"] > sample["target_gap_ms"]
        ]
        steps.append(
            step(
                "real footage (read-only) sampling",
                "PASS" if real["added"] > 0 and not real["errors"] and not bad else "FAIL",
                f"{real['added']} clips scanned in {real['scan_seconds']}s · "
                f"{len(real['samples'])} sampled · bad={len(bad)}",
                real,
            )
        )
    else:
        steps.append(step("real footage (read-only) sampling", "SKIP", "PHUTHANHAI_TEST_FOOTAGE not set"))

    # S01 browser regression (library virtualization, playback, notes, shortcuts).
    env = {**os.environ}
    if footage is not None:
        env["PHUTHANHAI_TEST_FOOTAGE"] = str(footage)
    flow = run_tool([str(VENV_PY), str(REPO / "tools" / "run_s01_ui_flow.py")], OUT / "ui_flow.log", env=env)
    flow_path = REPO / "artifacts" / "qa" / "S01" / "e2e" / "ui_flow.json"
    flow_result = json.loads(flow_path.read_text(encoding="utf-8")) if flow_path.is_file() else {}
    steps.append(
        step(
            "browser E2E regression (notes, wall-clock, flush, shortcuts)",
            "PASS" if flow["exit_code"] == 0 and flow_result.get("passed") else "FAIL",
            f"checks={flow_result.get('checks')} shortcuts={flow_result.get('shortcut_checks')}",
            flow_result,
        )
    )

    library = run_tool([str(VENV_PY), str(REPO / "tools" / "run_s01_e2e.py")], OUT / "library_e2e.log", env=env)
    lib_path = REPO / "artifacts" / "qa" / "S01" / "e2e" / "e2e.json"
    lib_result = json.loads(lib_path.read_text(encoding="utf-8")) if lib_path.is_file() else {}
    steps.append(
        step(
            "browser E2E regression (1,000-asset DOM + playback)",
            "PASS" if library["exit_code"] == 0 else "FAIL",
            " · ".join(
                f"{item['case']}: cards={item.get('asset_card_nodes')} play={item.get('playback_ok')}"
                for item in lib_result.get("results", [])
            ),
            lib_result,
        )
    )

    ps = subprocess.run(["/bin/ps", "-axo", "pid=,command="], capture_output=True, text=True, check=False)
    leftovers = [
        line.strip()
        for line in ps.stdout.splitlines()
        if re.search(r"(phuthanhai\.cli serve|phuthanhai\.analyzer\.vlm_worker)", line)
    ]
    (OUT / "orphans.json").write_text(json.dumps({"matching": leftovers}, indent=2), encoding="utf-8")
    steps.append(step("orphans", "PASS" if not leftovers else "FAIL", f"leftovers={len(leftovers)}"))

    resources = snapshot()
    max_offset = max(
        (data.get("max_abs_offset_ms", 0) for data in matrix["fixtures"].values() if isinstance(data, dict)),
        default=0,
    )
    usage = resource.getrusage(resource.RUSAGE_SELF)
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    real_cache = next(
        (item["evidence"].get("cache_bytes", 0) for item in steps if item["step"].startswith("real footage")),
        0,
    )
    metrics = {
        "generated_at": now_iso(),
        "hardware": hardware_facts(),
        "resources": resources.to_dict(),
        "peak_rss_bytes": peak_rss_bytes(),
        "sampling_cache_bytes": matrix["cache_bytes"],
        "gate_elapsed_s": round(time.monotonic() - started, 2),
        "cpu_seconds_self": round(usage.ru_utime + usage.ru_stime, 2),
        "cpu_seconds_children": round(children.ru_utime + children.ru_stime, 2),
        "cpu_seconds_total": round(
            usage.ru_utime + usage.ru_stime + children.ru_utime + children.ru_stime, 2
        ),
        "real_footage_cache_bytes": real_cache,
        "max_abs_pts_offset_ms": max_offset,
        "fixture_count": len([name for name in matrix["fixtures"] if not name.startswith("_")]),
    }
    (OUT / "device_metrics.json").write_text(json.dumps(metrics, indent=2, ensure_ascii=False), encoding="utf-8")
    steps.append(
        step(
            "device metrics",
            "PASS",
            f"peak RSS {round(peak_rss_bytes() / 1024**2, 1)} MiB · sampling cache {round(matrix['cache_bytes'] / 1024**2, 1)} MiB · "
            f"free disk {resources.disk_free_bytes // 1024**3} GiB",
            metrics,
        )
    )

    verdict = "FAIL" if any(item["status"] == "FAIL" for item in steps) else (
        "PASS_WITH_WARNINGS" if any(item["status"] == "SKIP" for item in steps) else "PASS"
    )
    lines = [
        "# S02 — Two-Pass Sampling / PTS / Checkpoint — Gate G2",
        "",
        f"- Generated: {now_iso()}",
        "",
        "| # | Requirement | Result | Detail |",
        "|---|---|---|---|",
    ]
    for index, item in enumerate(steps, start=1):
        lines.append(f"| {index} | {item['step']} | **{item['status']}** | {item['detail']} |")
    lines += ["", f"## Verdict: {verdict}", ""]
    (OUT / "gate.json").write_text(
        json.dumps({"milestone": "S02", "generated_at": now_iso(), "verdict": verdict, "steps": steps, "metrics": metrics}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    (OUT / "gate.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    shutil.rmtree(data_root, ignore_errors=True)
    return 0 if verdict != "FAIL" else 1


if __name__ == "__main__":
    sys.exit(main())
