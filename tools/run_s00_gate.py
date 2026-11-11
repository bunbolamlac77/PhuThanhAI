#!/usr/bin/env python3
"""S00 gate evidence collector.

Runs real commands on this machine and writes machine-readable artifacts to
``artifacts/qa/S00/``. It never fakes a result: every step records the argv, exit
code, elapsed time and captured output, and the verdict is derived from those
records.

Usage:
    python3 tools/run_s00_gate.py [--source /path/to/footage.mov] [--skip-model]
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "artifacts" / "qa" / "S00"
VENV_PY = REPO / ".venv" / "bin" / "python"
SRC = REPO / "src"
SANDBOX_PROFILE = "(version 1)(allow default)(deny network*)"
SANDBOX = ["/usr/bin/sandbox-exec", "-p", SANDBOX_PROFILE]
#: Path to REAL footage for the frame smoke. Deliberately not committed: a media
#: path is private operational data, so it comes from the operator.
SOURCE_ENV = "PHUTHANHAI_SMOKE_SOURCE"


class Recorder:
    """Tees every command and result into commands.log."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("", encoding="utf-8")

    def log(self, text: str) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(text.rstrip() + "\n")

    def run(
        self,
        argv: list[str],
        *,
        timeout: float = 600,
        env: dict[str, str] | None = None,
        stdin: bytes | None = None,
        label: str | None = None,
    ) -> dict[str, Any]:
        started = time.monotonic()
        self.log(f"$ {' '.join(argv)}")
        completed = subprocess.run(  # noqa: S603 - argv lists only, never a shell
            argv,
            capture_output=True,
            timeout=timeout,
            env=env,
            input=stdin,
            check=False,
        )
        elapsed = round(time.monotonic() - started, 3)
        result = {
            "label": label or argv[-1],
            "argv": argv,
            "exit_code": completed.returncode,
            "elapsed_s": elapsed,
            "stdout": completed.stdout.decode("utf-8", "replace"),
            "stderr": completed.stderr.decode("utf-8", "replace"),
        }
        self.log(f"  exit={completed.returncode} elapsed={elapsed}s")
        if result["stdout"].strip():
            self.log("  stdout: " + result["stdout"].strip().splitlines()[-1][:400])
        if result["stderr"].strip():
            self.log("  stderr: " + result["stderr"].strip().splitlines()[-1][:400])
        return result


def step(name: str, status: str, detail: str, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "step": name,
        "status": status,
        "detail": detail,
        "evidence": evidence or {},
    }


def now_iso() -> str:
    return datetime.now(tz=timezone.utc).astimezone().isoformat(timespec="seconds")


def port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def pick_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# --------------------------------------------------------------------------- #
# Steps                                                                        #
# --------------------------------------------------------------------------- #


def step_doctor(rec: Recorder) -> dict[str, Any]:
    result = rec.run([str(VENV_PY), "-m", "phuthanhai.cli", "doctor", "--offline", "--json"], timeout=300)
    (OUT / "doctor.json").write_text(result["stdout"], encoding="utf-8")
    if result["exit_code"] != 0:
        return step("doctor", "FAIL", f"doctor exited {result['exit_code']}", {"stderr": result["stderr"][-2000:]})
    report = json.loads(result["stdout"])
    checks = {check["id"]: check for check in report["checks"]}
    hard = [
        "platform",
        "python_app",
        "data_root",
        "ffmpeg",
        "ffprobe",
        "model_visual_analysis_primary",
        "mlx_runtime",
        "offline_env",
        "security_bind",
        "contracts",
    ]
    failing = [key for key in hard if checks.get(key, {}).get("status") != "PASS"]
    status = "FAIL" if failing else ("WARN" if report["warnings"] else "PASS")
    detail = (
        f"status={report['status']} hard_checks_failing={failing or 'none'} "
        f"warnings={report['warnings']}"
    )
    return step("doctor", status, detail, {"report_status": report["status"], "checks": checks})


def step_doctor_deep(rec: Recorder) -> dict[str, Any]:
    result = rec.run(
        [str(VENV_PY), "-m", "phuthanhai.cli", "doctor", "--offline", "--deep", "--json"], timeout=900
    )
    (OUT / "doctor_deep.json").write_text(result["stdout"], encoding="utf-8")
    if result["exit_code"] != 0:
        return step("doctor_deep", "FAIL", f"deep doctor exited {result['exit_code']}")
    report = json.loads(result["stdout"])
    visual = next(c for c in report["checks"] if c["id"] == "model_visual_analysis_primary")
    verification = visual["evidence"].get("hash_verification", {})
    verified = verification.get("status") == "verified"
    checked = verification.get("checked", {})
    return step(
        "doctor_deep",
        "PASS" if verified else "FAIL",
        f"visual model hash verification={verification.get('status')} files={len(checked)}",
        {"hash_status": verification.get("status"), "checked_files": sorted(checked), "mismatches": verification.get("mismatches")},
    )


def step_contract_tool(rec: Recorder) -> dict[str, Any]:
    result = rec.run([str(VENV_PY), str(REPO / "tools" / "validate_specs.py")], timeout=180)
    (OUT / "validate_specs.log").write_text(result["stdout"] + result["stderr"], encoding="utf-8")
    required = [
        "PASS V2 legacy fixture and V3 golden fixture",
        "PASS V3 portable MD exact roundtrip",
        "PASS tamper hash detection",
    ]
    missing = [line for line in required if line not in result["stdout"]]
    status = "PASS" if result["exit_code"] == 0 and not missing else "FAIL"
    return step("contracts_tool", status, f"exit={result['exit_code']} missing_markers={missing}", {"stdout": result["stdout"]})


def step_pytest(rec: Recorder) -> dict[str, Any]:
    junit = OUT / "pytest.xml"
    result = rec.run(
        [str(VENV_PY), "-m", "pytest", f"--junitxml={junit}", "-o", "junit_family=xunit2"],
        timeout=900,
    )
    (OUT / "pytest.log").write_text(result["stdout"] + result["stderr"], encoding="utf-8")

    passed = failed = skipped = 0
    summary = ""
    if junit.is_file():
        import xml.etree.ElementTree as ET

        root = ET.parse(junit).getroot()
        suites = [root] if root.tag == "testsuite" else list(root)
        for suite in suites:
            passed += int(suite.get("tests", 0)) - int(suite.get("failures", 0)) - int(suite.get("errors", 0)) - int(suite.get("skipped", 0))
            failed += int(suite.get("failures", 0)) + int(suite.get("errors", 0))
            skipped += int(suite.get("skipped", 0))
        summary = f"{passed} passed, {failed} failed, {skipped} skipped (junit)"
    else:
        for line in reversed(result["stdout"].strip().splitlines()):
            if "passed" in line or "failed" in line:
                summary = line.strip()
                break
        match = re.search(r"(\d+) passed", summary)
        passed = int(match.group(1)) if match else 0
        failed = 1 if re.search(r"(\d+) failed", summary) else 0

    status = "PASS" if result["exit_code"] == 0 and passed > 0 and failed == 0 else "FAIL"
    payload = {
        "exit_code": result["exit_code"],
        "passed": passed,
        "skipped": skipped,
        "failed": failed,
        "summary": summary,
    }
    (OUT / "tests.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return step("pytest", status, summary or "no summary parsed", payload)


def step_network(rec: Recorder) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    curl = rec.run(SANDBOX + ["/usr/bin/curl", "-s", "-m", "2", "https://example.com"], timeout=30)
    checks.append({"name": "sandboxed curl to https", "exit_code": curl["exit_code"], "blocked": curl["exit_code"] != 0})

    sock = rec.run(
        SANDBOX
        + [
            sys.executable,
            "-c",
            "import socket;socket.create_connection(('1.1.1.1',443),timeout=2)",
        ],
        timeout=30,
    )
    checks.append(
        {"name": "sandboxed TCP connect to 1.1.1.1:443", "exit_code": sock["exit_code"], "blocked": sock["exit_code"] != 0}
    )

    # Control: the same interpreter without the sandbox, proving the test is meaningful
    # only if the network is otherwise reachable. No payload is fetched.
    control = rec.run(
        [sys.executable, "-c", "import socket;s=socket.create_connection(('1.1.1.1',443),timeout=2);print('connected');s.close()"],
        timeout=30,
    )
    checks.append(
        {
            "name": "unsandboxed TCP control",
            "exit_code": control["exit_code"],
            "reachable": control["exit_code"] == 0,
        }
    )

    blocked = all(item["blocked"] for item in checks[:2])
    payload = {"profile": SANDBOX_PROFILE, "checks": checks, "network_blocked_in_sandbox": blocked}
    (OUT / "network_check.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return step(
        "network_isolation",
        "PASS" if blocked else "FAIL",
        f"blocked_in_sandbox={blocked} control_reachable={checks[2]['reachable']}",
        payload,
    )


def step_web_bundle() -> dict[str, Any]:
    dist = REPO / "web" / "dist"
    index = dist / "index.html"
    if not index.is_file():
        return step("web_bundle", "FAIL", "web/dist/index.html missing (run npm run build)")
    html = index.read_text(encoding="utf-8")
    remote = re.findall(r"(?:src|href)=\"(https?://[^\"]+)\"", html)
    assets = sorted(p.name for p in (dist / "assets").glob("*"))

    # Distinguish real network entry points from inert strings. React embeds
    # documentation links (reactjs.org) in error messages and SVG namespace URIs
    # (www.w3.org/2000/svg) appear in markup; neither is fetched.
    fetch_patterns = re.compile(r"(?:fetch|import|Worker|EventSource|WebSocket)\(\s*[\"']https?://")
    doc_hosts = ("reactjs.org", "react.dev", "www.w3.org")
    fetched: list[str] = []
    inert: list[str] = []
    for path in (dist / "assets").glob("*.js"):
        text = path.read_text(encoding="utf-8", errors="replace")
        fetched.extend(fetch_patterns.findall(text))
        for url in re.findall(r"https?://[a-zA-Z0-9._-]+", text):
            if any(host in url for host in doc_hosts):
                inert.append(url)

    ok = not remote and not fetched
    payload = {
        "index_present": True,
        "assets": assets,
        "remote_attributes_in_html": remote,
        "network_fetch_calls_absolute": fetched,
        "inert_documentation_or_namespace_urls": len(inert),
        "note": "Only ./assets/* are loaded. Inert URL strings are React error-doc text and SVG namespaces; CSP forbids remote loads anyway.",
    }
    return step("web_bundle", "PASS" if ok else "FAIL", f"assets={len(assets)} remote_attrs={len(remote)} remote_fetches={len(fetched)}", payload)


def step_swift() -> dict[str, Any]:
    binary = REPO / "macos" / "PhuThanhAI" / ".build-direct" / "PhuThanhAI"
    if not binary.is_file():
        return step("swift_launcher", "WARN", "launcher binary not built")
    file_out = subprocess.run(["/usr/bin/file", str(binary)], capture_output=True, text=True, check=False)
    ok = "Mach-O 64-bit executable arm64" in file_out.stdout
    return step(
        "swift_launcher",
        "PASS" if ok else "FAIL",
        file_out.stdout.strip(),
        {"binary": str(binary.relative_to(REPO)), "size_bytes": binary.stat().st_size},
    )


def step_vlm_smoke(rec: Recorder, source: Path | None, *, skip: bool) -> dict[str, Any]:
    if skip:
        return step("vlm_smoke", "SKIP", "explicitly skipped by operator")
    if source is None:
        return step(
            "vlm_smoke",
            "FAIL",
            f"no footage supplied: pass --source <file> or set {SOURCE_ENV} "
            "(a media path is private and is never committed)",
        )
    if not source.is_file():
        return step("vlm_smoke", "FAIL", "source footage not found at the supplied path")

    sys.path.insert(0, str(SRC))
    from phuthanhai.analyzer.probe import probe_source
    from phuthanhai.analyzer.sampler import extract_frame
    from phuthanhai.analyzer.vlm_worker import VISUAL_PROMPT
    from phuthanhai.config import Settings

    settings = Settings.load()
    settings.data.ensure()
    frame_path = settings.data.sampling / "s00_smoke_frame.jpg"

    probe_started = time.monotonic()
    info = probe_source(source)
    frame = extract_frame(source, 5000, frame_path, max_edge=896)
    extract_s = round(time.monotonic() - probe_started, 3)

    request = {
        "request_version": "1.0.0",
        "job_id": "s00-gate-smoke",
        "model_path": str(Path.home() / "Library/Application Support/AIVideo/Models/visual_analysis_primary"),
        "image_path": str(frame.path),
        "allowed_image_root": str(settings.data.sampling),
        "prompt": VISUAL_PROMPT,
        "max_tokens": 320,
        "temperature": 0.0,
    }
    request_path = OUT / "smoke" / "request.json"
    request_path.parent.mkdir(parents=True, exist_ok=True)
    request_path.write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8")

    runtime = Path.home() / "Library/Application Support/AIVideo/Runtimes/g0-mlx-runtime/bin/python"
    env = {
        **os.environ,
        "PYTHONPATH": str(SRC),
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "TOKENIZERS_PARALLELISM": "false",
    }
    argv = SANDBOX + [str(runtime), "-m", "phuthanhai.analyzer.vlm_worker"]
    result = rec.run(argv, timeout=1200, env=env, stdin=request_path.read_bytes(), label="vlm_worker")

    (OUT / "smoke" / "worker_stdout.jsonl").write_text(result["stdout"], encoding="utf-8")
    (OUT / "smoke" / "worker_stderr.log").write_text(result["stderr"], encoding="utf-8")
    events = [json.loads(line) for line in result["stdout"].splitlines() if line.strip()]
    payload: dict[str, Any] = {
        "source": {"filename": source.name, "probed": info.to_dict()},
        "frame": {"time_ms": frame.time_ms, "width": frame.width, "height": frame.height, "bytes": frame.size_bytes, "sha256": frame.sha256},
        "extract_s": extract_s,
        "argv": argv,
        "sandbox": SANDBOX_PROFILE,
        "exit_code": result["exit_code"],
        "elapsed_s": result["elapsed_s"],
        "events": events,
        "stderr_tail": result["stderr"][-2000:],
    }
    result_event = next((event for event in events if event.get("type") == "result"), None)
    if result_event is None:
        (OUT / "vlm_smoke.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        error = next((event for event in events if event.get("type") == "error"), {})
        return step("vlm_smoke", "FAIL", f"no result event: {error.get('code')} {error.get('message')}", payload)

    payload["observation"] = result_event["observation"]
    payload["metrics"] = {
        "load_s": result_event.get("load_s"),
        "inference_s": result_event.get("elapsed_s"),
        "total_worker_s": result_event.get("total_s"),
        "peak_rss_bytes": result_event.get("peak_rss_bytes"),
        "mlx_peak_memory_bytes": result_event.get("mlx_peak_memory_bytes"),
        "generation_tokens": result_event.get("generation_tokens"),
        "prompt_tokens": result_event.get("prompt_tokens"),
        "versions": next(
            (e.get("versions") for e in events if e.get("type") == "worker_ready"), None
        ),
    }
    (OUT / "vlm_smoke.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    ok = (
        result["exit_code"] == 0
        and result_event["observation"]["description"].strip() != ""
        and result_event.get("peak_rss_bytes")
    )
    rss_gib = (result_event.get("peak_rss_bytes") or 0) / 1024**3
    mlx_gib = (result_event.get("mlx_peak_memory_bytes") or 0) / 1024**3
    return step(
        "vlm_smoke",
        "PASS" if ok else "FAIL",
        f"frame {frame.width}x{frame.height} · load {result_event.get('load_s')}s · "
        f"infer {result_event.get('elapsed_s')}s · peak RSS {rss_gib:.2f} GiB · "
        f"MLX peak {mlx_gib:.2f} GiB",
        payload,
    )


def step_server_e2e(rec: Recorder) -> dict[str, Any]:
    port = pick_port()
    argv = [
        str(VENV_PY),
        "-m",
        "phuthanhai.cli",
        "serve",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]
    rec.log(f"$ {' '.join(argv)}  (background)")
    popen = subprocess.Popen(  # noqa: S603
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        text=True,
    )
    checks: list[dict[str, Any]] = []
    try:
        ready = False
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                status, _body = _http(port, "GET", "/api/v1/health", {})
                if status == 200:
                    ready = True
                    break
            except OSError:
                time.sleep(0.25)
        if not ready:
            return step("server_e2e", "FAIL", "backend did not become ready within 30s")

        status, body = _http(port, "GET", "/api/v1/health", {})
        health = json.loads(body)
        checks.append({"name": "health", "status": status, "loopback_only": health["loopback_only"]})

        status, body = _http(port, "GET", "/api/v1/health", {"Host": "evil.example.com"})
        checks.append({"name": "bad host", "status": status, "code": json.loads(body).get("code")})

        status, body = _http(port, "GET", "/api/v1/health", {"Origin": "http://evil.example.com"})
        checks.append({"name": "bad origin", "status": status, "code": json.loads(body).get("code")})

        session_file = Path.home() / "Library/Application Support/PhuThanhAI/runtime/session.json"
        session = json.loads(session_file.read_text(encoding="utf-8"))
        checks.append(
            {
                "name": "session file",
                "status": 200,
                "mode": oct(session_file.stat().st_mode & 0o777),
                "capability_in_url": "capability" in session.get("origin", ""),
            }
        )

        status, body = _http(port, "POST", "/api/v1/contracts:recheck", {})
        checks.append({"name": "POST without capability", "status": status, "code": json.loads(body).get("code")})

        status, body = _http(
            port,
            "POST",
            "/api/v1/contracts:recheck",
            {"X-PhuThanhAI-Capability": session["capability"]},
        )
        checks.append({"name": "POST with capability", "status": status, "result": json.loads(body).get("status")})

        status, _body = _http(port, "GET", "/api/v1/assets/../../etc/passwd", {})
        checks.append({"name": "traversal", "status": status})

        status, body = _http(port, "GET", "/", {})
        checks.append({"name": "web root", "status": status, "html": "<div id=\"root\">" in body})
    finally:
        try:
            os.killpg(os.getpgid(popen.pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            popen.wait(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(popen.pid), signal.SIGKILL)
            popen.wait(timeout=10)

    time.sleep(0.5)
    session_removed = not (Path.home() / "Library/Application Support/PhuThanhAI/runtime/session.json").exists()
    port_freed = port_is_free(port)
    payload = {"port": port, "checks": checks, "session_removed": session_removed, "port_freed": port_freed}
    (OUT / "server_e2e.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")

    expected = {
        "health": 200,
        "bad host": 400,
        "bad origin": 403,
        "POST without capability": 403,
        "POST with capability": 200,
    }
    by_name = {check["name"]: check for check in checks}
    failures = [name for name, code in expected.items() if by_name.get(name, {}).get("status") != code]
    ok = not failures and session_removed and port_freed and by_name["traversal"]["status"] in (400, 404)
    return step(
        "server_e2e",
        "PASS" if ok else "FAIL",
        f"failures={failures or 'none'} session_removed={session_removed} port_freed={port_freed}",
        payload,
    )


def _http(port: int, method: str, path: str, headers: dict[str, str]) -> tuple[int, str]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request(method, path, headers=headers)
        response = connection.getresponse()
        body = response.read().decode("utf-8", "replace")
        return response.status, body
    finally:
        connection.close()


def step_orphans(rec: Recorder) -> dict[str, Any]:
    ps = subprocess.run(["/bin/ps", "-axo", "pid=,command="], capture_output=True, text=True, check=False)
    pattern = re.compile(r"(phuthanhai\.analyzer\.vlm_worker|phuthanhai\.cli serve)")
    leftovers = [
        line.strip()
        for line in ps.stdout.splitlines()
        if pattern.search(line) and "run_s00_gate" not in line
    ]
    payload = {"matching_processes": leftovers}
    (OUT / "orphans.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return step(
        "orphans",
        "PASS" if not leftovers else "FAIL",
        f"leftover_worker_or_server_processes={len(leftovers)}",
        payload,
    )


def step_device_metrics() -> dict[str, Any]:
    sys.path.insert(0, str(SRC))
    from phuthanhai.config import hardware_facts, model_specs
    from phuthanhai.runtime.resource import snapshot

    facts = hardware_facts()
    resources = snapshot()
    models = []
    for spec in model_specs():
        from phuthanhai.config import legacy_aivideo_root

        path = legacy_aivideo_root() / "Models" / spec.relative_path
        models.append(
            {
                "role": spec.role,
                "present": path.is_dir(),
                "required_files": list(spec.required_files),
                "bytes": sum((path / name).stat().st_size for name in spec.required_files if (path / name).is_file()),
            }
        )
    payload = {
        "generated_at": now_iso(),
        "hardware": facts,
        "resources": resources.to_dict(),
        "models": models,
        "ffmpeg": shutil.which("ffmpeg"),
        "ffprobe": shutil.which("ffprobe"),
        "venv_python": str(VENV_PY),
    }
    (OUT / "device_metrics.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return payload


def render_gate_md(steps: list[dict[str, Any]], metrics: dict[str, Any], verdict: str) -> str:
    lines = [
        "# S00 — Foundation & Doctor — Gate evidence",
        "",
        f"- Generated: {now_iso()}",
        f"- Host: {metrics['hardware']['platform']}",
        f"- Python (app venv): {metrics['hardware']['python_version']} @ {metrics['venv_python']}",
        f"- Data root: ~/Library/Application Support/PhuThanhAI (outside the source tree)",
        "",
        "## Checklist",
        "",
        "| # | Requirement | Result | Detail |",
        "|---|---|---|---|",
    ]
    labels = {
        "doctor": "Environment, model paths, ffmpeg, MLX/Metal, disk, permissions",
        "doctor_deep": "Model artifact SHA-256 verified against install manifest",
        "contracts_tool": "V2/V3 schema + fixtures + semantic + portable checksum",
        "pytest": "Unit + integration tests",
        "network_isolation": "Model worker cannot reach the network (sandbox deny + socket block)",
        "web_bundle": "React shell present, bundled locally, no CDN",
        "swift_launcher": "SwiftUI launcher scaffold compiles to arm64 Mach-O",
        "vlm_smoke": "One real frame through Qwen3-VL offline, RAM measured",
        "server_e2e": "Loopback server: host/origin/capability/traversal/static/web root",
        "orphans": "No leftover worker or server process",
    }
    for index, item in enumerate(steps, start=1):
        detail = item["detail"].replace("|", "\\|")
        lines.append(f"| {index} | {labels.get(item['step'], item['step'])} | **{item['status']}** | {detail} |")
    lines += ["", f"## Verdict: {verdict}", ""]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        default=None,
        help=f"real footage for the frame smoke test (or set {SOURCE_ENV}); no default on purpose",
    )
    parser.add_argument("--skip-model", action="store_true", help="skip the model smoke (records SKIP honestly)")
    args = parser.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    rec = Recorder(OUT / "commands.log")
    steps: list[dict[str, Any]] = []

    steps.append(step_doctor(rec))
    steps.append(step_doctor_deep(rec))
    steps.append(step_contract_tool(rec))
    steps.append(step_pytest(rec))
    steps.append(step_network(rec))
    steps.append(step_web_bundle())
    steps.append(step_swift())
    source_raw = args.source or os.environ.get(SOURCE_ENV)
    source = Path(source_raw).expanduser() if source_raw else None
    steps.append(step_vlm_smoke(rec, source, skip=args.skip_model))
    steps.append(step_server_e2e(rec))
    steps.append(step_orphans(rec))

    metrics = step_device_metrics()
    hard_failures = [item["step"] for item in steps if item["status"] == "FAIL"]
    warnings = [item["step"] for item in steps if item["status"] in {"WARN", "SKIP"}]
    verdict = "FAIL" if hard_failures else ("PASS_WITH_WARNINGS" if warnings else "PASS")

    gate = {
        "milestone": "S00",
        "generated_at": now_iso(),
        "verdict": verdict,
        "steps": steps,
        "hard_failures": hard_failures,
        "warnings": warnings,
        "device_metrics_file": "device_metrics.json",
    }
    (OUT / "gate.json").write_text(json.dumps(gate, indent=2, ensure_ascii=False), encoding="utf-8")
    (OUT / "gate.md").write_text(render_gate_md(steps, metrics, verdict), encoding="utf-8")

    print(render_gate_md(steps, metrics, verdict))
    for item in steps:
        rec.log(f"[{item['status']}] {item['step']}: {item['detail']}")
    return 0 if verdict != "FAIL" else 1


if __name__ == "__main__":
    sys.exit(main())
