"""``doctor`` - evidence-based environment, model and runtime checks.

Every check returns what was *measured*: paths that exist, versions that were
imported, hashes that matched. A missing model or an incompatible runtime is
reported as FAIL/BLOCKED, never auto-downloaded and never silently upgraded.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from phuthanhai import PRODUCT_VERSION
from phuthanhai.config import (
    CONTRACT_VERSION,
    OFFLINE_ENVIRONMENT,
    ModelSpec,
    Settings,
    hardware_facts,
    mlx_runtime_candidates,
    model_specs,
    offline_environment,
)
from phuthanhai.analyzer.probe import tool_version
from phuthanhai.contracts.selfcheck import run_contract_selfcheck
from phuthanhai.utils.fs import atomic_write_text, strict_json_loads
from phuthanhai.utils.hashes import sha256_file

MLX_PROBE = r"""
import json, sys
info = {"python": sys.version.split()[0], "executable": sys.executable}
try:
    import mlx.core as mx
    info["mlx"] = getattr(mx, "__version__", None)
    try:
        info["metal_available"] = bool(mx.metal.is_available())
        info["default_device"] = str(mx.default_device())
    except Exception as exc:
        info["metal_available"] = None
        info["metal_error"] = f"{type(exc).__name__}: {exc}"
except Exception as exc:
    info["mlx"] = None
    info["mlx_error"] = f"{type(exc).__name__}: {exc}"

for pkg, mod in (("mlx-vlm", "mlx_vlm"), ("mlx-lm", "mlx_lm")):
    try:
        import importlib.metadata as md
        info[pkg] = md.version(pkg)
    except Exception as exc:
        info[pkg] = None
        info[pkg + "_error"] = f"{type(exc).__name__}: {exc}"

try:
    import mlx_vlm.models.qwen3_vl  # noqa: F401
    info["qwen3_vl_supported"] = True
except Exception as exc:
    info["qwen3_vl_supported"] = False
    info["qwen3_vl_error"] = f"{type(exc).__name__}: {exc}"

print(json.dumps(info))
"""


@dataclass
class Check:
    id: str
    title: str
    status: str  # PASS | WARN | FAIL | SKIP
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class DoctorReport:
    status: str
    generated_at: str
    product_version: str
    contract_version: str
    offline: bool
    checks: list[Check]
    hardware: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "generated_at": self.generated_at,
            "product_version": self.product_version,
            "contract_version": self.contract_version,
            "offline": self.offline,
            "checks": [asdict(check) for check in self.checks],
            "failures": [check.id for check in self.failures],
            "warnings": [check.id for check in self.warnings],
            "hardware": self.hardware,
        }

    @property
    def failures(self) -> list[Check]:
        return [check for check in self.checks if check.status == "FAIL"]

    @property
    def warnings(self) -> list[Check]:
        return [check for check in self.checks if check.status == "WARN"]


def _check_platform() -> Check:
    facts = hardware_facts()
    ok = facts["machine"] == "arm64" and str(facts["macos_version"]).startswith("2")
    return Check(
        id="platform",
        title="Apple Silicon / macOS",
        status="PASS" if ok else "WARN",
        detail=f"{facts['machine']} · macOS {facts['macos_version']} · {facts['cpu_count']} CPUs",
        evidence={k: facts[k] for k in ("machine", "macos_version", "cpu_count", "memory_bytes", "platform")},
    )


def _check_python() -> Check:
    import platform as _platform
    import sys

    version = _platform.python_version()
    ok = version.startswith("3.12.")
    return Check(
        id="python_app",
        title="Application Python 3.12",
        status="PASS" if ok else "FAIL",
        detail=f"Python {version}",
        evidence={"python_version": version, "executable": sys.executable},
    )


def _check_data_root(settings: Settings) -> Check:
    try:
        settings.data.ensure()
        probe = settings.data.runtime / ".write_probe"
        atomic_write_text(probe, "ok", mode=0o600)
        mode = oct(probe.stat().st_mode & 0o777)
        probe.unlink()
    except OSError as exc:
        return Check("data_root", "Writable data root", "FAIL", f"cannot write data root: {exc}")
    return Check(
        id="data_root",
        title="Writable data root (outside source tree)",
        status="PASS",
        detail=f"ok · runtime dir mode {oct(settings.data.runtime.stat().st_mode & 0o777)}",
        evidence={"data_root_configured": True, "write_probe_mode": mode},
    )


def _check_disk(settings: Settings) -> Check:
    usage = shutil.disk_usage(str(settings.data.root))
    free_gib = usage.free / 1024**3
    status = "PASS" if free_gib >= 5 else ("WARN" if free_gib >= 2 else "FAIL")
    return Check(
        id="disk_free",
        title="Free disk space (>= 5 GiB)",
        status=status,
        detail=f"{free_gib:.1f} GiB free",
        evidence={"free_bytes": usage.free, "total_bytes": usage.total},
    )


def _check_tool(name: str) -> Check:
    version = tool_version(name)
    binary = shutil.which(name)
    if not binary or not version:
        return Check(f"{name}", f"{name} available", "FAIL", "not found on PATH")
    return Check(
        id=name,
        title=f"{name} available",
        status="PASS",
        detail=version[:120],
        evidence={"path": binary, "version": version.split()[2] if len(version.split()) > 2 else version},
    )


def _verify_manifest_hashes(model_dir: Path, filenames: tuple[str, ...]) -> dict[str, Any]:
    manifest_path = model_dir / ".aivideo-install-manifest.json"
    result: dict[str, Any] = {"manifest": manifest_path.name, "checked": {}, "mismatches": []}
    if not manifest_path.is_file():
        result["status"] = "no_manifest"
        return result
    try:
        manifest = strict_json_loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        result["status"] = "manifest_invalid"
        result["error"] = str(exc)
        return result

    files = manifest.get("files", {})
    for name in filenames:
        expected = (files.get(name) or {}).get("sha256")
        target = model_dir / name
        if not expected or not target.is_file():
            continue
        actual = sha256_file(target)
        match = actual == expected
        result["checked"][name] = {"expected": expected, "actual": actual, "match": match}
        if not match:
            result["mismatches"].append(name)
    result["status"] = "verified" if not result["mismatches"] else "mismatch"
    return result


def _check_model(spec: ModelSpec, *, verify_hashes: bool) -> Check:
    from phuthanhai.config import legacy_aivideo_root

    model_dir = legacy_aivideo_root() / "Models" / spec.relative_path
    if not model_dir.is_dir():
        return Check(
            id=f"model_{spec.role}",
            title=f"Model snapshot: {spec.description}",
            status="FAIL" if spec.role == "visual_analysis_primary" else "WARN",
            detail=f"snapshot missing: {model_dir.name}",
            evidence={"role": spec.role, "present": False, "used_by": spec.used_by},
        )

    missing = [name for name in spec.required_files if not (model_dir / name).is_file()]
    sizes = {
        name: (model_dir / name).stat().st_size
        for name in spec.required_files
        if (model_dir / name).is_file()
    }
    evidence: dict[str, Any] = {
        "role": spec.role,
        "present": True,
        "used_by": spec.used_by,
        "missing_files": missing,
        "sizes": sizes,
        "total_bytes": sum(sizes.values()),
    }
    if verify_hashes:
        evidence["hash_verification"] = _verify_manifest_hashes(model_dir, spec.required_files)
        if evidence["hash_verification"].get("mismatches"):
            missing = list(missing) + ["sha256_mismatch"]

    status = "PASS" if not missing else ("FAIL" if spec.role == "visual_analysis_primary" else "WARN")
    detail = (
        f"{len(spec.required_files)} required files present · {evidence['total_bytes'] / 1024**3:.2f} GiB"
        if not missing
        else f"missing: {', '.join(missing)}"
    )
    return Check(
        id=f"model_{spec.role}",
        title=f"Model snapshot: {spec.description}",
        status=status,
        detail=detail,
        evidence=evidence,
    )


def _check_mlx_runtime() -> Check:
    candidates = mlx_runtime_candidates()
    existing = [spec for spec in candidates if spec.exists()]
    if not existing:
        return Check(
            id="mlx_runtime",
            title="MLX runtime interpreter",
            status="FAIL",
            detail="no candidate interpreter exists",
            evidence={"candidates": [str(spec.python) for spec in candidates]},
        )
    chosen = existing[0]
    probe = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [str(chosen.python), "-c", MLX_PROBE],
        capture_output=True,
        text=True,
        timeout=180,
        env=offline_environment(),
        check=False,
        cwd=str(Path.home()),
    )
    evidence: dict[str, Any] = {
        "chosen": str(chosen.python),
        "origin": chosen.origin,
        "returncode": probe.returncode,
        "stderr_tail": probe.stderr.strip().splitlines()[-3:] if probe.stderr.strip() else [],
    }
    if probe.returncode != 0:
        return Check(
            id="mlx_runtime",
            title="MLX runtime interpreter",
            status="FAIL",
            detail="runtime probe failed",
            evidence=evidence,
        )
    try:
        info = json.loads(probe.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError) as exc:
        evidence["error"] = str(exc)
        return Check("mlx_runtime", "MLX runtime interpreter", "FAIL", "probe output unparsable", evidence)

    evidence["probe"] = info
    problems: list[str] = []
    if not info.get("mlx"):
        problems.append("mlx not importable")
    if info.get("metal_available") is not True:
        problems.append("Metal unavailable")
    if not info.get("qwen3_vl_supported"):
        problems.append("mlx_vlm lacks qwen3_vl")
    status = "PASS" if not problems else "FAIL"
    detail = (
        f"mlx {info.get('mlx')} · mlx-vlm {info.get('mlx-vlm')} · mlx-lm {info.get('mlx-lm')} · "
        f"metal={info.get('metal_available')} · qwen3_vl={info.get('qwen3_vl_supported')}"
    )
    if problems:
        detail = f"{detail} · problems: {', '.join(problems)}"
    return Check("mlx_runtime", "MLX runtime interpreter", status, detail, evidence)


def _check_offline(offline: bool) -> Check:
    # Always export the guard so any later import inherits it.
    os.environ.update(OFFLINE_ENVIRONMENT)
    enforced = {key: os.environ.get(key) for key in OFFLINE_ENVIRONMENT}
    bad = [key for key, value in enforced.items() if value != OFFLINE_ENVIRONMENT[key]]
    status = "PASS" if not bad else "FAIL"
    return Check(
        id="offline_env",
        title="Offline environment guard",
        status=status,
        detail=("enforced" if not bad else f"not enforced: {', '.join(bad)}") + (" (--offline)" if offline else ""),
        evidence={"variables": enforced, "requested_offline": offline},
    )


def _check_bind(settings: Settings) -> Check:
    loopback = settings.host in {"127.0.0.1", "::1", "localhost"}
    return Check(
        id="security_bind",
        title="Loopback-only bind",
        status="PASS" if loopback else "FAIL",
        detail=f"host={settings.host}",
        evidence={"host": settings.host, "loopback_only": loopback},
    )


def _check_session_permissions(settings: Settings) -> Check:
    path = settings.data.session_file
    if not path.exists():
        return Check(
            "security_session_file",
            "Session capability file (0600)",
            "SKIP",
            "no running backend; file is created at serve time",
        )
    mode = path.stat().st_mode & 0o777
    ok = mode == 0o600
    return Check(
        "security_session_file",
        "Session capability file (0600)",
        "PASS" if ok else "FAIL",
        f"mode {oct(mode)}",
        {"mode": oct(mode)},
    )


def _check_web(settings: Settings) -> Check:
    index = settings.web_dist / "index.html"
    if index.is_file():
        assets = [p.name for p in (settings.web_dist / "assets").glob("*")] if (settings.web_dist / "assets").is_dir() else []
        return Check(
            "web_bundle",
            "Frontend bundle present (offline)",
            "PASS",
            f"index.html + {len(assets)} asset(s)",
            {"web_dist": "web/dist", "asset_count": len(assets)},
        )
    return Check("web_bundle", "Frontend bundle present (offline)", "WARN", "web/dist not built yet")


def _check_swift() -> Check:
    swift = shutil.which("swift")
    if not swift:
        return Check("swift_toolchain", "Swift toolchain", "WARN", "swift not found")
    completed = subprocess.run(  # noqa: S603
        [swift, "--version"], capture_output=True, text=True, timeout=30, check=False
    )
    return Check(
        "swift_toolchain",
        "Swift toolchain (launcher scaffold)",
        "PASS" if completed.returncode == 0 else "WARN",
        completed.stdout.splitlines()[0] if completed.stdout else "swift present",
        {"path": swift},
    )


def _check_contracts(settings: Settings) -> Check:
    report = run_contract_selfcheck(settings.contracts_dir, settings.fixtures_dir)
    return Check(
        id="contracts",
        title="V2/V3 schemas + fixtures + portable roundtrip",
        status="PASS" if report.get("status") == "pass" else "FAIL",
        detail=report.get("error") or "structural + semantic + checksum checks pass",
        evidence=report,
    )


def run_doctor(settings: Settings, *, offline: bool = False, verify_hashes: bool = False) -> DoctorReport:
    checks: list[Check] = [
        _check_platform(),
        _check_python(),
        _check_data_root(settings),
        _check_disk(settings),
        _check_tool("ffmpeg"),
        _check_tool("ffprobe"),
    ]
    for spec in model_specs():
        checks.append(_check_model(spec, verify_hashes=verify_hashes))
    checks.append(_check_mlx_runtime())
    checks.append(_check_offline(offline))
    checks.append(_check_bind(settings))
    checks.append(_check_session_permissions(settings))
    checks.append(_check_web(settings))
    checks.append(_check_swift())
    checks.append(_check_contracts(settings))

    failures = [check for check in checks if check.status == "FAIL"]
    warnings = [check for check in checks if check.status == "WARN"]
    if failures:
        status = "FAIL"
    elif warnings:
        status = "PASS_WITH_WARNINGS"
    else:
        status = "PASS"

    return DoctorReport(
        status=status,
        generated_at=datetime.now(tz=timezone.utc).astimezone().isoformat(timespec="seconds"),
        product_version=PRODUCT_VERSION,
        contract_version=CONTRACT_VERSION,
        offline=offline,
        checks=checks,
        hardware=hardware_facts(),
    )


def render_text(report: DoctorReport) -> str:
    width = 5
    lines = [
        f"PhuThanhAI {report.product_version} doctor  ·  contract {report.contract_version}",
        f"generated  {report.generated_at}",
        f"offline    {report.offline}",
        "",
    ]
    for check in report.checks:
        lines.append(f"[{check.status:<{width}}] {check.id:<22} {check.detail}")
        if check.status in {"FAIL", "WARN"} and check.evidence:
            snippet = json.dumps(check.evidence, ensure_ascii=False, sort_keys=True)
            lines.append(f"{'':<{width + 3}}> {snippet[:400]}")
    lines.append("")
    lines.append(f"RESULT: {report.status}")
    if report.failures:
        lines.append("Failures: " + ", ".join(check.id for check in report.failures))
    if report.warnings:
        lines.append("Warnings: " + ", ".join(check.id for check in report.warnings))
    return "\n".join(lines)
