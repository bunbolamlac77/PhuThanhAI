#!/usr/bin/env python3
"""S05 gate: P0 timecode/migration, recognition, native app, security, endurance.

Writes artifacts/qa/S05/{gate.md,gate.json,device_metrics.json,recognition.json,
scene_audit.json,app_selftest.json,app_launch.json}.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "artifacts" / "qa" / "S05"
S04 = REPO / "artifacts" / "qa" / "S04"
VENV_PY = REPO / ".venv" / "bin" / "python"
APP = REPO / "dist" / "PhuThanhAI.app"
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

from phuthanhai.analyzer.context_fusion import build_overlay  # noqa: E402
from phuthanhai.config import Settings  # noqa: E402
from phuthanhai.library.service import LibraryService  # noqa: E402
from phuthanhai.runtime.resource import GOVERNOR, snapshot  # noqa: E402


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def step(name: str, status: str, detail: str, evidence: dict | None = None) -> dict:
    return {"step": name, "status": status, "detail": detail, "evidence": evidence or {}}


def run_pytest(label: str, selection: list[str], junit_name: str, env: dict | None = None) -> dict:
    junit = OUT / junit_name
    completed = subprocess.run(
        [str(VENV_PY), "-m", "pytest", *selection, f"--junitxml={junit}", "-o", "junit_family=xunit2"],
        capture_output=True, text=True, timeout=2400, check=False, cwd=str(REPO),
        env={**os.environ, "PHUTHANHAI_DATA_DIR": "", **(env or {})} if env else
        {k: v for k, v in os.environ.items() if k != "PHUTHANHAI_DATA_DIR"},
    )
    (OUT / junit_name.replace(".xml", ".log")).write_text(completed.stdout + completed.stderr, encoding="utf-8")
    counts = {"passed": 0, "failed": 0, "skipped": 0}
    if junit.is_file():
        root = ET.parse(junit).getroot()
        suites = [root] if root.tag == "testsuite" else list(root)
        for suite in suites:
            counts["passed"] += int(suite.get("tests", 0)) - int(suite.get("failures", 0)) - int(suite.get("errors", 0)) - int(suite.get("skipped", 0))
            counts["failed"] += int(suite.get("failures", 0)) + int(suite.get("errors", 0))
            counts["skipped"] += int(suite.get("skipped", 0))
    return step(label, "PASS" if completed.returncode == 0 and counts["passed"] else "FAIL",
                f"{counts['passed']} passed, {counts['failed']} failed, {counts['skipped']} skipped", counts)


def recognition() -> tuple[dict, list[dict]]:
    os.environ["PHUTHANHAI_DATA_DIR"] = str(S04 / "data")
    service = LibraryService(Settings.load())
    try:
        truth = json.loads((S04 / "ground_truth.json").read_text())["labels"]
        predicted: dict[str, set[str]] = {}
        unresolved: dict[str, list[str]] = {}
        for project in service.list_projects():
            for asset in service.list_assets(project["project_id"], limit=5000)["items"]:
                evidence = service.vision.load_evidence(asset["asset_id"])
                if not evidence:
                    continue
                manifest = service.pipeline.load_manifest(asset["asset_id"], asset["sampling_fingerprint"])
                overlay = build_overlay(
                    asset_id=asset["asset_id"], asset_duration_ms=int(asset["duration_ms"]),
                    evidence=evidence, profile_id="wedding_traditional",
                    user_labels=service._labels(project["project_id"], asset["asset_id"]),
                    scene_windows=manifest.get("segments"),
                )
                predicted[asset["filename"]] = {item["label_key"] for item in overlay["candidates"]}
                unresolved[asset["filename"]] = [item["label_key"] for item in overlay["unresolved"]]
        tp = fp = fn = 0
        rows = []
        for filename, labels in predicted.items():
            expected = set(truth.get(filename, []))
            tp += len(labels & expected)
            fp += len(labels - expected)
            fn += len(expected - labels)
            rows.append({"file": filename, "expected": sorted(expected), "predicted": sorted(labels),
                         "missing": sorted(expected - labels), "extra": sorted(labels - expected),
                         "unresolved": unresolved[filename]})
        controls = [f for f in predicted if f in {"cfr.mp4", "dark.mp4"}]
        evaluation = {
            "dataset": "2 real wedding clips + 2 synthetic controls (same data used for tuning)",
            "before": {"precision": 0.8, "recall": 0.444, "source": "S04 gate"},
            "after": {"precision": round(tp / (tp + fp), 3) if tp + fp else None,
                      "recall": round(tp / (tp + fn), 3) if tp + fn else None},
            "confusion": {"tp": tp, "fp": fp, "fn": fn},
            "altar_false_positives_on_controls": sum(1 for f in controls if "ancestor_altar" in predicted[f]),
            "unknown_entries": sum(len(v) for v in unresolved.values()),
            "rows": rows,
            "held_out": False,
            "note": "Tuned and evaluated on the same two clips; not a generalization estimate.",
        }
        return evaluation, rows
    finally:
        service.close()


def scene_audit() -> dict:
    os.environ["PHUTHANHAI_DATA_DIR"] = str(S04 / "data")
    service = LibraryService(Settings.load())
    total = corrected = errors = 0
    assets = []
    try:
        for project in service.list_projects():
            for asset in service.list_assets(project["project_id"], limit=5000)["items"]:
                evidence = service.vision.load_evidence(asset["asset_id"])
                if not evidence or not asset.get("sampling_fingerprint"):
                    continue
                manifest = service.pipeline.load_manifest(asset["asset_id"], asset["sampling_fingerprint"])
                segments = {seg["scene_id"]: seg for seg in manifest.get("segments", [])}
                for frame in evidence["frames"]:
                    if frame["interpretation_level"] != "observed":
                        continue
                    total += 1
                    seg = segments.get(frame["scene_id"])
                    if seg is None:
                        errors += 1
                        continue
                    if not (seg["start_ms"] <= frame["normalized_pts_ms"] < seg["end_ms"]):
                        errors += 1
                    if frame["normalized_pts_ms"] > seg["start_ms"]:
                        corrected += 1
                first = next((f for f in evidence["frames"] if f["interpretation_level"] == "observed"), None)
                if first and first["scene_id"] in segments:
                    assets.append({
                        "file": asset["filename"], "scene": first["scene_id"],
                        "manifest_start_ms": segments[first["scene_id"]]["start_ms"],
                        "first_frame_ms": first["normalized_pts_ms"],
                        "offset_ms": first["normalized_pts_ms"] - segments[first["scene_id"]]["start_ms"],
                    })
        return {"frames_checked": total, "frames_inside_manifest_windows": total - errors,
                "frames_outside": errors, "first_frame_after_scene_start": corrected,
                "assets": assets, "passed": errors == 0 and total > 0}
    finally:
        service.close()


def app_build() -> dict:
    completed = subprocess.run([str(VENV_PY), str(REPO / "tools" / "build_macos_app.py")],
                               capture_output=True, text=True, timeout=600, check=False, cwd=str(REPO))
    (OUT / "app_build.log").write_text(completed.stdout + completed.stderr, encoding="utf-8")
    return {"exit_code": completed.returncode, "app": str(APP), "exists": APP.is_dir()}


def app_selftest() -> dict:
    output = OUT / "app_selftest.json"
    output.unlink(missing_ok=True)
    data = OUT / "data"
    command = [str(APP / "Contents/MacOS/PhuThanhAI"), "--self-test",
               "--self-test-output", str(output), "--data-dir", str(data)]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=240, check=False)
        result = {"exit_code": completed.returncode, "stderr": completed.stderr[-1000:]}
    except subprocess.TimeoutExpired:
        result = {"exit_code": -1, "error": "self-test timeout"}
    if output.is_file():
        result.update(json.loads(output.read_text(encoding="utf-8")))
    return result


def launch_check() -> dict:
    def sh(command: str) -> str:
        return subprocess.run(command, shell=True, capture_output=True, text=True).stdout.strip()
    out: dict = {}
    subprocess.run(["/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister", "-f", str(APP)], check=False)
    out["spotlight_paths"] = sh("mdfind 'kMDItemCFBundleIdentifier == \"com.phuthanh.PhuThanhAI\"'").splitlines()
    out["open_returncode"] = subprocess.run(["open", "-a", str(APP)], capture_output=True).returncode
    time.sleep(10)
    out["app_pids_first"] = sh("pgrep -f 'PhuThanhAI.app/Contents/MacOS/PhuThanhAI'").split()
    out["backend_pids_first"] = sh("pgrep -f 'phuthanhai.cli serve'").split()
    out["backend_parents"] = {pid: sh(f"ps -o ppid= -p {pid}").strip() for pid in out["backend_pids_first"]}
    out["application_type"] = sh("lsappinfo info -only ApplicationType $(lsappinfo find bundleID=com.phuthanh.PhuThanhAI)")
    out["system_events_exists"] = sh('osascript -e \'tell application "System Events" to exists process "PhuThanhAI"\'')
    subprocess.run(["open", "-a", str(APP)], check=False)
    time.sleep(4)
    out["app_pids_second"] = sh("pgrep -f 'PhuThanhAI.app/Contents/MacOS/PhuThanhAI'").split()
    out["backend_pids_second"] = sh("pgrep -f 'phuthanhai.cli serve'").split()
    out["screenshot_returncode"] = subprocess.run(["screencapture", "-x", str(OUT / "app_screen.png")], capture_output=True).returncode
    sh('osascript -e \'tell application "PhuThanhAI" to quit\'')
    time.sleep(8)
    out["app_pids_after_quit"] = sh("pgrep -f 'PhuThanhAI.app/Contents/MacOS/PhuThanhAI'").split()
    out["backend_pids_after_quit"] = sh("pgrep -f 'phuthanhai.cli serve'").split()
    out["port_listening_after_quit"] = bool(sh("lsof -nP -iTCP:49321 -sTCP:LISTEN"))
    out["passed"] = (out["open_returncode"] == 0 and len(out["app_pids_first"]) == 1
                     and len(out["backend_pids_first"]) == 1 and not out["app_pids_after_quit"]
                     and not out["backend_pids_after_quit"] and not out["port_listening_after_quit"]
                     and bool(out["spotlight_paths"]))
    return out


def portable_security() -> dict:
    package = S04 / "report_samples" / "workspace_package.json"
    portable = S04 / "report_samples" / "workspace_report_portable.md"
    result = {"package_present": package.is_file(), "portable_present": portable.is_file()}
    if portable.is_file():
        from phuthanhai.contracts.validators import parse_portable_workspace_md
        parsed = parse_portable_workspace_md(portable.read_text(encoding="utf-8"))
        result["roundtrip"] = parsed["kind"] == "phuthanhai.workspace_package"
        result["project_id"] = parsed["project_id"]
    return result


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    steps: list[dict] = []
    started = time.monotonic()
    os.environ.pop("PHUTHANHAI_DATA_DIR", None)

    steps.append(run_pytest("pytest (S00–S05 regression)", [], "pytest.xml"))
    steps.append(run_pytest("P0 scene timecode + scoped labels", ["tests/test_s05_p0_fields.py", "tests/test_s04_context_fusion.py"], "p0.xml"))
    steps.append(run_pytest("P0 real v4→v5 migration + restore", ["tests/test_s05_migration_v4.py"], "migration.xml"))
    steps.append(run_pytest("governor swap-rate + label/recognition regressions", ["tests/test_s05_governor.py", "tests/test_s04_context_profiles.py", "tests/test_s04_reports.py"], "quality.xml"))

    evaluation, rows = recognition()
    (OUT / "recognition.json").write_text(json.dumps(evaluation, indent=2, ensure_ascii=False), encoding="utf-8")
    steps.append(step(
        "wedding recognition before/after",
        "PASS" if evaluation["after"]["precision"] >= evaluation["before"]["precision"] and evaluation["after"]["recall"] > evaluation["before"]["recall"] else "FAIL",
        f"before {evaluation['before']['precision']}/{evaluation['before']['recall']} → after {evaluation['after']['precision']}/{evaluation['after']['recall']} "
        f"(same 2 clips; not held out)",
        evaluation,
    ))

    audit = scene_audit()
    (OUT / "scene_audit.json").write_text(json.dumps(audit, indent=2, ensure_ascii=False), encoding="utf-8")
    steps.append(step(
        "scene boundaries from S02 manifest",
        "PASS" if audit["passed"] and audit["first_frame_after_scene_start"] > 0 else "FAIL",
        f"{audit['frames_checked']} frames checked, {audit['frames_outside']} outside windows, "
        f"{audit['first_frame_after_scene_start']} first-frames offset from scene start",
        audit,
    ))

    bench5000 = json.loads((OUT / "bench5000.json").read_text()) if (OUT / "bench5000.json").is_file() else {}
    steps.append(step(
        "5,000-asset benchmark (API + virtualized DOM + restart)",
        "PASS" if bench5000.get("passed") else "NOT VERIFIED",
        f"API list p95={bench5000.get('backend',{}).get('list_p95_ms')}ms search p95={bench5000.get('backend',{}).get('search_p95_ms')}ms · "
        f"DOM cards={bench5000.get('browser',{}).get('asset_card_nodes')} total={bench5000.get('browser',{}).get('total_label')} · "
        f"restart={bench5000.get('backend',{}).get('restart_total')}",
        bench5000,
    ))

    endurance = json.loads((OUT / "endurance.json").read_text()) if (OUT / "endurance.json").is_file() else {}
    cancel = endurance.get("cancel", {})
    optimized = json.loads((OUT / "endurance_optimized.json").read_text()) if (OUT / "endurance_optimized.json").is_file() else {}
    optimized_refusals = optimized.get("governor_refusals", [])
    steps.append(step(
        "real-model endurance (multi-batch, RSS/MLX/swap)",
        "PASS" if endurance.get("passed") else "NOT VERIFIED",
        f"{endurance.get('completed_clips',0)}/{endurance.get('planned_clips',0)} clips completed; "
        f"deferred_by_governor={bool(endurance.get('governor_refusals'))}; "
        f"first batch swap delta={round((endurance.get('clips',[{}])[0].get('swap_delta_bytes') or 0)/1024**3,2)} GiB; "
        f"optimized 512px/batch1 refusals={[item.get('code') for item in optimized_refusals]}",
        {"main": endurance, "optimized": optimized},
    ))
    steps.append(step(
        "live Qwen3-VL cancel + resume",
        "PASS" if cancel.get("passed") else "FAIL",
        f"committed={cancel.get('committed_frames_at_cancel')} cancel={cancel.get('cancel_seconds')}s "
        f"state={cancel.get('job_state')} workers={cancel.get('active_workers')} resume={cancel.get('resume_state')}",
        cancel,
    ))

    build = app_build()
    selftest = app_selftest()
    (OUT / "app_selftest.json").write_text(json.dumps(selftest, indent=2, ensure_ascii=False), encoding="utf-8")
    videos = selftest.get("videos", [])
    playable = [v for v in videos if v.get("ok")]
    by_name = {v.get("file"): v for v in videos}
    expected = {"cfr.mp4", "prores.mov", "rotation.mp4", "vfr.mp4"}
    codec_ok = expected <= {v.get("file") for v in playable}
    a11y = selftest.get("a11y") or {}
    a11y_ok = not a11y.get("unnamed_buttons") and not a11y.get("unlabeled_inputs") and a11y.get("images_without_alt") == 0
    steps.append(step(
        "native .app build + WKWebView self-test",
        "PASS" if build["exists"] and selftest.get("ok") and codec_ok else "FAIL",
        f"library={selftest.get('library')} health={selftest.get('health')} playable={len(playable)}/{len(videos)} "
        f"codecs={'ok' if codec_ok else 'missing'} · a11y_auto={'ok' if a11y_ok else 'issues'}",
        selftest,
    ))
    steps.append(step(
        "automated accessibility audit (WKWebView DOM)",
        "PASS" if a11y_ok else "FAIL",
        f"lang={a11y.get('lang')} buttons={a11y.get('buttons')} unnamed={len(a11y.get('unnamed_buttons',[]))} "
        f"inputs={a11y.get('inputs')} unlabeled={len(a11y.get('unlabeled_inputs',[]))} "
        f"images_without_alt={a11y.get('images_without_alt')} focusable={a11y.get('focusable')}",
        a11y,
    ))

    launch = launch_check()
    (OUT / "app_launch.json").write_text(json.dumps(launch, indent=2), encoding="utf-8")
    steps.append(step(
        "Finder/Spotlight/Dock launch, single instance, quit reap",
        "PASS" if launch["passed"] else "FAIL",
        f"spotlight={bool(launch['spotlight_paths'])} type={launch['application_type']} "
        f"app={len(launch['app_pids_first'])} backend={len(launch['backend_pids_first'])} "
        f"after_quit={len(launch['backend_pids_after_quit'])}",
        launch,
    ))

    security = portable_security()
    steps.append(step(
        "V2/V3 portable + privacy regression",
        "PASS" if security.get("roundtrip") else "FAIL",
        f"portable={security.get('portable_present')} package={security.get('package_present')} roundtrip={security.get('roundtrip')}",
        security,
    ))

    golden = json.loads((OUT / "golden" / "manifest.json").read_text()) if (OUT / "golden" / "manifest.json").is_file() else {}
    available = golden.get("available", {})
    confirmed_by_genre = {
        genre: sum(1 for row in golden.get("rows", []) if row.get("genre") == genre and row.get("owner_confirmed"))
        for genre in ("wedding_traditional", "vlog_travel", "event_bts")
    }
    split = golden.get("split", {})
    golden_ready = (
        all(available.get(genre, 0) >= 10 and confirmed_by_genre.get(genre, 0) >= 10
            for genre in ("wedding_traditional", "vlog_travel", "event_bts"))
        and split.get("locked") is True
        and split.get("held_out_rows", 0) > 0
        and golden.get("held_out_evaluation") is not None
    )
    steps.append(step(
        "golden dataset: 3 genres with human labels",
        "PASS" if golden_ready else "NOT VERIFIED",
        f"available={available} confirmed={confirmed_by_genre} split_locked={split.get('locked')} "
        f"held_out_rows={split.get('held_out_rows', 0)} held_out_metrics={golden.get('held_out_evaluation') is not None}",
        {"available": available, "confirmed": confirmed_by_genre, "split": split,
         "held_out_evaluation": golden.get("held_out_evaluation")},
    ))
    playback = json.loads((OUT / "playback_inference.json").read_text()) if (OUT / "playback_inference.json").is_file() else {}
    steps.append(step(
        "playback + notes while real inference runs (browser E2E)",
        "PASS" if playback.get("passed") else "FAIL",
        f"job running before/after={playback.get('analysis_state_before_ui')}/{playback.get('analysis_state_after_ui')} "
        f"play={playback.get('play_ok')} note={playback.get('note_saved')} ({playback.get('note_saved_ms')}ms) "
        f"clip_switch_clean={playback.get('second_clip_note_empty')} workers={len(playback.get('workers_seen',[]))} "
        f"final={playback.get('final_state')}",
        playback,
    ))
    manual_path = OUT / "accessibility_manual.json"
    manual = json.loads(manual_path.read_text(encoding="utf-8")) if manual_path.is_file() else {}
    manual_checks = {k: v for k, v in manual.items() if isinstance(v, bool) and k not in {"owner_confirmed"}}
    manual_ready = bool(manual.get("owner_confirmed")) and bool(manual_checks) and all(manual_checks.values())
    steps.append(step(
        "manual VoiceOver / reduced-motion QA",
        "PASS" if manual_ready else "NOT VERIFIED",
        f"owner_confirmed={manual.get('owner_confirmed')} passed={sum(1 for v in manual_checks.values() if v)}/{len(manual_checks)} "
        f"reduced_motion={manual.get('reduced_motion_confirmed')}",
        manual,
    ))

    leftovers = [line for line in subprocess.run(["/bin/ps", "-axo", "pid=,command="], capture_output=True, text=True).stdout.splitlines()
                 if ("phuthanhai.analyzer.vlm_worker" in line or "phuthanhai.cli serve" in line) and "grep" not in line]
    steps.append(step("orphans", "PASS" if not leftovers else "FAIL", f"leftovers={len(leftovers)}", {"matching": leftovers}))

    device = {
        "generated_at": now_iso(), "hardware": None, "resources": snapshot().to_dict(),
        "gate_elapsed_s": round(time.monotonic() - started, 2),
        "swap_rate_bytes_per_min": GOVERNOR.last_swap_rate_bytes_per_min,
        "selftest_videos": videos,
    }
    from phuthanhai.config import hardware_facts
    device["hardware"] = hardware_facts()
    (OUT / "device_metrics.json").write_text(json.dumps(device, indent=2, ensure_ascii=False), encoding="utf-8")
    steps.append(step("device metrics", "PASS", f"gate {device['gate_elapsed_s']}s · swap rate {round(device['swap_rate_bytes_per_min']/1024**2,1)} MiB/min", device))

    statuses = {item["status"] for item in steps}
    verdict = "FAIL" if "FAIL" in statuses else ("NOT VERIFIED" if "NOT VERIFIED" in statuses else "PASS")
    lines = ["# S05 — Native App, Recognition, Reliability, Production Readiness — Gate G5", "",
             f"- Generated: {now_iso()}",
             "- This gate distinguishes implemented / automatically tested / tested on the real Mac / not verified.",
             "", "| # | Requirement | Result | Detail |", "|---|---|---|---|"]
    for index, item in enumerate(steps, 1):
        lines.append(f"| {index} | {item['step']} | **{item['status']}** | {item['detail']} |")
    lines += ["", f"## Verdict: {verdict}", ""]
    (OUT / "gate.json").write_text(json.dumps({"milestone": "S05", "generated_at": now_iso(), "verdict": verdict, "steps": steps, "metrics": device}, indent=2, ensure_ascii=False), encoding="utf-8")
    (OUT / "gate.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    return 0 if verdict != "FAIL" else 1


if __name__ == "__main__":
    sys.exit(main())
