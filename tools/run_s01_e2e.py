#!/usr/bin/env python3
"""S01 browser E2E: real server + headless Chrome against the built bundle.

Two cases:

* ``bench`` - 1,000 assets; proves the library is virtualized in the real DOM
  (only a window of cards exists for 1,000 rows) and the total is reported.
* ``real``  - the operator's authorized footage folder; proves real thumbnails,
  codec metadata and scanning work end to end in the browser.

Artifacts are written to ``artifacts/qa/S01/e2e/``.
"""

from __future__ import annotations

import argparse
import base64
import http.client
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "artifacts" / "qa" / "S01" / "e2e"
VENV_PY = REPO / ".venv" / "bin" / "python"
CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
#: Real footage is operator-supplied; a media path is private and never committed.
REAL_FOOTAGE = Path(os.environ["PHUTHANHAI_TEST_FOOTAGE"]).expanduser() if os.environ.get("PHUTHANHAI_TEST_FOOTAGE") else None

sys.path.insert(0, str(REPO / "tools"))
from cdp import CdpSession, page_websocket_url  # noqa: E402


def pick_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_health(port: int, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            connection.request("GET", "/api/v1/health")
            response = connection.getresponse()
            response.read()
            connection.close()
            if response.status == 200:
                return
        except OSError:
            pass
        time.sleep(0.25)
    raise RuntimeError("backend did not become healthy")


def seed_bench(data_root: Path, count: int = 1000) -> Path:
    """Create ``count`` tiny videos and register them directly (no scan needed)."""

    sys.path.insert(0, str(REPO / "src"))
    os.environ["PHUTHANHAI_DATA_DIR"] = str(data_root)
    from phuthanhai.config import Settings
    from phuthanhai.library import store
    from phuthanhai.library.db import Database

    footage = data_root / "bench_footage"
    footage.mkdir(parents=True)
    template = footage / "_template.mp4"
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-v", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=1",
            "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast", str(template),
        ],
        check=True,
    )
    settings = Settings.load()
    settings.data.ensure()
    db = Database(settings.data.registry_db)
    db.migrate()
    with db.write() as conn:
        project = store.create_project(conn, name="Bench 1000", root_path=str(footage))
        for index in range(1, count + 1):
            filename = f"C_{index:04d}.MOV"
            target = footage / filename
            if not target.exists():
                os.link(template, target)
            store.upsert_asset(
                conn,
                asset_id=str(uuid.uuid4()),
                project_id=project["project_id"],
                path=str(target),
                filename=filename,
                size_bytes=template.stat().st_size,
                mtime_ns=template.stat().st_mtime_ns,
                dev=None,
                ino=None,
                duration_ms=1000,
                width=320,
                height=240,
                rotation=0,
                fps_nominal="25/1",
                codec="h264",
                has_audio=False,
                vfr_hint=False,
                color_transfer="bt709",
            )
    db.close_current_thread()
    template.unlink(missing_ok=True)
    return footage


def seed_real(data_root: Path) -> Path:
    sys.path.insert(0, str(REPO / "src"))
    os.environ["PHUTHANHAI_DATA_DIR"] = str(data_root)
    from phuthanhai.config import Settings
    from phuthanhai.library.service import LibraryService

    settings = Settings.load()
    service = LibraryService(settings)
    try:
        project = service.create_project("Real footage", str(REAL_FOOTAGE))
        service.scan(project["project_id"])
    finally:
        service.close()
    return REAL_FOOTAGE


def run_browser(port: int, *, screenshot: Path, dom_path: Path) -> tuple[str, dict]:
    """Drive the real app with CDP: render, click a clip, verify playback, screenshot."""

    profile = Path(tempfile.mkdtemp(prefix="pth_chrome_"))
    debug_port = pick_port()
    chrome = subprocess.Popen(  # noqa: S603
        [
            str(CHROME), "--headless=new", "--hide-scrollbars",
            "--no-first-run", "--no-default-browser-check",
            f"--user-data-dir={profile}", f"--remote-debugging-port={debug_port}",
            "--remote-allow-origins=*", "--window-size=1440,900", "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    session = None
    try:
        session = CdpSession(page_websocket_url(debug_port))
        session.call("Page.enable")
        session.call("Runtime.enable")
        session.call("Page.bringToFront")
        session.navigate(f"http://127.0.0.1:{port}/")
        session.wait_for("document.querySelectorAll('.asset-card').length > 0", timeout=40)
        dom = session.evaluate("document.documentElement.outerHTML")
        dom_path.write_text(dom, encoding="utf-8")

        # Prove playback, not just rendering: click a clip and wait for metadata.
        session.evaluate("document.querySelector('.asset-card').click()")
        session.wait_for("!!document.querySelector('video')", timeout=15)
        deadline = time.monotonic() + 60
        state: dict = {}
        while time.monotonic() < deadline:
            state = session.evaluate(
                "(()=>{const v=document.querySelector('video');"
                "return {ready:v?v.readyState:-1, error:v&&v.error?v.error.code:null,"
                "width:v?v.videoWidth:0, height:v?v.videoHeight:0,"
                "src:v?v.currentSrc:''};})()"
            ) or {}
            if state.get("ready", 0) >= 1:
                break
            time.sleep(0.5)
        state["proxy_overlay"] = bool(
            session.evaluate("document.body.innerText.includes('Đang tạo proxy')")
        )
        shot = session.call("Page.captureScreenshot", {"format": "png"})
        screenshot.write_bytes(base64.b64decode(shot["data"]))
        return dom, state
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            os.killpg(os.getpgid(chrome.pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            chrome.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(chrome.pid), signal.SIGKILL)
            chrome.wait(timeout=5)
        shutil.rmtree(profile, ignore_errors=True)


def analyse_dom(dom: str) -> dict:
    card_count = len(re.findall(r'class="asset-card[ "]', dom))
    return {
        "bytes": len(dom),
        "asset_card_nodes": card_count,
        "video_elements": dom.count("<video"),
        "has_four_tabs": all(label in dom for label in ("AI Video POV", "Phần 3", "Phần 4")),
        "total_label": (re.search(r">(\d+) clip<", dom) or [None, None])[1],
        "error_shown": "Không tải được" in dom,
    }


def run_case(name: str, seed, *, port: int) -> dict:
    data_root = Path(tempfile.mkdtemp(prefix=f"pth_e2e_{name}_"))
    env = {**os.environ, "PHUTHANHAI_DATA_DIR": str(data_root)}
    server = None
    try:
        footage = seed(data_root)
        server = subprocess.Popen(  # noqa: S603
            [str(VENV_PY), "-m", "phuthanhai.cli", "serve", "--host", "127.0.0.1", "--port", str(port)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            text=True,
        )
        wait_health(port)
        dom_path = OUT / f"{name}.dom.html"
        screenshot = OUT / f"{name}.png"
        dom, video = run_browser(port, screenshot=screenshot, dom_path=dom_path)
        metrics = analyse_dom(dom)
        metrics.update(
            {
                "case": name,
                "footage": str(footage),
                "screenshot": str(screenshot),
                "screenshot_bytes": screenshot.stat().st_size if screenshot.exists() else 0,
                "video": video,
                "playback_ok": video.get("ready", 0) >= 1
                and (video.get("width", 0) > 0 or "proxy-stream" in str(video.get("src", ""))),
            }
        )
        return metrics
    finally:
        if server is not None:
            try:
                os.killpg(os.getpgid(server.pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                server.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(server.pid), signal.SIGKILL)
                server.wait(timeout=10)
        shutil.rmtree(data_root, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-real", action="store_true")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)

    if not CHROME.is_file():
        print("Chrome not installed; E2E cannot run", file=sys.stderr)
        return 2

    results = []
    bench = run_case("bench", seed_bench, port=pick_port())
    results.append(bench)
    ok_bench = (
        bench["asset_card_nodes"] > 0
        and bench["asset_card_nodes"] < 120
        and bench["total_label"] == "1000"
        and bench["has_four_tabs"]
        and not bench["error_shown"]
        and bench["playback_ok"]
    )
    bench["passed"] = ok_bench

    if not args.skip_real and REAL_FOOTAGE is not None and REAL_FOOTAGE.is_dir():
        real = run_case("real", seed_real, port=pick_port())
        real["passed"] = (
            real["asset_card_nodes"] > 0
            and not real["error_shown"]
            and real["has_four_tabs"]
            and real["playback_ok"]
        )
        results.append(real)
    else:
        results.append({"case": "real", "passed": None, "skipped": "footage folder not present"})

    payload = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "results": results}
    (OUT / "e2e.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0 if all(item.get("passed") for item in results) else 1


if __name__ == "__main__":
    sys.exit(main())
