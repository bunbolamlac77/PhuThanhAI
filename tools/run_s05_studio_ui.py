#!/usr/bin/env python3
"""S05.3 Studio UI E2E: layout, Inspector tabs, draft preservation, thumbnail fallback."""

from __future__ import annotations

import base64
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
sys.path.insert(0, str(REPO / "tools"))

from cdp import CdpSession, page_websocket_url  # noqa: E402


def pick_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def seed(root: Path) -> str:
    import os as _os
    _os.environ["PHUTHANHAI_DATA_DIR"] = str(root)
    sys.path.insert(0, str(REPO / "src"))
    from phuthanhai.config import Settings
    from phuthanhai.library.service import LibraryService
    footage = root / "footage"
    footage.mkdir(parents=True)
    for index in (1, 2, 3, 4):
        subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi",
             "-i", "testsrc=size=480x270:rate=25:duration=3",
             "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
             str(footage / f"clip{index}.mp4")], check=True)
    service = LibraryService(Settings.load())
    try:
        project = service.create_project("Studio UI", str(footage))
        service.scan(project["project_id"])
        return project["project_id"]
    finally:
        service.close()


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    data = Path(tempfile.mkdtemp(prefix="pth_studio_"))
    profile = Path(tempfile.mkdtemp(prefix="pth_studio_chrome_"))
    port, debug = pick_port(), pick_port()
    server = chrome = session = None
    result: dict = {"passed": False}
    try:
        project_id = seed(data)
        server = subprocess.Popen(
            [str(VENV_PY), "-m", "phuthanhai.cli", "serve", "--host", "127.0.0.1", "--port", str(port)],
            env={**os.environ, "PHUTHANHAI_DATA_DIR": str(data)},
            start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        for _ in range(150):
            try:
                import http.client
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                c.request("GET", "/api/v1/health")
                if c.getresponse().status == 200:
                    c.close()
                    break
                c.close()
            except OSError:
                pass
            time.sleep(0.2)
        chrome = subprocess.Popen(
            [str(CHROME), "--headless=new", "--no-first-run", "--no-default-browser-check",
             f"--user-data-dir={profile}", f"--remote-debugging-port={debug}",
             "--remote-allow-origins=*", "--window-size=1440,900",
             f"http://127.0.0.1:{port}/"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )
        time.sleep(2.5)
        session = CdpSession(page_websocket_url(debug))
        session.call("Page.enable")
        session.call("Runtime.enable")
        session.call("Network.enable")
        session.call("Page.bringToFront")
        for _ in range(60):
            if session.evaluate("!!document.querySelector('.studio-workspace')"):
                break
            time.sleep(0.25)
        result["workspace_present"] = bool(session.evaluate("!!document.querySelector('.studio-workspace')"))
        result["grid_columns"] = session.evaluate(
            "getComputedStyle(document.querySelector('.studio-workspace')).gridTemplateColumns")
        result["asset_cards"] = session.evaluate("document.querySelectorAll('.asset-card').length")
        result["inspector_tabs"] = session.evaluate("document.querySelectorAll('[data-inspector-tab]').length")
        result["panels_mounted"] = session.evaluate("document.querySelectorAll('[role=tabpanel]').length")
        # Select a clip and type a note.
        session.evaluate("document.querySelector('.asset-card').click()")
        time.sleep(0.8)
        result["video_present"] = bool(session.evaluate("!!document.querySelector('video')"))
        session.evaluate("document.getElementById('note-input').focus()")
        session.call("Input.insertText", {"text": "Nháp chuyển tab"})
        time.sleep(0.2)
        draft_before = session.evaluate("document.getElementById('note-input').value")
        # Switch to AI analysis and back; the draft must survive.
        session.evaluate("document.querySelector('[data-inspector-tab=analysis]').click()")
        time.sleep(0.3)
        result["analysis_visible"] = session.evaluate(
            "!document.querySelector('[role=tabpanel][aria-labelledby$=\"analysis\"]').hidden")
        session.evaluate("document.querySelector('[data-inspector-tab=notes]').click()")
        time.sleep(0.2)
        draft_after = session.evaluate("document.getElementById('note-input').value")
        result["draft_preserved"] = draft_before == draft_after == "Nháp chuyển tab"
        # Keyboard shortcut guard: Space on a focused tab must not toggle playback.
        session.evaluate("document.querySelector('[data-inspector-tab=analysis]').focus()")
        paused_before = session.evaluate("document.querySelector('video').paused")
        session.call("Input.dispatchKeyEvent", {"type": "keyDown", "key": " ", "code": "Space", "windowsVirtualKeyCode": 32, "text": " "})
        session.call("Input.dispatchKeyEvent", {"type": "keyUp", "key": " ", "code": "Space"})
        time.sleep(0.3)
        paused_after = session.evaluate("document.querySelector('video').paused")
        result["space_on_tab_ignored"] = paused_before == paused_after
        # Thumbnail fallback: block thumbnail requests and reload.
        session.call("Network.setBlockedURLs", {"urls": ["*/thumbnail"]})
        session.call("Page.reload", {"ignoreCache": True})
        time.sleep(2.0)
        for _ in range(40):
            if session.evaluate("document.querySelectorAll('.asset-card__fallback').length > 0"):
                break
            time.sleep(0.25)
        result["thumbnail_fallbacks"] = session.evaluate("document.querySelectorAll('.asset-card__fallback').length")
        result["fallback_text"] = session.evaluate(
            "document.querySelector('.asset-card__fallback')?.textContent?.trim() || ''")
        result["broken_images_visible"] = session.evaluate(
            "[...document.querySelectorAll('.asset-card__image')].filter(i => i.complete && i.naturalWidth === 0 && !i.hidden).length")
        # Responsive: no horizontal overflow at 1200 and 820 widths.
        session.call("Emulation.setDeviceMetricsOverride", {"width": 1200, "height": 800, "deviceScaleFactor": 1, "mobile": False})
        time.sleep(0.5)
        result["overflow_1200"] = session.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth + 2")
        session.call("Emulation.setDeviceMetricsOverride", {"width": 820, "height": 900, "deviceScaleFactor": 1, "mobile": False})
        time.sleep(0.5)
        result["overflow_820"] = session.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth + 2")
        shot = session.call("Page.captureScreenshot", {"format": "png"})
        (OUT / "studio_ui.png").write_bytes(base64.b64decode(shot["data"]))
        result["screenshot"] = str(OUT / "studio_ui.png")
        result["passed"] = (
            result["workspace_present"] and result["asset_cards"] >= 4
            and result["inspector_tabs"] == 2 and result["panels_mounted"] == 2
            and result["video_present"] and result["draft_preserved"]
            and result["space_on_tab_ignored"] and result["thumbnail_fallbacks"] > 0
            and "Không có ảnh" in result["fallback_text"]
            and result["broken_images_visible"] == 0
            and not result["overflow_1200"] and not result["overflow_820"]
        )
    finally:
        if session:
            try:
                session.call("Page.close")
            except Exception:
                pass
        for process in (chrome, server):
            if process:
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
        shutil.rmtree(data, ignore_errors=True)
        shutil.rmtree(profile, ignore_errors=True)
        (OUT / "studio_ui.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result, indent=2))
    return 0 if result.get("passed") else 1


if __name__ == "__main__":
    sys.exit(main())
