"""S03 visual analysis: scheduler, isolated worker, inference cache, evidence.

Uses the S00 ``vlm_worker`` subprocess protocol (v2) under the runtime interpreter
that ``doctor`` verified. The FastAPI process never imports MLX.

Guarantees:

* one ``AI_HEAVY`` owner: slot acquired before spawn and released only after the
  worker is reaped;
* the ResourceGovernor is consulted before granting the slot;
* full source SHA-256 and every frame hash are verified before inference (P0-B,
  P0-A); corrupt frame cache is repaired through the sampling pipeline;
* inference cache key covers source, sampling fingerprint, frame hashes, model
  identity, runtime versions, prompt version and generation settings - results
  are never reused across model versions;
* checkpoints are atomic per batch; resume skips committed frames, retries only
  missing ones, and never duplicates frame ids;
* failed frames become ``unknown`` entries with uncertainty, never fabricated
  observations; coverage is propagated from S02, so insufficient coverage can
  never be reported as fully analysed.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from phuthanhai.analyzer import vlm_worker
from phuthanhai.analyzer.evidence import EvidenceError, build_evidence, validate_evidence
from phuthanhai.analyzer.optimizer import FrameResultCache, build_selection
from phuthanhai.analyzer.sampling_pipeline import SamplingError, SamplingPipeline
from phuthanhai.config import (
    Settings,
    legacy_aivideo_root,
    mlx_runtime_candidates,
    model_specs,
    offline_environment,
)
from phuthanhai.library import store
from phuthanhai.library.db import Database
from phuthanhai.runtime.resource import GOVERNOR
from phuthanhai.runtime.scheduler import HEAVY_SLOT, PROCESSES, HeavySlotBusy
from phuthanhai.utils.fs import atomic_write_json
from phuthanhai.utils.hashes import sha256_file

VISION_VERSION = "s03-vision-1"
MAX_WORKER_OUTPUT_BYTES = 32 * 1024 * 1024
MAX_WORKER_STDERR_BYTES = 1024 * 1024
DEFAULT_BATCH_SIZE = 4
DEFAULT_TIMEOUT_S = 900.0
MAX_ATTEMPTS = 3
MAX_REQUEST_BYTES = 512 * 1024

_RUNTIME_CACHE: dict[str, dict[str, Any]] = {}

_VERSION_PROBE = """
import json
import sys
import importlib.metadata as md
out = {"python": sys.version.split()[0]}
for name in ("mlx", "mlx-vlm", "mlx-lm", "transformers"):
    try:
        out[name] = md.version(name)
    except Exception:
        out[name] = None
print(json.dumps(out))
"""


class VisionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class VisionOutcome:
    analysis_id: str
    status: str  # complete | partial | cancelled
    method: str
    fingerprint: str
    cache_dir: Path
    evidence_path: Path
    frames_total: int
    frames_observed: int
    frames_unknown: int
    coverage_status: str
    elapsed_s: float
    retries: int
    worker_restarts: int
    sampling_frames: int = 0
    selected_frames: int = 0
    selection_mode: str = "full"
    frame_cache_hits: int = 0
    inferences: int = 0
    worker_loads: int = 0
    model_load_s: float = 0.0
    generation_tokens: int = 0
    prompt_tokens: int = 0
    peak_worker_rss_bytes: int = 0
    mlx_peak_memory_bytes: int = 0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "analysis_id": self.analysis_id,
            "status": self.status,
            "method": self.method,
            "fingerprint": self.fingerprint,
            "frames_total": self.frames_total,
            "frames_observed": self.frames_observed,
            "frames_unknown": self.frames_unknown,
            "sampling_frames": self.sampling_frames,
            "selected_frames": self.selected_frames,
            "selection_mode": self.selection_mode,
            "frame_cache_hits": self.frame_cache_hits,
            "inferences": self.inferences,
            "worker_loads": self.worker_loads,
            "model_load_s": round(self.model_load_s, 3),
            "generation_tokens": self.generation_tokens,
            "prompt_tokens": self.prompt_tokens,
            "coverage_status": self.coverage_status,
            "elapsed_s": round(self.elapsed_s, 3),
            "retries": self.retries,
            "worker_restarts": self.worker_restarts,
            "peak_worker_rss_bytes": self.peak_worker_rss_bytes,
            "mlx_peak_memory_bytes": self.mlx_peak_memory_bytes,
            "warnings": self.warnings,
        }


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def runtime_versions(python: Path) -> dict[str, Any]:
    key = str(python)
    if key in _RUNTIME_CACHE:
        return _RUNTIME_CACHE[key]
    result: dict[str, Any] = {"python": None, "mlx": None, "mlx-vlm": None, "mlx-lm": None, "transformers": None}
    if not python.is_file():
        result["error"] = "runtime python missing"
        _RUNTIME_CACHE[key] = result
        return result
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv
            [str(python), "-c", _VERSION_PROBE],
            capture_output=True,
            text=True,
            timeout=120,
            env=offline_environment(),
            check=False,
        )
        if completed.returncode == 0 and completed.stdout.strip():
            result.update(json.loads(completed.stdout.strip().splitlines()[-1]))
            result["python"] = ".".join(str(v) for v in __import__("sys").version_info[:3])
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    _RUNTIME_CACHE[key] = result
    return result


def model_identity(model_dir: Path) -> dict[str, Any]:
    manifest = model_dir / ".aivideo-install-manifest.json"
    config = model_dir / "config.json"
    weights = model_dir / "model.safetensors"
    return {
        "name": model_dir.name,
        "manifest_sha256": sha256_file(manifest) if manifest.is_file() else None,
        "config_sha256": sha256_file(config) if config.is_file() else None,
        "weights_bytes": weights.stat().st_size if weights.is_file() else 0,
    }


def inference_fingerprint(
    *,
    source_sha256: str,
    sampling_fingerprint: str,
    frame_hashes: list[list[str]],
    model: dict[str, Any],
    runtime: dict[str, Any],
    prompt_version: str,
    method: str,
    batch_size: int,
    max_tokens: int,
    temperature: float,
    selection: dict[str, Any] | None = None,
) -> str:
    payload = {
        "vision_version": VISION_VERSION,
        "source_sha256": source_sha256,
        "sampling_fingerprint": sampling_fingerprint,
        "frame_hashes": frame_hashes,
        "model": model,
        "runtime": runtime,
        "prompt_version": prompt_version,
        "method": method,
        "batch_size": batch_size,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "selection": selection or {},
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class VisionService:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        *,
        runtime_python: Path | None = None,
        worker_module: str | None = None,
        model_path: Path | None = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
        request_timeout_s: float = DEFAULT_TIMEOUT_S,
        allow_multi_frame: bool = False,
        require_governor: bool = True,
    ) -> None:
        self.db = db
        self.settings = settings
        self.sampling = SamplingPipeline(db, settings.data.sampling)
        self.runtime_python = runtime_python or self._discover_runtime()
        self.worker_module = worker_module or os.environ.get(
            "PHUTHANHAI_VLM_WORKER_MODULE", "phuthanhai.analyzer.vlm_worker"
        )
        self.model_path = model_path or self._discover_model()
        self.batch_size = max(1, min(batch_size, vlm_worker.MAX_FRAMES))
        self._peak_worker_rss = 0
        self._mlx_peak = 0
        self.request_timeout_s = request_timeout_s
        self.allow_multi_frame = allow_multi_frame
        self.require_governor = require_governor
        settings.data.inference.mkdir(parents=True, exist_ok=True, mode=0o700)

    @staticmethod
    def _discover_runtime() -> Path:
        env = os.environ.get("PHUTHANHAI_VLM_PYTHON")
        if env:
            return Path(env)
        for candidate in mlx_runtime_candidates():
            if candidate.exists():
                return candidate.python
        raise VisionError("MODEL_RUNTIME_INCOMPATIBLE", "no MLX runtime interpreter found")

    @staticmethod
    def _discover_model() -> Path:
        for spec in model_specs():
            if spec.role == "visual_analysis_primary":
                path = legacy_aivideo_root() / "Models" / spec.relative_path
                if path.is_dir():
                    return path
        raise VisionError("MODEL_SNAPSHOT_MISSING", "Qwen3-VL snapshot not found")

    # -- cache/checkpoint -------------------------------------------------- #
    def _cache_dir(self, asset_id: str, fingerprint: str) -> Path:
        return self.settings.data.inference / asset_id / fingerprint[:20]

    def _load_checkpoint(self, path: Path, fingerprint: str) -> dict[str, Any] | None:
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("fingerprint") != fingerprint or data.get("vision_version") != VISION_VERSION:
                return None
            if not isinstance(data.get("results"), dict):
                return None
            return data
        except (OSError, ValueError):
            return None

    def _write_checkpoint(self, path: Path, fingerprint: str, analysis_id: str, method: str, results: dict[str, Any]) -> None:
        atomic_write_json(
            path,
            {
                "vision_version": VISION_VERSION,
                "fingerprint": fingerprint,
                "analysis_id": analysis_id,
                "method": method,
                "results": results,
                "completed_frame_ids": sorted(results),
                "updated_at": _now_iso(),
            },
        )

    # -- worker process ---------------------------------------------------- #
    def _worker_env(self) -> dict[str, str]:
        repo = Path(__file__).resolve().parents[3]
        paths = [str(repo / "src"), str(repo)]
        existing = os.environ.get("PYTHONPATH")
        if existing:
            paths.append(existing)
        env = offline_environment()
        env["PYTHONPATH"] = os.pathsep.join(paths)
        return env

    def _terminate(self, popen: subprocess.Popen[bytes], *, grace_s: float = 5.0) -> None:
        if popen.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(popen.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            popen.wait(timeout=grace_s)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(os.getpgid(popen.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            popen.wait(timeout=5)
        except subprocess.TimeoutExpired as exc:
            raise VisionError("WORKER_ORPHAN_REAP_FAILED", "worker survived SIGKILL") from exc

    def _run_worker(
        self,
        request: dict[str, Any],
        *,
        cancel: threading.Event | None,
        on_frame: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Consume worker JSONL incrementally; never buffer the whole run.

        The reader thread only enqueues bytes. Validation and durable commits run
        in the orchestration thread (no SQLite access from the reader thread).
        """
        payload = json.dumps(request, ensure_ascii=False).encode("utf-8")
        if len(payload) > MAX_REQUEST_BYTES:
            raise VisionError("WORKER_REQUEST_TOO_LARGE", "request exceeds the worker size cap")
        argv = [str(self.runtime_python), "-m", self.worker_module]
        if os.environ.get("PHUTHANHAI_SANDBOX_WORKER") == "1":
            argv = ["/usr/bin/sandbox-exec", "-p", "(version 1)(allow default)(deny network*)", *argv]
        started = time.monotonic()
        popen = PROCESSES.spawn(argv, env=self._worker_env())
        lines: queue.Queue[tuple[str, bytes | None]] = queue.Queue(maxsize=1024)
        stderr_bytes = bytearray()
        stderr_lock = threading.Lock()
        timed_out = False
        cancelled = False
        output_too_large = False
        expected = {frame["frame_id"]: frame for frame in request.get("frames", [])}
        seen: set[str] = set()

        def drain_stderr() -> None:
            try:
                while True:
                    chunk = popen.stderr.read(65536) if popen.stderr else b""
                    if not chunk:
                        return
                    with stderr_lock:
                        if len(stderr_bytes) < MAX_WORKER_STDERR_BYTES:
                            stderr_bytes.extend(chunk[: MAX_WORKER_STDERR_BYTES - len(stderr_bytes)])
            except OSError:
                return

        def read_stdout() -> None:
            nonlocal output_too_large
            total = 0
            try:
                assert popen.stdout is not None
                for raw in popen.stdout:
                    total += len(raw)
                    if total > MAX_WORKER_OUTPUT_BYTES or len(raw) > MAX_WORKER_OUTPUT_BYTES:
                        output_too_large = True
                        lines.put(("too_large", None))
                        return
                    lines.put(("line", raw))
            finally:
                lines.put(("eof", None))

        err_thread = threading.Thread(target=drain_stderr, name="vlm-worker-stderr", daemon=True)
        out_thread = threading.Thread(target=read_stdout, name="vlm-worker-stdout", daemon=True)
        err_thread.start()
        out_thread.start()
        try:
            assert popen.stdin is not None
            popen.stdin.write(payload)
            popen.stdin.close()
        except (BrokenPipeError, OSError):
            pass

        last_seq = 0
        final: dict[str, Any] | None = None
        error_event: dict[str, Any] | None = None
        try:
            while True:
                if cancel is not None and cancel.is_set():
                    cancelled = True
                    self._terminate(popen)
                    break
                if time.monotonic() - started > self.request_timeout_s:
                    timed_out = True
                    self._terminate(popen)
                    break
                try:
                    kind, raw = lines.get(timeout=0.2)
                except queue.Empty:
                    continue
                if kind == "eof":
                    break
                if kind == "too_large":
                    output_too_large = True
                    self._terminate(popen)
                    break
                assert raw is not None
                try:
                    event = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    self._terminate(popen)
                    raise VisionError("WORKER_PROTOCOL_INVALID", "worker emitted invalid JSONL") from exc
                if not isinstance(event, dict):
                    self._terminate(popen)
                    raise VisionError("WORKER_PROTOCOL_INVALID", "worker event must be an object")
                seq = event.get("seq")
                if not isinstance(seq, int) or seq <= last_seq:
                    self._terminate(popen)
                    raise VisionError("WORKER_PROTOCOL_INVALID", "worker protocol sequence is not increasing")
                last_seq = seq
                event_type = event.get("type")
                if event_type not in {"worker_ready", "model_loaded", "frame_result", "sequence_result", "result", "error"}:
                    self._terminate(popen)
                    raise VisionError("WORKER_PROTOCOL_INVALID", f"unknown worker event: {event_type}")
                if event.get("job_id") is not None and event.get("job_id") != request["job_id"]:
                    self._terminate(popen)
                    raise VisionError("WORKER_PROTOCOL_INVALID", "worker job identity mismatch")
                if event.get("asset_id") is not None and event.get("asset_id") != request["asset_id"]:
                    self._terminate(popen)
                    raise VisionError("WORKER_PROTOCOL_INVALID", "worker asset identity mismatch")
                if event_type == "frame_result":
                    frame_id = event.get("frame_id")
                    if frame_id not in expected:
                        self._terminate(popen)
                        raise VisionError("WORKER_PROTOCOL_INVALID", f"frame not in request: {frame_id}")
                    verified = expected[frame_id]
                    if event.get("sha256") not in (None, verified.get("sha256")):
                        self._terminate(popen)
                        raise VisionError("WORKER_PROTOCOL_INVALID", "frame hash mismatch")
                    if event.get("normalized_pts_ms") not in (None, int(verified["normalized_pts_ms"])):
                        self._terminate(popen)
                        raise VisionError("WORKER_PROTOCOL_INVALID", "frame PTS mismatch")
                    if event.get("scene_id") not in (None, verified["scene_id"]):
                        self._terminate(popen)
                        raise VisionError("WORKER_PROTOCOL_INVALID", "frame scene mismatch")
                    if on_frame is not None and frame_id not in seen:
                        on_frame(event)
                        seen.add(frame_id)
                elif event_type == "error":
                    error_event = event
                elif event_type == "result":
                    final = event
        finally:
            if popen.poll() is None:
                self._terminate(popen)
            try:
                popen.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._terminate(popen)
            out_thread.join(timeout=5)
            err_thread.join(timeout=5)
            PROCESSES.discard(popen.pid)

        if cancelled:
            raise VisionError("WORKER_CANCELLED", "analysis cancelled by operator")
        if timed_out:
            raise VisionError("WORKER_TIMEOUT", f"worker exceeded {self.request_timeout_s}s")
        if output_too_large:
            raise VisionError("WORKER_OUTPUT_TOO_LARGE", "worker stdout exceeded the size cap")
        if error_event is not None:
            raise VisionError(
                str(error_event.get("code", "MODEL_INFERENCE_FAILED")),
                vlm_worker.redact(str(error_event.get("message", "")))[:500],
            )
        if final is None:
            raise VisionError("WORKER_CRASHED", "worker exited without a final result event")

        # Legacy/one-shot workers may only send frames in the final result. Commit
        # any validated frames that were not already streamed.
        if on_frame is not None:
            for entry in final.get("frames") or []:
                frame_id = entry.get("frame_id")
                if frame_id in expected and frame_id not in seen:
                    on_frame({**entry, "type": "frame_result", "job_id": request["job_id"]})
                    seen.add(frame_id)

        self._worker_loads = getattr(self, "_worker_loads", 0) + 1
        self._load_s_total = getattr(self, "_load_s_total", 0.0) + float(final.get("load_s") or 0.0)
        self._peak_worker_rss = max(
            getattr(self, "_peak_worker_rss", 0), int(final.get("peak_rss_bytes") or 0)
        )
        self._mlx_peak = max(
            getattr(self, "_mlx_peak", 0), int(final.get("mlx_peak_memory_bytes") or 0)
        )
        return final

    # -- analysis ---------------------------------------------------------- #
    def analyze(
        self,
        asset: dict[str, Any],
        source_path: Path,
        *,
        method: str = "auto",
        selection_mode: str = "full",
        user_ranges: list[list[int]] | None = None,
        progress: Callable[[dict[str, Any]], None] | None = None,
        cancel: threading.Event | None = None,
        job_id: str | None = None,
    ) -> VisionOutcome:
        started = time.monotonic()
        self._worker_loads = 0
        self._load_s_total = 0.0
        asset_id = asset["asset_id"]
        retries = 0
        restarts = 0

        def emit(stage: str, done: int, total: int, **extra: Any) -> None:
            if progress is not None:
                progress(
                    {
                        "stage": stage,
                        "done": done,
                        "total": total,
                        "fraction": (done / total) if total else 0.0,
                        **extra,
                    }
                )

        for required in ("config.json", "model.safetensors"):
            if not (self.model_path / required).is_file():
                raise VisionError("MODEL_SNAPSHOT_MISSING", f"model snapshot missing {required}")

        sampling_fingerprint = asset.get("sampling_fingerprint")
        if not sampling_fingerprint or asset.get("sampling_status") != "complete":
            raise VisionError("SAMPLING_REQUIRED", "run S02 sampling before visual analysis")
        manifest = self.sampling.load_manifest(asset_id, sampling_fingerprint)
        if manifest is None:
            raise VisionError("SAMPLING_REQUIRED", "sampling manifest is missing")

        emit("verifying_source", 0, 0)
        source_sha = sha256_file(source_path)
        if source_sha != manifest["source"]["sha256"]:
            raise VisionError(
                "SOURCE_CHANGED", "source bytes differ from the sampled manifest; re-run sampling"
            )

        frame_refs = manifest["frames"]
        corrupt = [
            frame
            for frame in frame_refs
            if not Path(frame["path"]).is_file()
            or sha256_file(Path(frame["path"])) != frame["sha256"]
        ]
        if corrupt:
            emit("repairing_frames", 0, len(frame_refs))
            try:
                self.sampling.run(asset, source_path, profile=manifest["profile"])
            except SamplingError as exc:
                raise VisionError("FRAME_CACHE_CORRUPTED", f"could not repair frame cache: {exc}") from exc
            manifest = self.sampling.load_manifest(asset_id, sampling_fingerprint)
            if manifest is None:
                raise VisionError("FRAME_CACHE_CORRUPTED", "sampling manifest disappeared during repair")
            frame_refs = manifest["frames"]
            still_bad = [
                frame
                for frame in frame_refs
                if sha256_file(Path(frame["path"])) != frame["sha256"]
            ]
            if still_bad:
                raise VisionError("FRAME_CACHE_CORRUPTED", "frame cache hashes still mismatch after repair")

        selection = build_selection(manifest, selection_mode, user_ranges=user_ranges)
        selected_ids = set(selection["frame_ids"])
        frame_refs = [frame for frame in manifest["frames"] if frame["frame_id"] in selected_ids]
        if not frame_refs:
            raise VisionError("SAMPLING_PLAN_EMPTY", "selection produced no frames")
        sampling_total = len(manifest["frames"])

        resolved_method = method
        if resolved_method == "auto":
            resolved_method = "single_frame" if len(frame_refs) == 1 else "sequential_frames"
        if resolved_method == "multi_frame" and not self.allow_multi_frame:
            resolved_method = "sequential_frames"
        if resolved_method == "single_frame" and len(frame_refs) > 1:
            resolved_method = "sequential_frames"

        model = model_identity(self.model_path)
        runtime = runtime_versions(self.runtime_python)
        prompt_version = vlm_worker.PROMPT_VERSION
        frame_hashes = [[frame["frame_id"], frame["sha256"]] for frame in frame_refs]
        fingerprint = inference_fingerprint(
            source_sha256=source_sha,
            sampling_fingerprint=sampling_fingerprint,
            frame_hashes=frame_hashes,
            model=model,
            runtime=runtime,
            prompt_version=prompt_version,
            method=resolved_method,
            batch_size=self.batch_size,
            max_tokens=320,
            temperature=0.0,
            selection={"mode": selection_mode, "frame_ids": selection["frame_ids"]},
        )
        cache_dir = self._cache_dir(asset_id, fingerprint)
        cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        checkpoint_path = cache_dir / "analysis_checkpoint.json"
        evidence_path = cache_dir / "visual_evidence.json"

        checkpoint = self._load_checkpoint(checkpoint_path, fingerprint)
        analysis_id = checkpoint["analysis_id"] if checkpoint else str(uuid.uuid4())
        results: dict[str, dict[str, Any]] = dict(checkpoint["results"]) if checkpoint else {}
        valid_results = {
            frame_id: value
            for frame_id, value in results.items()
            if isinstance(value, dict) and isinstance(value.get("observation"), dict)
        }
        results = valid_results
        frame_cache = FrameResultCache(self.settings.data.inference / "frames")
        cache_keys: dict[str, str] = {}
        frame_cache_hits = 0
        for frame in frame_refs:
            key = FrameResultCache.key(
                source_sha=source_sha,
                frame_sha=str(frame["sha256"]),
                model=model,
                runtime=runtime,
                prompt_version=prompt_version,
                max_tokens=320,
                temperature=0.0,
            )
            cache_keys[frame["frame_id"]] = key
            if frame["frame_id"] in results:
                continue
            cached = frame_cache.get(key)
            if cached is not None:
                results[frame["frame_id"]] = {
                    "observation": cached["observation"],
                    "method": cached["method"],
                    "elapsed_s": 0.0,
                    "cached": True,
                }
                frame_cache_hits += 1
        if frame_cache_hits:
            self._write_checkpoint(checkpoint_path, fingerprint, analysis_id, resolved_method, results)

        with self.db.write() as conn:
            store.set_analysis_state(
                conn,
                asset_id,
                status="inferring",
                analysis_id=analysis_id,
                fingerprint=fingerprint,
                coverage=manifest.get("coverage_status"),
            )

        pending = [frame for frame in frame_refs if frame["frame_id"] not in results]
        total = len(frame_refs)
        committed = len(results)
        inferences = 0

        def commit_frame(event: dict[str, Any]) -> None:
            """Durably commit one validated worker frame before progress advances."""
            nonlocal inferences
            frame_id = event.get("frame_id")
            frame = next((item for item in frame_refs if item["frame_id"] == frame_id), None)
            if frame is None or frame_id in results:
                return
            try:
                observation = vlm_worker.validate_observation(event.get("observation"))
            except vlm_worker.WorkerError:
                # Invalid model output must remain unknown, never committed/cached.
                return
            frame_cache.put(cache_keys[frame_id], observation, method=event.get("method", resolved_method))
            inferences += 1
            results[frame_id] = {
                "observation": observation,
                "method": event.get("method", resolved_method),
                "elapsed_s": event.get("elapsed_s"),
                "generation_tokens": event.get("generation_tokens"),
                "prompt_tokens": event.get("prompt_tokens"),
                "streamed": True,
            }
            self._write_checkpoint(checkpoint_path, fingerprint, analysis_id, resolved_method, results)
            emit(
                "inferring", len(results), total,
                current_scene=frame["scene_id"], worker_state="committed",
            )
        emit(
            "inferring",
            committed,
            total,
            current_scene=(pending[0]["scene_id"] if pending else None),
            worker_state="starting",
        )

        for start in range(0, len(pending), self.batch_size):
            if cancel is not None and cancel.is_set():
                self._write_checkpoint(checkpoint_path, fingerprint, analysis_id, resolved_method, results)
                return self._finalize(
                    asset,
                    analysis_id,
                    resolved_method,
                    fingerprint,
                    cache_dir,
                    evidence_path,
                    manifest,
                    frame_refs,
                    results,
                    selection,
                    sampling_total,
                    frame_cache_hits,
                    inferences,
                    source_sha,
                    model,
                    runtime,
                    prompt_version,
                    started,
                    retries,
                    restarts,
                    status="cancelled",
                    warnings=["cancelled by operator; committed frames are reusable"],
                )

            batch = pending[start : start + self.batch_size]
            if self.require_governor:
                allowed, reason = GOVERNOR.can_start_heavy_worker()
                if not allowed:
                    raise VisionError(reason, "resource governor refused a heavy worker")
            owner = job_id or analysis_id
            try:
                HEAVY_SLOT.acquire(owner, timeout=self.request_timeout_s)
            except HeavySlotBusy as exc:
                raise VisionError("HEAVY_SLOT_BUSY", "another heavy model is already running") from exc
            try:
                batch_results = self._infer_with_retries(
                    asset, batch, resolved_method, cancel, checkpoint_path, fingerprint, analysis_id,
                    results, total, on_frame=commit_frame,
                )
            finally:
                HEAVY_SLOT.release(owner)

            for frame_id, value in batch_results.items():
                if value.get("observation") is None or value.get("cached") or value.get("streamed"):
                    continue
                try:
                    frame_cache.put(cache_keys[frame_id], value["observation"], method=value.get("method", resolved_method))
                except vlm_worker.WorkerError:
                    # Invalid observations stay in the evidence as unknown; never cache them.
                    pass
            results.update(batch_results)
            committed = len(results)
            self._write_checkpoint(checkpoint_path, fingerprint, analysis_id, resolved_method, results)
            emit(
                "inferring",
                committed,
                total,
                current_scene=batch[-1]["scene_id"],
                worker_state="running",
            )
            if any(value.get("worker_restart") for value in batch_results.values()):
                restarts += 1

        self._write_checkpoint(checkpoint_path, fingerprint, analysis_id, resolved_method, results)
        finished_status = "cancelled" if cancel is not None and cancel.is_set() else "complete"
        return self._finalize(
            asset,
            analysis_id,
            resolved_method,
            fingerprint,
            cache_dir,
            evidence_path,
            manifest,
            frame_refs,
            results,
            selection,
            sampling_total,
            frame_cache_hits,
            inferences,
            source_sha,
            model,
            runtime,
            prompt_version,
            started,
            retries,
            restarts,
            status=finished_status,
            warnings=["cancelled by operator; committed frames are reusable"] if finished_status == "cancelled" else None,
        )

    def _infer_with_retries(
        self,
        asset: dict[str, Any],
        batch: list[dict[str, Any]],
        method: str,
        cancel: threading.Event | None,
        checkpoint_path: Path,
        fingerprint: str,
        analysis_id: str,
        committed: dict[str, Any],
        total: int,
        *,
        on_frame: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        current = list(batch)
        attempt = 0
        while True:
            attempt += 1
            try:
                return self._infer_batch(asset, current, method, cancel, analysis_id, on_frame=on_frame)
            except VisionError as exc:
                if exc.code == "WORKER_CANCELLED":
                    return {}
                retryable = exc.code in {
                    "MODEL_OOM",
                    "MODEL_INFERENCE_FAILED",
                    "MODEL_OUTPUT_INVALID",
                    "MODEL_PROMPT_TEMPLATE_FAILED",
                    "WORKER_TIMEOUT",
                    "WORKER_CRASHED",
                }
                if not retryable or attempt >= MAX_ATTEMPTS:
                    # Never invent evidence: record the frames as failed and let the
                    # caller mark them unknown, while keeping committed results.
                    return {}
                self._write_checkpoint(
                    checkpoint_path, fingerprint, analysis_id, method, committed
                )
                current = [frame for frame in current if frame["frame_id"] not in committed]
                if not current:
                    return {}
                if exc.code == "MODEL_OOM" and len(current) > 1:
                    current = current[:1]
                time.sleep(min(2.0, 0.5 * attempt))

    def _infer_batch(
        self,
        asset: dict[str, Any],
        batch: list[dict[str, Any]],
        method: str,
        cancel: threading.Event | None,
        analysis_id: str,
        *,
        on_frame: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        request = {
            "request_version": vlm_worker.REQUEST_VERSION,
            "job_id": analysis_id,
            "asset_id": asset["asset_id"],
            "analysis_id": analysis_id,
            "model_path": str(self.model_path),
            "allowed_image_root": str(self.settings.data.sampling),
            "prompt": (
                vlm_worker.VISUAL_SEQUENCE_PROMPT
                if method == "multi_frame"
                else vlm_worker.VISUAL_PROMPT
            ),
            "prompt_version": vlm_worker.PROMPT_VERSION,
            "method": method,
            "frames": [
                {
                    "frame_id": frame["frame_id"],
                    "scene_id": frame["scene_id"],
                    "image_path": frame["path"],
                    "normalized_pts_ms": int(frame["normalized_pts_ms"]),
                    "sha256": frame["sha256"],
                }
                for frame in batch
            ],
            "max_tokens": 320,
            "temperature": 0.0,
            "seed": 0,
        }
        final = self._run_worker(request, cancel=cancel, on_frame=on_frame)
        if final.get("job_id") != analysis_id or final.get("asset_id") != asset["asset_id"]:
            raise VisionError("WORKER_PROTOCOL_INVALID", "worker result identity mismatch")
        if final.get("analysis_id") != analysis_id:
            raise VisionError("WORKER_PROTOCOL_INVALID", "worker analysis_id mismatch")
        returned = final.get("frames") or []
        expected = {frame["frame_id"] for frame in batch}
        seen: set[str] = set()
        results: dict[str, Any] = {}
        for entry in returned:
            frame_id = entry.get("frame_id")
            if frame_id not in expected or frame_id in seen:
                raise VisionError("WORKER_PROTOCOL_INVALID", f"unexpected/duplicate frame: {frame_id}")
            seen.add(frame_id)
            results[frame_id] = {
                "observation": entry.get("observation"),
                "method": entry.get("method", method),
                "elapsed_s": entry.get("elapsed_s"),
                "generation_tokens": entry.get("generation_tokens"),
                "prompt_tokens": entry.get("prompt_tokens"),
            }
        if seen != expected:
            raise VisionError("WORKER_PROTOCOL_INVALID", "worker skipped frames")
        return results

    def _finalize(
        self,
        asset: dict[str, Any],
        analysis_id: str,
        method: str,
        fingerprint: str,
        cache_dir: Path,
        evidence_path: Path,
        manifest: dict[str, Any],
        frame_refs: list[dict[str, Any]],
        results: dict[str, Any],
        selection: dict[str, Any],
        sampling_total: int,
        frame_cache_hits: int,
        inferences: int,
        source_sha: str,
        model: dict[str, Any],
        runtime: dict[str, Any],
        prompt_version: str,
        started: float,
        retries: int,
        restarts: int,
        *,
        status: str,
        warnings: list[str] | None = None,
    ) -> VisionOutcome:
        warnings = list(dict.fromkeys([*(manifest.get("warnings") or []), *(warnings or [])]))
        if manifest.get("coverage_status") != "sufficient":
            warnings.append(
                f"sampling coverage is {manifest.get('coverage_status')}: evidence is partial"
            )
        if selection.get("partial"):
            warnings.append(
                f"visual_inference_partial: {selection.get('selected_frames')} of "
                f"{selection.get('sampling_frames')} verified sampling frames were sent to the VLM"
            )
        evidence = build_evidence(
            analysis_id=analysis_id,
            asset_id=asset["asset_id"],
            source_sha256=source_sha,
            sampling_fingerprint=manifest["fingerprint"],
            sampling_profile=manifest.get("profile", "unknown"),
            coverage_status=manifest.get("coverage_status", "unknown"),
            method=method,
            prompt_version=prompt_version,
            model_identity=model,
            runtime_versions=runtime,
            frame_refs=frame_refs,
            results=results,
            visual_coverage=selection.get("visual_coverage"),
            warnings=warnings,
        )
        evidence["execution_status"] = status
        coverage = evidence.setdefault("visual_coverage", {})
        coverage["status"] = "partial" if coverage.get("partial") else "complete"
        coverage["coverage_claim"] = (
            "selected_verified_frames_only" if coverage.get("partial") else "all_sampling_frames"
        )
        atomic_write_json(evidence_path, evidence)
        observed = sum(1 for entry in evidence["frames"] if entry["interpretation_level"] == "observed")
        unknown = len(evidence["frames"]) - observed
        final_status = status
        if status == "complete" and unknown:
            final_status = "partial"
        with self.db.write() as conn:
            store.set_analysis_state(
                conn,
                asset["asset_id"],
                status=final_status,
                analysis_id=analysis_id,
                fingerprint=fingerprint,
                coverage=manifest.get("coverage_status"),
            )
        return VisionOutcome(
            analysis_id=analysis_id,
            status=final_status,
            method=method,
            fingerprint=fingerprint,
            cache_dir=cache_dir,
            evidence_path=evidence_path,
            frames_total=len(frame_refs),
            frames_observed=observed,
            frames_unknown=unknown,
            sampling_frames=sampling_total,
            selected_frames=len(frame_refs),
            selection_mode=selection.get("mode", "full"),
            frame_cache_hits=frame_cache_hits,
            inferences=inferences,
            worker_loads=getattr(self, "_worker_loads", 0),
            model_load_s=getattr(self, "_load_s_total", 0.0),
            coverage_status=manifest.get("coverage_status", "unknown"),
            elapsed_s=time.monotonic() - started,
            retries=retries,
            worker_restarts=restarts,
            peak_worker_rss_bytes=getattr(self, "_peak_worker_rss", 0),
            mlx_peak_memory_bytes=getattr(self, "_mlx_peak", 0),
            warnings=warnings,
        )

    def load_evidence(self, asset_id: str) -> dict[str, Any] | None:
        asset = store.get_asset(self.db.connection(), asset_id)
        fingerprint = asset.get("analysis_fingerprint")
        if not fingerprint:
            return None
        path = self._cache_dir(asset_id, fingerprint) / "visual_evidence.json"
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
