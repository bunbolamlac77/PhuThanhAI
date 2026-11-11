#!/usr/bin/env python3
"""S05.1: browser playback + notes while Qwen3-VL is generating (real worker).

Seeds a small H.264 project for UI interaction, and a real 4K wedding clip for the
model job. Chrome drives the real React app; the job is cancelled afterwards.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "artifacts" / "qa" / "S05"
VENV_PY = REPO / ".venv" / "bin" / "python"
CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
FOOTAGE = Path(os.environ.get("PHUTHANHAI_TEST_FOOTAGE", "")).expanduser()
sys.path.insert(0, str(REPO / "tools"))
sys.path.insert(0, str(REPO / "src"))
from cdp import CdpSession, page_websocket_url  # noqa: E402


def pick_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def http(port: int, method: str, path: str, body: dict | None = None, capability: str | None = None):
    import http.client
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    headers = {"Content-Type": "application/json"}
    if capability:
        headers["X-PhuThanhAI-Capability"] = capability
    connection.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
    response = connection.getresponse()
    raw = response.read().decode("utf-8")
    connection.close()
    return response.status, (json.loads(raw) if raw else {})


def wait_health(port: int) -> None:
    for _ in range(150):
        try:
            if http(port, "GET", "/api/v1/health")[0] == 200:
                return
        except OSError:
            pass
        time.sleep(0.2)
    raise RuntimeError("backend not healthy")


def seed(data_root: Path) -> tuple[str, str]:
    os.environ["PHUTHANHAI_DATA_DIR"] = str(data_root)
    from phuthanhai.config import Settings
    from phuthanhai.library.service import LibraryService
    fixtures = data_root / "fixtures"
    fixtures.mkdir(parents=True)
    for index in (1, 2):
        subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi",
             "-i", "testsrc=size=320x240:rate=25:duration=2",
             "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
             str(fixtures / f"clip{index}.mp4")],
            check=True,
        )
    service = LibraryService(Settings.load())
    try:
        # Create the model project first; the UI selects the newest project, which
        # must be the fixture project used for playback/notes.
        real_project = service.create_project("Live model", str(FOOTAGE))
        service.scan(real_project["project_id"])
        real_assets = service.list_assets(real_project["project_id"], sort="duration", limit=1)["items"]
        asset = real_assets[0]
        if asset.get("sampling_status") != "complete":
            service.pipeline.run(asset, Path(asset["path"]), profile="quick", max_edge=640)
        ui_project = service.create_project("Playback during inference", str(fixtures))
        service.scan(ui_project["project_id"])
        return ui_project["project_id"], asset["asset_id"]
    finally:
        service.close()


def main() -> int:
    if not FOOTAGE.is_dir():
        print("PHUTHANHAI_TEST_FOOTAGE is required", file=sys.stderr)
        return 2
    OUT.mkdir(parents=True, exist_ok=True)
    data_root = Path(tempfile.mkdtemp(prefix="pth_s05pi_"))
    chrome_profile = Path(tempfile.mkdtemp(prefix="pth_s05pi_chrome_"))
    result: dict = {"passed": False}
    server = chrome = session = None
    try:
        project_id, asset_id = seed(data_root)
        port = pick_port()
        server = subprocess.Popen(
            [str(VENV_PY), "-m", "phuthanhai.cli", "serve", "--host", "127.0.0.1", "--port", str(port)],
            env={**os.environ, "PHUTHANHAI_DATA_DIR": str(data_root)},
            start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        wait_health(port)
        _, session_info = http(port, "GET", "/api/v1/session")
        capability = session_info["capability"]
        status, job = http(port, "POST", f"/api/v1/assets/{asset_id}:analyze", {"method": "auto"}, capability)
        result["analysis_start_status"] = status
        for _ in range(120):
            state = http(port, "GET", f"/api/v1/jobs/{job['job_id']}")[1]
            if state["state"] == "running":
                break
            time.sleep(0.5)
        result["analysis_state_before_ui"] = state["state"]

        debug_port = pick_port()
        chrome = subprocess.Popen(
            [str(CHROME), "--headless=new", "--no-first-run", "--no-default-browser-check",
             f"--user-data-dir={chrome_profile}", f"--remote-debugging-port={debug_port}",
             "--remote-allow-origins=*", "--window-size=1440,900",
             "--autoplay-policy=no-user-gesture-required",
             f"http://127.0.0.1:{port}/"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )
        time.sleep(2.5)
        session = CdpSession(page_websocket_url(debug_port))
        session.call("Page.enable")
        session.call("Runtime.enable")
        session.call("Page.bringToFront")
        session.call("Page.navigate", {"url": f"http://127.0.0.1:{port}/"})
        for _ in range(60):
            if session.evaluate("!!document.querySelector('.asset-card')"):
                break
            time.sleep(0.25)
        result["asset_cards"] = session.evaluate("document.querySelectorAll('.asset-card').length")
        started = time.perf_counter()
        session.evaluate("document.querySelector('.asset-card').click()")
        time.sleep(1.0)
        result["video_present"] = bool(session.evaluate("!!document.querySelector('video')"))
        session.evaluate("document.querySelector('video').play().catch(()=>{})")
        time.sleep(1.0)
        result["play_ok"] = bool(session.evaluate("(()=>{const v=document.querySelector('video'); return !v.paused && v.currentTime > 0;})()"))
        session.evaluate("(()=>{const v=document.querySelector('video'); v.currentTime=1.0; v.pause();})()")
        result["seek_time"] = session.evaluate("document.querySelector('video').currentTime")
        # Type a note and wait for the API to persist it.
        time.sleep(0.5)
        session.call("Page.bringToFront")
        session.evaluate("document.getElementById('note-input').focus()")
        session.call("Input.insertText", {"text": "Ghi chú trong lúc AI chạy"})
        time.sleep(0.5)
        result["note_textarea_value"] = session.evaluate("document.getElementById('note-input').value")
        note_saved = False
        note_ms = None
        for _ in range(40):
            _, notes = http(port, "GET", f"/api/v1/projects/{project_id}/annotations")
            if any("trong lúc AI" in item.get("text", "") for item in notes):
                note_saved = True
                note_ms = round((time.perf_counter() - started) * 1000, 1)
                break
            time.sleep(0.25)
        result["note_saved"] = note_saved
        result["note_saved_ms"] = note_ms
        # Switch clip: the next clip must start with an empty draft (no leak).
        session.evaluate("document.querySelectorAll('.asset-card')[1]?.click()")
        time.sleep(0.6)
        result["second_clip_note_empty"] = session.evaluate("document.getElementById('note-input').value === ''")
        # Switch tabs while the job is still running.
        result["tabs_clicked"] = session.evaluate(
            "(()=>{const b=[...document.querySelectorAll('button')].filter(x=>/Phân tích|Ghi chú/.test(x.textContent)); b[0]?.click(); return b.length>0;})()"
        )
        time.sleep(0.5)
        state = http(port, "GET", f"/api/v1/jobs/{job['job_id']}")[1]
        result["analysis_state_after_ui"] = state["state"]
        workers = subprocess.run(["pgrep", "-f", "phuthanhai.analyzer.vlm_worker"], capture_output=True, text=True).stdout.split()
        result["workers_seen"] = workers
        # Cancel so no backend/model is left behind.
        http(port, "POST", f"/api/v1/jobs/{job['job_id']}:cancel", {}, capability)
        for _ in range(60):
            state = http(port, "GET", f"/api/v1/jobs/{job['job_id']}")[1]
            if state["state"] not in {"queued", "running", "cancelling"}:
                break
            time.sleep(0.5)
        result["final_state"] = state["state"]
        result["api_latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
        result["passed"] = (
            result["asset_cards"] >= 2 and result["video_present"] and result["play_ok"]
            and result["note_saved"] and result["second_clip_note_empty"]
            and result["analysis_state_before_ui"] == "running"
            and result["analysis_state_after_ui"] == "running"
            and len(workers) <= 1 and result["final_state"] in {"cancelled", "complete"}
        )
    finally:
        if session is not None:
            try:
                session.call("Page.close")
            except Exception:
                pass
        for process in (chrome, server):
            if process is not None:
                try:
                    os.killpg(os.getpgid(process.pid), 15)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(os.getpgid(process.pid), 9)
                    except ProcessLookupError:
                        pass
        shutil.rmtree(data_root, ignore_errors=True)
        shutil.rmtree(chrome_profile, ignore_errors=True)
        (OUT / "playback_inference.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result, indent=2))
    return 0 if result.get("passed") else 1


if __name__ == "__main__":
    sys.exit(main())
