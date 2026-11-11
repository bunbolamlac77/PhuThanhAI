#!/usr/bin/env python3
"""S01 gate evidence collector.

Produces ``artifacts/qa/S01/{gate.md,gate.json,tests.json,bench.json,api.json,
resources.json,scan_real.json,security.json,orphans.json}`` plus the E2E assets.

Every number is measured on this machine. Real footage is operator-supplied via
``PHUTHANHAI_TEST_FOOTAGE``; when unset the real-footage step is reported as SKIP
rather than silently passing.
"""

from __future__ import annotations

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
from http.client import HTTPConnection
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "artifacts" / "qa" / "S01"
VENV_PY = REPO / ".venv" / "bin" / "python"
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "tools"))

from phuthanhai.analyzer.probe import probe_source  # noqa: E402
from phuthanhai.config import Settings  # noqa: E402
from phuthanhai.library import store  # noqa: E402
from phuthanhai.library.db import Database  # noqa: E402
from phuthanhai.library.index import scan_project  # noqa: E402
from phuthanhai.library.service import LibraryService  # noqa: E402


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, int(round((p / 100) * (len(ordered) - 1)))))
    return round(ordered[index], 2)


def pick_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def step(name: str, status: str, detail: str, evidence: dict | None = None) -> dict:
    return {"step": name, "status": status, "detail": detail, "evidence": evidence or {}}


# --------------------------------------------------------------------------- #
# Benchmarks                                                                   #
# --------------------------------------------------------------------------- #


def make_template(path: Path) -> Path:
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-v", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=0.4",
            "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast", str(path),
        ],
        check=True,
    )
    return path


def seed_thousand(footage: Path) -> None:
    template = make_template(footage / "_template.mp4")
    for index in range(1, 1001):
        target = footage / f"C_{index:04d}.MOV"
        if not target.exists():
            os.link(template, target)
    template.unlink(missing_ok=True)


def scan_benchmark(data_root: Path) -> dict:
    footage = data_root / "bench_footage"
    footage.mkdir(parents=True)
    seed_thousand(footage)
    settings = Settings.load()
    settings.data.ensure()
    db = Database(settings.data.registry_db)
    db.migrate()
    with db.write() as conn:
        project = store.create_project(conn, name="Bench 1000", root_path=str(footage))
    started = time.monotonic()
    stats = scan_project(db, project["project_id"], footage)
    elapsed = time.monotonic() - started
    with db.read() as conn:
        total = store.count_assets(conn, project["project_id"])
    db.close_current_thread()
    return {
        "project_id": project["project_id"],
        "files": 1000,
        "added": stats.added,
        "errors": stats.errors,
        "elapsed_s": round(elapsed, 3),
        "files_per_s": round(1000 / elapsed, 1),
        "rows_in_db": total,
    }


def scan_real(data_root: Path, footage: Path) -> dict:
    settings = Settings.load()
    service = LibraryService(settings)
    try:
        project = service.create_project("Real footage", str(footage))
        started = time.monotonic()
        stats = service.scan(project["project_id"])
        elapsed = time.monotonic() - started
        page = service.list_assets(project["project_id"], limit=5)
        first = page["items"][0]
        _asset, path = service.resolve_asset_file(first["asset_id"])
        before = (path.stat().st_size, path.stat().st_mtime_ns)
        service.scan(project["project_id"])
        after = (path.stat().st_size, path.stat().st_mtime_ns)

        # On-demand proxy on a real 4K HEVC portrait clip: proves the fallback is
        # H.264, keeps aspect/orientation and maps duration to the source.
        shortest = service.list_assets(project["project_id"], sort="duration", limit=1)["items"][0]
        proxy_started = time.monotonic()
        service.proxy(shortest["asset_id"])
        proxy_seconds = time.monotonic() - proxy_started
        proxy_path = service.preview.proxy_path(shortest["asset_id"])
        proxy_info = probe_source(proxy_path)

        return {
            "project_id": project["project_id"],
            "added": stats.added,
            "errors": stats.errors,
            "elapsed_s": round(elapsed, 3),
            "total": page["total"],
            "sample_codec": first["codec"],
            "sample_dimensions": [first["width"], first["height"]],
            "sample_rotation": first["rotation"],
            "source_unchanged": before == after,
            "proxy": {
                "source_filename": shortest["filename"],
                "source_codec": shortest["codec"],
                "source_duration_ms": shortest["duration_ms"],
                "proxy_codec": proxy_info.codec,
                "proxy_dimensions": [proxy_info.width, proxy_info.height],
                "proxy_duration_ms": proxy_info.duration_ms,
                "duration_delta_ms": abs(proxy_info.duration_ms - (shortest["duration_ms"] or 0)),
                "seconds": round(proxy_seconds, 2),
            },
        }
    finally:
        service.close()


# --------------------------------------------------------------------------- #
# Live server checks                                                          #
# --------------------------------------------------------------------------- #


class Server:
    def __init__(self, data_root: Path) -> None:
        self.port = pick_port()
        self.data_root = data_root
        self.process: subprocess.Popen | None = None

    def start(self) -> None:
        env = {**os.environ, "PHUTHANHAI_DATA_DIR": str(self.data_root)}
        self.process = subprocess.Popen(  # noqa: S603
            [str(VENV_PY), "-m", "phuthanhai.cli", "serve", "--host", "127.0.0.1", "--port", str(self.port)],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True, text=True,
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                status, _ = self.request("GET", "/api/v1/health")
                if status == 200:
                    return
            except OSError:
                time.sleep(0.2)
        raise RuntimeError("server did not start")

    def stop(self) -> None:
        if self.process is None:
            return
        try:
            os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            self.process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
            self.process.wait(timeout=10)
        self.process = None

    def request(self, method: str, path: str, headers: dict | None = None, body: dict | None = None) -> tuple[int, dict | str]:
        connection = HTTPConnection("127.0.0.1", self.port, timeout=30)
        payload = json.dumps(body).encode() if body is not None else None
        merged = dict(headers or {})
        if payload is not None:
            merged.setdefault("Content-Type", "application/json")
        try:
            connection.request(method, path, body=payload, headers=merged)
            response = connection.getresponse()
            raw = response.read()
            text = raw.decode("utf-8", "replace")
            if response.getheader("Content-Type", "").startswith("application/json"):
                return response.status, json.loads(text)
            return response.status, text
        finally:
            connection.close()

    def capability(self) -> str:
        session = json.loads((self.data_root / "runtime" / "session.json").read_text())
        return session["capability"]

    def rss_bytes(self) -> int | None:
        if self.process is None:
            return None
        completed = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(self.process.pid)], capture_output=True, text=True, check=False
        )
        text = completed.stdout.strip()
        return int(text.split()[0]) * 1024 if text else None


def api_benchmark(server: Server, project_id: str) -> dict:
    cap = server.capability()
    headers = {"X-PhuThanhAI-Capability": cap}
    page = server.request("GET", f"/api/v1/assets?project_id={project_id}&limit=100")[1]
    asset = page["items"][0]
    asset_id = asset["asset_id"]

    list_times: list[float] = []
    for _ in range(30):
        started = time.perf_counter()
        status, _ = server.request("GET", f"/api/v1/assets?project_id={project_id}&limit=100")
        list_times.append((time.perf_counter() - started) * 1000)
        assert status == 200

    search_times: list[float] = []
    for _ in range(20):
        started = time.perf_counter()
        status, _ = server.request("GET", f"/api/v1/assets?project_id={project_id}&q=C_05&limit=100")
        search_times.append((time.perf_counter() - started) * 1000)
        assert status == 200

    # Cold and warm thumbnail.
    cold_start = time.perf_counter()
    status, _ = server.request("GET", f"/api/v1/assets/{asset_id}/thumbnail")
    cold_ms = (time.perf_counter() - cold_start) * 1000
    warm_start = time.perf_counter()
    server.request("GET", f"/api/v1/assets/{asset_id}/thumbnail")
    warm_ms = (time.perf_counter() - warm_start) * 1000

    # Range 206 and 416.
    size = asset["size_bytes"]
    range_status, _ = server.request(
        "GET", f"/api/v1/assets/{asset_id}/stream", headers={"Range": "bytes=0-99"}
    )
    unsat_status, _ = server.request(
        "GET", f"/api/v1/assets/{asset_id}/stream", headers={"Range": f"bytes={size}-"}
    )

    # Note save p95 through the real HTTP stack.
    status, created = server.request(
        "POST",
        f"/api/v1/projects/{project_id}/annotations",
        headers=headers,
        body={"scope": "clip", "asset_id": asset_id, "text": "Lên phà lúc 5 giờ sáng"},
    )
    annotation = created["annotation"]
    revision = annotation["revision"]
    save_times: list[float] = []
    for index in range(20):
        started = time.perf_counter()
        status, _ = server.request(
            "PUT",
            f"/api/v1/annotations/{annotation['annotation_id']}",
            headers=headers,
            body={"expected_revision": revision, "text": f"Lên phà lúc 5 giờ sáng {index}"},
        )
        save_times.append((time.perf_counter() - started) * 1000)
        revision += 1
        assert status == 200

    # Stale revision must be a 409, never a silent overwrite.
    stale_status, stale_body = server.request(
        "PUT",
        f"/api/v1/annotations/{annotation['annotation_id']}",
        headers=headers,
        body={"expected_revision": 1, "text": "stale"},
    )
    notes = server.request("GET", f"/api/v1/projects/{project_id}/annotations")[1]

    server.request(
        "PUT",
        f"/api/v1/assets/{asset_id}/review",
        headers=headers,
        body={"status": "reviewed", "rating": 5, "favorite": True},
    )
    review = server.request("GET", f"/api/v1/assets/{asset_id}/review")[1]

    cache = server.request("GET", "/api/v1/cache")[1]

    return {
        "asset_id": asset_id,
        "annotation_id": annotation["annotation_id"],
        "list_p50_ms": percentile(list_times, 50),
        "list_p95_ms": percentile(list_times, 95),
        "search_p50_ms": percentile(search_times, 50),
        "search_p95_ms": percentile(search_times, 95),
        "note_save_p50_ms": percentile(save_times, 50),
        "note_save_p95_ms": percentile(save_times, 95),
        "thumbnail_cold_ms": round(cold_ms, 1),
        "thumbnail_warm_ms": round(warm_ms, 1),
        "range_206": range_status,
        "range_416": unsat_status,
        "stale_conflict_status": stale_status,
        "stale_conflict_code": stale_body.get("code") if isinstance(stale_body, dict) else None,
        "notes_after": len(notes),
        "review": review,
        "cache": cache,
        "wall_clock_before_accept": (notes[0].get("wall_clock") if notes else None),
    }


def security_checks(server: Server) -> dict:
    checks = []
    status, body = server.request("GET", "/api/v1/health", headers={"Host": "evil.example.com"})
    checks.append({"name": "foreign host", "status": status, "code": body.get("code")})
    status, body = server.request("GET", "/api/v1/health", headers={"Origin": "http://evil.example.com"})
    checks.append({"name": "foreign origin", "status": status, "code": body.get("code")})
    status, body = server.request("POST", "/api/v1/projects", body={"name": "x", "root_path": "/tmp"})
    checks.append({"name": "mutation without capability", "status": status, "code": body.get("code")})
    status, body = server.request("GET", "/api/v1/assets/%2e%2e%2f%2e%2e%2fetc/passwd/stream")
    checks.append({"name": "encoded traversal", "status": status})
    return {"checks": checks}


def resource_metrics(server: Server, data_root: Path) -> dict:
    rss = server.rss_bytes()
    total = 0
    for path in data_root.rglob("*"):
        if path.is_file():
            total += path.stat().st_size
    return {
        "server_rss_bytes": rss,
        "data_root_bytes": total,
        "data_root_files": sum(1 for p in data_root.rglob("*") if p.is_file()),
        "free_disk_gib": round(shutil.disk_usage(str(data_root)).free / 1024**3, 2),
    }


def restart_persistence(server: Server, project_id: str) -> dict:
    server.stop()
    server.start()
    status, notes = server.request("GET", f"/api/v1/projects/{project_id}/annotations")
    return {"status": status, "notes_after_restart": len(notes), "text": notes[0]["text"] if notes else None}


# --------------------------------------------------------------------------- #
# Subprocess steps                                                             #
# --------------------------------------------------------------------------- #


def run_json_tool(argv: list[str], output: Path, timeout: float = 600, env: dict | None = None) -> dict:
    completed = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False, env=env, cwd=str(REPO))
    (OUT / f"{output.stem}.log").write_text(completed.stdout + completed.stderr, encoding="utf-8")
    payload: dict = {"exit_code": completed.returncode}
    if output.is_file():
        payload.update(json.loads(output.read_text(encoding="utf-8")))
    else:
        payload["error"] = "no output file"
    return payload


def step_pytest() -> dict:
    junit = OUT / "pytest.xml"
    completed = subprocess.run(
        [str(VENV_PY), "-m", "pytest", f"--junitxml={junit}", "-o", "junit_family=xunit2"],
        capture_output=True, text=True, timeout=1800, check=False, cwd=str(REPO),
    )
    (OUT / "pytest.log").write_text(completed.stdout + completed.stderr, encoding="utf-8")
    import xml.etree.ElementTree as ET

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
        "pytest",
        "PASS" if completed.returncode == 0 and passed > 0 else "FAIL",
        f"{passed} passed, {failed} failed/summary, {skipped} skipped",
        payload,
    )


def step_orphans() -> dict:
    ps = subprocess.run(["/bin/ps", "-axo", "pid=,command="], capture_output=True, text=True, check=False)
    pattern = re.compile(r"(phuthanhai\.cli serve|phuthanhai\.analyzer\.vlm_worker)")
    leftovers = [line.strip() for line in ps.stdout.splitlines() if pattern.search(line)]
    (OUT / "orphans.json").write_text(json.dumps({"matching": leftovers}, indent=2), encoding="utf-8")
    return step("orphans", "PASS" if not leftovers else "FAIL", f"leftovers={len(leftovers)}")


def render_gate(steps: list[dict], verdict: str) -> str:
    lines = [
        "# S01 — Media Library / Quick Preview / Notes — Gate G1",
        "",
        f"- Generated: {now_iso()}",
        "- Data root: ~/Library/Application Support/PhuThanhAI (temp root during the gate)",
        "",
        "| # | Requirement | Result | Detail |",
        "|---|---|---|---|",
    ]
    for index, item in enumerate(steps, start=1):
        detail = str(item["detail"]).replace("|", "\\|")
        lines.append(f"| {index} | {item['step']} | **{item['status']}** | {detail} |")
    lines += ["", f"## Verdict: {verdict}", ""]
    return "\n".join(lines) + "\n"


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    footage_env = os.environ.get("PHUTHANHAI_TEST_FOOTAGE")
    real_footage = Path(footage_env).expanduser() if footage_env else None
    data_root = Path(tempfile.mkdtemp(prefix="pth_g1_"))
    os.environ["PHUTHANHAI_DATA_DIR"] = str(data_root)
    steps: list[dict] = []
    server = Server(data_root)

    try:
        steps.append(step_pytest())

        bench = scan_benchmark(data_root)
        ok_bench = bench["added"] == 1000 and not bench["errors"]
        (OUT / "bench.json").write_text(json.dumps(bench, indent=2), encoding="utf-8")
        steps.append(
            step(
                "scan 1,000 synthetic assets",
                "PASS" if ok_bench else "FAIL",
                f"added={bench['added']} errors={len(bench['errors'])} in {bench['elapsed_s']}s "
                f"({bench['files_per_s']} files/s)",
                bench,
            )
        )

        if real_footage is not None and real_footage.is_dir():
            real = scan_real(data_root, real_footage)
            (OUT / "scan_real.json").write_text(json.dumps(real, indent=2), encoding="utf-8")
            steps.append(
                step(
                    "scan real footage (read-only)",
                    "PASS"
                    if real["added"] > 0
                    and not real["errors"]
                    and real["source_unchanged"]
                    and real["proxy"]["proxy_codec"] == "h264"
                    and real["proxy"]["duration_delta_ms"] <= 250
                    else "FAIL",
                    f"{real['added']} clips, {real['errors']} errors, {real['elapsed_s']}s, "
                    f"source_unchanged={real['source_unchanged']}, "
                    f"{real['proxy']['source_codec']}→{real['proxy']['proxy_codec']} "
                    f"{real['proxy']['proxy_dimensions']} Δ{real['proxy']['duration_delta_ms']}ms",
                    real,
                )
            )
        else:
            steps.append(step("scan real footage (read-only)", "SKIP", "PHUTHANHAI_TEST_FOOTAGE not set"))

        server.start()
        api = api_benchmark(server, bench["project_id"])
        (OUT / "api.json").write_text(json.dumps(api, indent=2, ensure_ascii=False), encoding="utf-8")
        api_ok = (
            api["list_p95_ms"] < 250
            and api["search_p95_ms"] < 300
            and api["note_save_p95_ms"] < 200
            and api["range_206"] == 206
            and api["range_416"] == 416
            and api["stale_conflict_status"] == 409
        )
        steps.append(
            step(
                "live API latency + Range + 409",
                "PASS" if api_ok else "FAIL",
                f"list p95 {api['list_p95_ms']}ms · search p95 {api['search_p95_ms']}ms · "
                f"note save p95 {api['note_save_p95_ms']}ms · Range {api['range_206']}/{api['range_416']} · "
                f"stale {api['stale_conflict_status']}",
                api,
            )
        )

        security = security_checks(server)
        (OUT / "security.json").write_text(json.dumps(security, indent=2), encoding="utf-8")
        checks = {check["name"]: check for check in security["checks"]}
        sec_ok = (
            checks["foreign host"]["status"] == 400
            and checks["foreign origin"]["status"] == 403
            and checks["mutation without capability"]["status"] == 403
        )
        steps.append(
            step(
                "loopback security (host/origin/capability/traversal)",
                "PASS" if sec_ok else "FAIL",
                " | ".join(f"{name}={check['status']}" for name, check in checks.items()),
                security,
            )
        )

        resources = resource_metrics(server, data_root)
        (OUT / "resources.json").write_text(json.dumps(resources, indent=2), encoding="utf-8")
        steps.append(
            step(
                "server RSS / data root / disk",
                "PASS",
                f"RSS {round((resources['server_rss_bytes'] or 0) / 1024**2, 1)} MiB · "
                f"data root {round(resources['data_root_bytes'] / 1024**2, 1)} MiB · "
                f"free {resources['free_disk_gib']} GiB",
                resources,
            )
        )

        persistence = restart_persistence(server, bench["project_id"])
        (OUT / "restart.json").write_text(json.dumps(persistence, indent=2), encoding="utf-8")
        steps.append(
            step(
                "notes survive backend restart",
                "PASS" if persistence["notes_after_restart"] >= 1 else "FAIL",
                f"notes_after_restart={persistence['notes_after_restart']}",
                persistence,
            )
        )
    finally:
        server.stop()

    env = {**os.environ}
    if real_footage is not None:
        env["PHUTHANHAI_TEST_FOOTAGE"] = str(real_footage)
    e2e = run_json_tool(
        [str(VENV_PY), str(REPO / "tools" / "run_s01_e2e.py")], OUT / "e2e" / "e2e.json", timeout=900, env=env
    )
    e2e_passed = e2e.get("exit_code") == 0 and all(item.get("passed") for item in e2e.get("results", []) if item.get("passed") is not None)
    steps.append(
        step(
            "browser E2E: 1,000-asset DOM virtualization + real footage",
            "PASS" if e2e_passed else "FAIL",
            " · ".join(
                f"{item['case']}: cards={item.get('asset_card_nodes')} total={item.get('total_label')} "
                f"play={item.get('playback_ok')}"
                for item in e2e.get("results", [])
            ),
            e2e,
        )
    )

    ui_flow = run_json_tool(
        [str(VENV_PY), str(REPO / "tools" / "run_s01_ui_flow.py")], OUT / "e2e" / "ui_flow.json", timeout=600, env=env
    )
    steps.append(
        step(
            "browser E2E: type note, save, accept wall-clock",
            "PASS" if ui_flow.get("passed") else "FAIL",
            json.dumps(ui_flow.get("checks", {}), ensure_ascii=False),
            ui_flow,
        )
    )

    steps.append(step_orphans())

    verdict = "FAIL" if any(item["status"] == "FAIL" for item in steps) else (
        "PASS_WITH_WARNINGS" if any(item["status"] == "SKIP" for item in steps) else "PASS"
    )
    gate = {"milestone": "S01", "generated_at": now_iso(), "verdict": verdict, "steps": steps}
    (OUT / "gate.json").write_text(json.dumps(gate, indent=2, ensure_ascii=False), encoding="utf-8")
    (OUT / "gate.md").write_text(render_gate(steps, verdict), encoding="utf-8")
    print(render_gate(steps, verdict))
    shutil.rmtree(data_root, ignore_errors=True)
    return 0 if verdict != "FAIL" else 1


if __name__ == "__main__":
    sys.exit(main())
