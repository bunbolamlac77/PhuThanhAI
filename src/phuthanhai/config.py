"""Configuration: repository layout, data root, model snapshots, MLX runtime.

Hard rules encoded here
-----------------------
* The application never writes into the legacy ``AIVideo`` tree; model snapshots
  and the MLX runtime are opened read-only.
* There are no secrets and no hardcoded tokens. The local API capability is
  generated per run and stored with mode ``0600`` under the data root.
* Core functionality must work with no network. ``offline_environment()`` builds
  the environment every model subprocess receives.

Paths are discovered, not assumed: ``doctor`` reports exactly what was found and
falls back to an explicit ``MISSING`` state instead of inventing a location.
"""

from __future__ import annotations

import os
import platform
import sys
from dataclasses import dataclass
from pathlib import Path

PRODUCT_VERSION = "3.0.0"
CONTRACT_VERSION = "1.0.0"

#: Offline guard applied to every model subprocess and to doctor --offline.
OFFLINE_ENVIRONMENT: dict[str, str] = {
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
    "HF_HUB_DISABLE_TELEMETRY": "1",
    "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1",
    "DO_NOT_TRACK": "1",
    "TOKENIZERS_PARALLELISM": "false",
    "PIP_NO_INPUT": "1",
    "UV_OFFLINE": "1",
}

DEFAULT_HOST = "127.0.0.1"
#: 0 asks the OS for a free loopback port; the chosen port is written to the
#: session file so the launcher and the UI never guess.
DEFAULT_PORT = 0


class ConfigError(RuntimeError):
    """Configuration problem that must stop startup (never silently ignored)."""

    code = "CONFIG_ERROR"


def repo_root() -> Path:
    """Return the source checkout root (``.../PhuThanhAI``)."""

    env = os.environ.get("PHUTHANHAI_HOME")
    if env:
        return Path(env).expanduser().resolve()
    # src/phuthanhai/config.py -> parents[2] is the repository root.
    return Path(__file__).resolve().parents[2]


def user_home() -> Path:
    return Path.home()


def data_root() -> Path:
    env = os.environ.get("PHUTHANHAI_DATA_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return user_home() / "Library" / "Application Support" / "PhuThanhAI"


def legacy_aivideo_root() -> Path:
    env = os.environ.get("PHUTHANHAI_AIVIDEO_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    return user_home() / "Library" / "Application Support" / "AIVideo"


@dataclass(frozen=True)
class ModelSpec:
    """A required local model snapshot.

    ``role`` mirrors the specification; ``relative_path`` is resolved against the
    legacy AIVideo model root. ``required_files`` are the artifacts needed before
    loading; a snapshot missing any of them is reported as NOT READY, never
    auto-downloaded.
    """

    role: str
    relative_path: str
    required_files: tuple[str, ...]
    used_by: str
    description: str


def model_specs() -> list[ModelSpec]:
    return [
        ModelSpec(
            role="visual_analysis_primary",
            relative_path="visual_analysis_primary",
            required_files=(
                "config.json",
                "model.safetensors",
                "tokenizer.json",
                "tokenizer_config.json",
                "preprocessor_config.json",
            ),
            used_by="S00 smoke, S03 VLM",
            description="Qwen3-VL-4B-Instruct-4bit (mlx-vlm)",
        ),
        ModelSpec(
            role="story_synthesis",
            relative_path="story_synthesis",
            required_files=("config.json", "model.safetensors", "tokenizer.json", "tokenizer_config.json"),
            used_by="S06 script writer (not used at S00)",
            description="Qwen3-8B-4bit (mlx-lm)",
        ),
        ModelSpec(
            role="tts_kokoro_vi",
            relative_path="tts_kokoro_vi",
            required_files=("config.json", "kokoro_vi.pth", "voices.json"),
            used_by="S07 TTS (not used at S00)",
            description="Kokoro Vietnamese voicepacks",
        ),
        ModelSpec(
            role="g0_text_optional",
            relative_path="g0-text/73e3e38d981303bc594367cd910ea6eb48349da8",
            required_files=("config.json", "model.safetensors", "tokenizer.json"),
            used_by="optional lightweight classification (never resident)",
            description="Qwen3-0.6B-4bit",
        ),
    ]


@dataclass(frozen=True)
class RuntimeSpec:
    """A discovered Python interpreter that owns the MLX stack."""

    python: Path
    origin: str

    def exists(self) -> bool:
        return self.python.is_file() and os.access(self.python, os.X_OK)


def mlx_runtime_candidates() -> list[RuntimeSpec]:
    """Ordered candidates for the interpreter that can import ``mlx_vlm``.

    The first candidate is the runtime recorded in the architecture document; the
    second is the legacy worker virtualenv; the third is the app venv itself, which
    is only useful if the operator explicitly installed MLX there.
    """

    legacy = legacy_aivideo_root()
    candidates = [
        RuntimeSpec(legacy / "Runtimes" / "g0-mlx-runtime" / "bin" / "python", "documented-g0-mlx-runtime"),
        RuntimeSpec(
            user_home() / "APP_CaNhan" / "AIVideo" / "workers" / ".venv" / "bin" / "python",
            "legacy-worker-venv",
        ),
    ]
    env = os.environ.get("PHUTHANHAI_MLX_PYTHON")
    if env:
        candidates.insert(0, RuntimeSpec(Path(env).expanduser().resolve(), "env-PHUTHANHAI_MLX_PYTHON"))
    candidates.append(RuntimeSpec(Path(sys.executable), "app-venv"))
    return candidates


@dataclass(frozen=True)
class AppPaths:
    """Operational paths. Runtime data never lives inside the source tree."""

    root: Path

    @property
    def registry_db(self) -> Path:
        return self.root / "registry.sqlite3"

    @property
    def projects(self) -> Path:
        return self.root / "projects"

    @property
    def caches(self) -> Path:
        return self.root / "caches"

    @property
    def thumbs(self) -> Path:
        return self.caches / "thumbs"

    @property
    def preview_proxies(self) -> Path:
        return self.caches / "preview_proxies"

    @property
    def sampling(self) -> Path:
        return self.caches / "sampling"

    @property
    def runtime(self) -> Path:
        return self.root / "runtime"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def session_file(self) -> Path:
        return self.runtime / "session.json"

    @property
    def instance_lock(self) -> Path:
        return self.runtime / "instance.lock"

    def ensure(self) -> None:
        # 0700: the data root holds private footage metadata and notes.
        for path in (
            self.root,
            self.projects,
            self.caches,
            self.thumbs,
            self.preview_proxies,
            self.sampling,
            self.runtime,
            self.logs,
        ):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)


@dataclass(frozen=True)
class Settings:
    repo: Path
    data: AppPaths
    contracts_dir: Path
    web_dist: Path
    fixtures_dir: Path
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT

    @property
    def contracts(self) -> Path:
        return self.contracts_dir

    @classmethod
    def load(cls, *, repo: Path | None = None) -> "Settings":
        root = (repo or repo_root()).resolve()
        return cls(
            repo=root,
            data=AppPaths(data_root()),
            contracts_dir=root / "contracts",
            web_dist=root / "web" / "dist",
            fixtures_dir=root / "tests" / "fixtures",
        )


def offline_environment(base: dict[str, str] | None = None) -> dict[str, str]:
    """Return an environment dict that forbids model/package downloads."""

    env = dict(os.environ if base is None else base)
    env.update(OFFLINE_ENVIRONMENT)
    return env


def hardware_facts() -> dict[str, object]:
    """Facts about this machine, collected without third-party packages."""

    import shutil

    facts: dict[str, object] = {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "macos_version": platform.mac_ver()[0],
        "python_version": platform.python_version(),
        "python_executable": sys.executable,
        "cpu_count": os.cpu_count(),
        "memory_bytes": os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
        if hasattr(os, "sysconf")
        else None,
    }
    usage = shutil.disk_usage(str(data_root().parent if data_root().parent.exists() else Path.home()))
    facts["disk_free_bytes"] = usage.free
    facts["disk_total_bytes"] = usage.total
    return facts
