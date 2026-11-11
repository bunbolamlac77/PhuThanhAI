#!/usr/bin/env python3
"""S01 UI flow E2E: type a note in the real browser and verify persistence.

Flow (all on loopback, real server, real React build, real SQLite):

1. create a project over two generated clips and scan it;
2. open the app in headless Chrome via the DevTools Protocol;
3. click the first asset, type "Lên phà lúc 5 giờ sáng" into the note box;
4. wait for the UI to report "Đã lưu";
5. click the wall-clock suggestion "Dùng 05:00";
6. assert through the API that the note exists with wall_clock 05:00 and that
   ``start_ms`` stayed null (a clock time is not a video timestamp);
7. capture a screenshot as UI QA evidence.
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "artifacts" / "qa" / "S01" / "e2e"
VENV_PY = REPO / ".venv" / "bin" / "python"
CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")

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
        time.sleep(0.2)
    raise RuntimeError("backend not healthy")


def api(port: int, method: str, path: str) -> dict:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request(method, path)
        response = connection.getresponse()
        body = response.read().decode("utf-8")
        if not body:
            return {"status": response.status}
        payload = json.loads(body)
        if isinstance(payload, list):
            return {"items": payload, "_status": response.status}
        payload["_status"] = response.status
        return payload
    finally:
        connection.close()


def seed(data_root: Path) -> Path:
    sys.path.insert(0, str(REPO / "src"))
    os.environ["PHUTHANHAI_DATA_DIR"] = str(data_root)
    from phuthanhai.config import Settings
    from phuthanhai.library.service import LibraryService

    footage = data_root / "footage"
    footage.mkdir(parents=True)
    for index in (1, 2):
        subprocess.run(
            [
                "ffmpeg", "-nostdin", "-v", "error", "-y",
                "-f", "lavfi", "-i", f"testsrc=size=320x240:rate=25:duration=1",
                "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
                str(footage / f"C{index:04d}.MOV"),
            ],
            check=True,
        )
    service = LibraryService(Settings.load())
    try:
        project = service.create_project("UI Flow", str(footage))
        service.scan(project["project_id"])
        return Path(project["project_id"])
    finally:
        service.close()


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    data_root = Path(tempfile.mkdtemp(prefix="pth_uiflow_"))
    chrome_profile = Path(tempfile.mkdtemp(prefix="pth_uichrome_"))
    server = None
    chrome = None
    session = None
    result: dict = {"passed": False}
    try:
        project_id = seed(data_root)
        port = pick_port()
        env = {**os.environ, "PHUTHANHAI_DATA_DIR": str(data_root)}
        server = subprocess.Popen(  # noqa: S603
            [str(VENV_PY), "-m", "phuthanhai.cli", "serve", "--host", "127.0.0.1", "--port", str(port)],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True, text=True,
        )
        wait_health(port)

        debug_port = pick_port()
        chrome = subprocess.Popen(  # noqa: S603
            [
                str(CHROME), "--headless=new", "--disable-gpu", "--no-first-run",
                "--no-default-browser-check", f"--user-data-dir={chrome_profile}",
                f"--remote-debugging-port={debug_port}", "--remote-allow-origins=*",
                "--window-size=1440,900", "about:blank",
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )
        session = CdpSession(page_websocket_url(debug_port))
        session.call("Page.enable")
        session.call("Runtime.enable")
        # Headless pages are not focused by default; without this, Input.insertText
        # updates the DOM value but React never receives a trusted input event.
        session.call("Page.bringToFront")
        session.navigate(f"http://127.0.0.1:{port}/")
        session.wait_for("document.querySelectorAll('.asset-card').length > 0", timeout=30)
        session.evaluate("document.querySelector('.asset-card').click()")
        session.wait_for("!!document.getElementById('note-input')", timeout=15)

        typed = "Lên phà lúc 5 giờ sáng"
        status_probe = "/(Đang chờ lưu…|Đang lưu…|Đã lưu)/.test(document.body.innerText)"
        typed_ok = False
        last_value = ""
        for attempt in range(5):
            session.call("Page.bringToFront")
            session.evaluate("document.getElementById('note-input').focus()")
            time.sleep(0.15)
            session.call("Input.insertText", {"text": typed})
            time.sleep(0.35)
            last_value = session.evaluate("document.getElementById('note-input').value")
            if last_value == typed and session.evaluate(status_probe):
                typed_ok = True
                break
            # Reset DOM + React state before retrying so text cannot accumulate.
            session.evaluate(
                "(()=>{const el=document.getElementById('note-input');"
                "const set=Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set;"
                "set.call(el,'');el.dispatchEvent(new InputEvent('input',{bubbles:true,data:''}));})()"
            )
            time.sleep(0.2)
        if not typed_ok:
            raise RuntimeError(f"typing did not drive React (value={last_value!r})")


        # Poll the API as the source of truth, then require the UI to confirm it.
        deadline = time.monotonic() + 20
        notes: list = []
        while time.monotonic() < deadline:
            notes = api(port, "GET", f"/api/v1/projects/{project_id}/annotations")["items"]
            if notes:
                break
            time.sleep(0.2)
        if not notes:
            result["debug_body"] = session.evaluate("document.body.innerText")
            raise RuntimeError("note was never persisted to SQLite")
        session.wait_for("document.body.innerText.includes('Đã lưu')", timeout=20)
        result["dom_has_saved_badge"] = bool(
            session.evaluate("document.body.innerText.includes('Đã lưu')")
        )
        notes = api(port, "GET", f"/api/v1/projects/{project_id}/annotations")["items"]
        result["note_after_save"] = notes

        clicked = session.evaluate(
            "(() => {const b=[...document.querySelectorAll('button')].find(x=>x.textContent.includes('Dùng 05:00'));"
            "if(!b) return false; b.click(); return true;})()"
        )
        if not clicked:
            raise RuntimeError("wall-clock suggestion button not found")
        session.wait_for("document.body.innerText.includes('Giờ thực tế do bạn xác nhận: 05:00')", timeout=20)

        notes = api(port, "GET", f"/api/v1/projects/{project_id}/annotations")["items"]
        result["note_after_suggestion"] = notes
        annotation = notes[0] if notes else {}
        result["checks"] = {
            "note_text": annotation.get("text") == typed,
            "wall_clock_0500": (annotation.get("wall_clock") or {}).get("time_of_day") == "05:00",
            "start_ms_null": annotation.get("start_ms") is None,
            "scope_clip": annotation.get("scope") == "clip",
            "revision": annotation.get("revision"),
        }

        # --- flush-on-switch: type on clip 2 and switch away before the debounce ---
        session.evaluate("document.activeElement && document.activeElement.blur()")
        session.call(
            "Input.dispatchKeyEvent",
            {"type": "keyDown", "key": "]", "code": "BracketRight", "windowsVirtualKeyCode": 221},
        )
        session.call("Input.dispatchKeyEvent", {"type": "keyUp", "key": "]", "code": "BracketRight"})
        session.wait_for("document.querySelector('.note-target').textContent.includes('C0002')", timeout=15)
        second_empty = session.evaluate("document.getElementById('note-input').value === ''")

        session.call("Page.bringToFront")
        session.evaluate("document.getElementById('note-input').focus()")
        time.sleep(0.15)
        session.call("Input.insertText", {"text": "Ghi chú clip hai"})
        time.sleep(0.1)  # deliberately inside the 450 ms debounce window
        session.evaluate("document.activeElement && document.activeElement.blur()")
        session.call(
            "Input.dispatchKeyEvent",
            {"type": "keyDown", "key": "[", "code": "BracketLeft", "windowsVirtualKeyCode": 219},
        )
        session.call("Input.dispatchKeyEvent", {"type": "keyUp", "key": "[", "code": "BracketLeft"})
        session.wait_for("document.querySelector('.note-target').textContent.includes('C0001')", timeout=15)
        # The note is fetched asynchronously after the switch; wait for it rather
        # than sampling the textarea mid-load.
        session.wait_for(
            "document.getElementById('note-input').value === %s" % json.dumps(typed),
            timeout=15,
        )
        restored = session.evaluate("document.getElementById('note-input').value")

        deadline = time.monotonic() + 15
        final_notes: list = []
        while time.monotonic() < deadline:
            final_notes = api(port, "GET", f"/api/v1/projects/{project_id}/annotations")["items"]
            if len(final_notes) >= 2:
                break
            time.sleep(0.2)
        texts = {note.get("text") for note in final_notes}
        assets = {note.get("asset_id") for note in final_notes}
        result["flush_checks"] = {
            "clip2_starts_empty": bool(second_empty),
            "clip2_note_flushed": "Ghi chú clip hai" in texts,
            "two_distinct_assets": len(assets) >= 2,
            "clip1_text_restored": restored == typed,
        }

        shot = session.call("Page.captureScreenshot", {"format": "png"})
        screenshot = OUT / "ui_flow.png"
        screenshot.write_bytes(base64.b64decode(shot["data"]))
        result["screenshot"] = str(screenshot)
        result["passed"] = all(
            bool(value) for key, value in result["checks"].items() if key != "revision"
        ) and all(result["flush_checks"].values())
    except Exception as exc:  # noqa: BLE001 - E2E must report, not crash silently
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["passed"] = False
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:  # noqa: BLE001 - cleanup must always continue
                pass
        for process in (chrome, server):
            if process is not None and process.poll() is None:
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                    process.wait(timeout=5)
        shutil.rmtree(data_root, ignore_errors=True)
        shutil.rmtree(chrome_profile, ignore_errors=True)

    result["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    (OUT / "ui_flow.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
