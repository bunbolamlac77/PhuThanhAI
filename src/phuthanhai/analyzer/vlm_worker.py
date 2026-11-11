"""Isolated Qwen3-VL worker (runs under the discovered MLX runtime interpreter).

Contract
--------
* **stdin**: one JSON request object.
* **stdout**: JSONL protocol events only (no prints, no logs).
* **stderr**: human diagnostics, tracebacks, library chatter.

The worker never receives user notes. Its prompt is visual-only, so a user note
such as "boarding the ferry at 05:00" cannot become a visual "observation". Clock
time, names and locations are explicitly out of scope for the model.

Safety
------
* The image path must resolve inside the request's ``allowed_image_root``; the
  worker refuses arbitrary filesystem paths.
* Network access is blocked at the socket layer and the parent additionally runs
  this process inside ``sandbox-exec`` with ``deny network*`` during the gate.
* Environment variables are forced offline before MLX is imported.

Invoke as::

    python -m phuthanhai.analyzer.vlm_worker            # request on stdin
"""

from __future__ import annotations

import json
import os
import resource
import socket
import sys
import time
import traceback
from pathlib import Path
from typing import Any

REQUEST_VERSION = "1.0.0"
#: Bump whenever VISUAL_PROMPT changes; part of every evidence cache fingerprint.
PROMPT_VERSION = "visual-only-v1"

EXIT_OK = 0
EXIT_BAD_REQUEST = 2
EXIT_MODEL_LOAD_FAILED = 3
EXIT_INFERENCE_FAILED = 4
EXIT_OUTPUT_INVALID = 5

REQUIRED_KEYS = {
    "description",
    "objects",
    "actions",
    "camera",
    "lighting",
    "visible_text",
    "uncertainty",
}

VISUAL_PROMPT = (
    "You are a strict visual observer looking at a single still frame.\n"
    "Describe ONLY what is literally visible.\n"
    "Rules:\n"
    "- Do not guess anyone's name, identity or relationship.\n"
    "- Do not guess an exact location, date or time of day.\n"
    "- Do not invent speech, sound, music or events you cannot see.\n"
    "- Do not assert a long action from one still frame; if the pose is ambiguous, say so in uncertainty.\n"
    "- Prefer concrete visible nouns over interpretation.\n"
    "Return ONLY a JSON object with exactly these keys:\n"
    '  "description": string,\n'
    '  "objects": array of strings,\n'
    '  "actions": array of strings,\n'
    '  "camera": string or null,\n'
    '  "lighting": string or null,\n'
    '  "visible_text": array of strings,\n'
    '  "uncertainty": array of strings.\n'
    "No prose outside the JSON object."
)


class WorkerError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------- #
# Protocol helpers (importable without MLX, for unit tests)                    #
# --------------------------------------------------------------------------- #


def validate_request(payload: Any) -> dict[str, Any]:
    """Validate the worker request; raises :class:`WorkerError` on any problem."""

    if not isinstance(payload, dict):
        raise WorkerError("WORKER_REQUEST_INVALID", "request must be a JSON object")
    if payload.get("request_version") != REQUEST_VERSION:
        raise WorkerError(
            "WORKER_REQUEST_INVALID",
            f"request_version must be {REQUEST_VERSION!r}, got {payload.get('request_version')!r}",
        )
    job_id = payload.get("job_id")
    if not isinstance(job_id, str) or not job_id:
        raise WorkerError("WORKER_REQUEST_INVALID", "job_id must be a non-empty string")

    model_path = payload.get("model_path")
    if not isinstance(model_path, str) or not model_path:
        raise WorkerError("MODEL_SNAPSHOT_MISSING", "model_path is required")
    model_dir = Path(model_path).expanduser()
    if not model_dir.is_dir():
        raise WorkerError("MODEL_SNAPSHOT_MISSING", f"model snapshot not found: {model_dir.name}")
    for required in ("config.json", "model.safetensors"):
        if not (model_dir / required).is_file():
            raise WorkerError("MODEL_SNAPSHOT_MISSING", f"model snapshot missing {required}")

    image_path = payload.get("image_path")
    if not isinstance(image_path, str) or not image_path:
        raise WorkerError("WORKER_REQUEST_INVALID", "image_path is required")
    root_raw = payload.get("allowed_image_root")
    if not isinstance(root_raw, str) or not root_raw:
        raise WorkerError("WORKER_REQUEST_INVALID", "allowed_image_root is required")
    allowed_root = Path(root_raw).expanduser().resolve()
    image = Path(image_path).expanduser().resolve()
    if allowed_root not in image.parents and image != allowed_root:
        raise WorkerError("SECURITY_DENIED", "image_path is outside the allowed image root")
    if not image.is_file():
        raise WorkerError("WORKER_REQUEST_INVALID", f"image not found: {image.name}")

    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise WorkerError("WORKER_REQUEST_INVALID", "prompt is required")
    if len(prompt) > 8000:
        raise WorkerError("WORKER_REQUEST_INVALID", "prompt exceeds 8000 characters")

    max_tokens = payload.get("max_tokens", 320)
    if not isinstance(max_tokens, int) or not (1 <= max_tokens <= 2048):
        raise WorkerError("WORKER_REQUEST_INVALID", "max_tokens must be an int in [1, 2048]")

    temperature = payload.get("temperature", 0.0)
    if not isinstance(temperature, (int, float)) or not (0.0 <= float(temperature) <= 2.0):
        raise WorkerError("WORKER_REQUEST_INVALID", "temperature must be within [0, 2]")

    return {
        "request_version": REQUEST_VERSION,
        "job_id": job_id,
        "model_path": str(model_dir),
        "image_path": str(image),
        "allowed_image_root": str(allowed_root),
        "prompt": prompt,
        "max_tokens": max_tokens,
        "temperature": float(temperature),
        "seed": payload.get("seed", 0),
    }


def parse_model_json(text: str) -> dict[str, Any]:
    """Extract and validate the model's JSON observation from raw completion text."""

    raw = text.strip()
    if raw.startswith("```"):
        first_newline = raw.find("\n")
        raw = raw[first_newline + 1 :] if first_newline != -1 else raw
        if raw.rstrip().endswith("```"):
            raw = raw.rstrip()[: -3]
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise WorkerError("MODEL_OUTPUT_INVALID", "model output contains no JSON object")
    candidate = raw[start : end + 1]
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise WorkerError("MODEL_OUTPUT_INVALID", f"model JSON is not decodable: {exc}") from exc
    if not isinstance(parsed, dict):
        raise WorkerError("MODEL_OUTPUT_INVALID", "model JSON is not an object")

    missing = REQUIRED_KEYS - set(parsed)
    extra = set(parsed) - REQUIRED_KEYS
    if missing:
        raise WorkerError("MODEL_OUTPUT_INVALID", f"model JSON missing keys: {sorted(missing)}")
    if extra:
        raise WorkerError("MODEL_OUTPUT_INVALID", f"model JSON has unexpected keys: {sorted(extra)}")

    if not isinstance(parsed["description"], str):
        raise WorkerError("MODEL_OUTPUT_INVALID", "description must be a string")
    for key in ("objects", "actions", "visible_text", "uncertainty"):
        value = parsed[key]
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise WorkerError("MODEL_OUTPUT_INVALID", f"{key} must be an array of strings")
    for key in ("camera", "lighting"):
        if parsed[key] is not None and not isinstance(parsed[key], str):
            raise WorkerError("MODEL_OUTPUT_INVALID", f"{key} must be a string or null")
    return parsed


# --------------------------------------------------------------------------- #
# Emission                                                                     #
# --------------------------------------------------------------------------- #


def emit(event: dict[str, Any]) -> None:
    """Write one protocol event to stdout and flush immediately."""

    sys.stdout.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
    sys.stdout.flush()


def peak_rss_bytes() -> int:
    # macOS reports ru_maxrss in bytes (Linux reports KiB); this worker targets macOS.
    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)


# --------------------------------------------------------------------------- #
# Offline enforcement                                                          #
# --------------------------------------------------------------------------- #


def enforce_offline_environment() -> None:
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    os.environ["DO_NOT_TRACK"] = "1"


def block_network() -> None:
    """Deny AF_INET/AF_INET6 connections in this process.

    This makes "offline" verifiable rather than aspirational: if any library tries
    to reach the network it fails loudly here instead of silently succeeding.
    AF_UNIX is left alone because local IPC is legitimate.
    """

    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def _denied(address: Any) -> None:
        family = address[0] if isinstance(address, tuple) and address else None
        if family in (socket.AF_INET, socket.AF_INET6):
            raise OSError("network access is disabled in the PhuThanhAI model worker")

    def guarded_connect(self, address):  # type: ignore[no-untyped-def]
        _denied(address)
        return original_connect(self, address)

    def guarded_connect_ex(self, address):  # type: ignore[no-untyped-def]
        _denied(address)
        return original_connect_ex(self, address)

    socket.socket.connect = guarded_connect  # type: ignore[assignment]
    socket.socket.connect_ex = guarded_connect_ex  # type: ignore[assignment]

    def guarded_create_connection(address, *args, **kwargs):  # type: ignore[no-untyped-def]
        _denied(address)
        raise OSError("network access is disabled in the PhuThanhAI model worker")

    socket.create_connection = guarded_create_connection  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# Main                                                                         #
# --------------------------------------------------------------------------- #


def _import_versions() -> dict[str, str]:
    versions: dict[str, str] = {}
    try:
        from importlib.metadata import version

        for name in ("mlx", "mlx-vlm", "mlx-lm", "transformers"):
            try:
                versions[name] = version(name)
            except Exception:  # noqa: BLE001 - metadata only, never fatal
                versions[name] = "unknown"
    except Exception:  # noqa: BLE001
        pass
    return versions


def run(request: dict[str, Any]) -> int:
    import mlx.core as mx  # imported lazily: only inside the isolated worker
    from mlx_vlm import apply_chat_template, generate, load

    started = time.monotonic()
    emit(
        {
            "type": "worker_ready",
            "job_id": request["job_id"],
            "pid": os.getpid(),
            "python": sys.version.split()[0],
            "executable": sys.executable,
            "versions": _import_versions(),
            "metal_available": bool(mx.metal.is_available()),
            "device": str(mx.default_device()),
            "peak_rss_bytes": peak_rss_bytes(),
        }
    )

    load_started = time.monotonic()
    try:
        model, processor = load(request["model_path"])
    except Exception as exc:  # noqa: BLE001 - surfaced as a structured failure
        emit(
            {
                "type": "error",
                "code": "MODEL_RUNTIME_INCOMPATIBLE",
                "job_id": request["job_id"],
                "message": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(limit=6),
            }
        )
        return EXIT_MODEL_LOAD_FAILED
    load_s = time.monotonic() - load_started
    emit(
        {
            "type": "model_loaded",
            "job_id": request["job_id"],
            "load_s": round(load_s, 3),
            "peak_rss_bytes": peak_rss_bytes(),
        }
    )

    infer_started = time.monotonic()
    # The qwen3_vl processor requires exactly one image placeholder for one image.
    # Rendering the chat template inserts it; passing the raw string would make the
    # model reject the request ("0 image placeholders, but 1 image supplied").
    try:
        templated_prompt = apply_chat_template(
            processor,
            model.config,
            request["prompt"],
            num_images=1,
        )
    except Exception as exc:  # noqa: BLE001
        emit(
            {
                "type": "error",
                "code": "MODEL_PROMPT_TEMPLATE_FAILED",
                "job_id": request["job_id"],
                "message": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(limit=6),
            }
        )
        return EXIT_INFERENCE_FAILED

    try:
        result = generate(
            model,
            processor,
            prompt=templated_prompt,
            image=[request["image_path"]],
            max_tokens=request["max_tokens"],
            temperature=request["temperature"],
            verbose=False,
        )
    except Exception as exc:  # noqa: BLE001
        emit(
            {
                "type": "error",
                "code": "MODEL_INFERENCE_FAILED",
                "job_id": request["job_id"],
                "message": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(limit=6),
            }
        )
        return EXIT_INFERENCE_FAILED

    elapsed_s = time.monotonic() - infer_started
    text = getattr(result, "text", "") or ""
    try:
        parsed = parse_model_json(text)
    except WorkerError as exc:
        emit(
            {
                "type": "error",
                "code": exc.code,
                "job_id": request["job_id"],
                "message": str(exc),
                "raw_text": text[:4000],
            }
        )
        return EXIT_OUTPUT_INVALID

    emit(
        {
            "type": "result",
            "job_id": request["job_id"],
            "observation": parsed,
            "raw_text": text[:4000],
            "elapsed_s": round(elapsed_s, 3),
            "load_s": round(load_s, 3),
            "total_s": round(time.monotonic() - started, 3),
            "peak_rss_bytes": peak_rss_bytes(),
            "mlx_peak_memory_bytes": int(getattr(result, "peak_memory", 0.0) * 1024**3),
            "prompt_version": PROMPT_VERSION,
            "prompt_template": "apply_chat_template(num_images=1)",
            "prompt_chars": len(templated_prompt),
            "prompt_tokens": getattr(result, "prompt_tokens", None),
            "generation_tokens": getattr(result, "generation_tokens", None),
            "model_path": request["model_path"],
        }
    )
    return EXIT_OK


def main() -> int:
    try:
        raw = sys.stdin.read()
    except OSError as exc:
        print(f"cannot read request: {exc}", file=sys.stderr)
        return EXIT_BAD_REQUEST
    if not raw.strip():
        print("empty request on stdin", file=sys.stderr)
        return EXIT_BAD_REQUEST

    try:
        payload = json.loads(raw)
        request = validate_request(payload)
    except json.JSONDecodeError as exc:
        print(f"invalid JSON request: {exc}", file=sys.stderr)
        emit({"type": "error", "code": "WORKER_REQUEST_INVALID", "message": f"invalid JSON: {exc}"})
        return EXIT_BAD_REQUEST
    except WorkerError as exc:
        print(f"{exc.code}: {exc}", file=sys.stderr)
        emit({"type": "error", "code": exc.code, "message": str(exc)})
        return EXIT_BAD_REQUEST

    enforce_offline_environment()
    block_network()
    try:
        return run(request)
    except Exception as exc:  # noqa: BLE001 - last-resort guard, still structured
        print(traceback.format_exc(), file=sys.stderr)
        emit(
            {
                "type": "error",
                "code": "WORKER_CRASHED",
                "job_id": request["job_id"],
                "message": f"{type(exc).__name__}: {exc}",
            }
        )
        return EXIT_INFERENCE_FAILED


if __name__ == "__main__":
    sys.exit(main())
