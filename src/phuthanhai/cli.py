"""Command-line interface: ``doctor``, ``serve``, ``validate``.

The CLI and the server share one implementation of configuration, validation and
security; there is no second code path that could disagree about a timecode or a
contract version.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
from pathlib import Path

from phuthanhai import PRODUCT_VERSION
from phuthanhai.config import DEFAULT_HOST, DEFAULT_PORT, ConfigError, Settings
from phuthanhai.contracts.validators import (
    ContractError,
    load_schemas,
    parse_portable_workspace_md,
    validate_document,
)
from phuthanhai.doctor import DoctorReport, render_text, run_doctor
from phuthanhai.utils.logging import configure

LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
EXIT_OK = 0
EXIT_GATE_FAIL = 1
EXIT_USAGE = 2


def pick_free_port(host: str) -> int:
    """Reserve a free loopback port by binding port 0, then release it."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def _cmd_doctor(args: argparse.Namespace) -> int:
    settings = Settings.load()
    report: DoctorReport = run_doctor(settings, offline=args.offline, verify_hashes=args.deep)
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render_text(report))
    return EXIT_OK if not report.failures else EXIT_GATE_FAIL


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    if args.host not in LOOPBACK_HOSTS:
        print(
            f"refusing to bind {args.host!r}: PhuThanhAI only serves on loopback",
            file=sys.stderr,
        )
        return EXIT_USAGE

    settings = Settings.load()
    settings.data.ensure()
    configure(log_file=settings.data.logs / "backend.jsonl")

    port = args.port or pick_free_port(args.host)
    from phuthanhai.api.app import SingleInstanceError, create_app

    app = create_app(settings, port=port)
    url = f"http://{args.host}:{port}"
    print(f"PhuThanhAI {PRODUCT_VERSION} serving {url} (loopback only)", flush=True)
    try:
        uvicorn.run(app, host=args.host, port=port, log_level="warning", access_log=False)
    except SingleInstanceError as exc:
        print(f"{exc.code}: {exc}", file=sys.stderr)
        return EXIT_GATE_FAIL
    return EXIT_OK


def _cmd_validate(args: argparse.Namespace) -> int:
    settings = Settings.load()
    path = Path(args.input).expanduser()
    if not path.is_file():
        print(f"input not found: {path.name}", file=sys.stderr)
        return EXIT_USAGE

    try:
        schemas = load_schemas(settings.contracts_dir)
        if path.suffix.lower() == ".md":
            document = parse_portable_workspace_md(path.read_text(encoding="utf-8"))
        else:
            document = json.loads(path.read_text(encoding="utf-8"))
        schema_name = validate_document(schemas, document, semantic=True)
    except (ContractError, json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
        code = getattr(exc, "code", "VALIDATION_FAILED")
        payload = {"status": "fail", "code": code, "message": str(exc), "input": path.name}
        print(json.dumps(payload, ensure_ascii=False, indent=2) if args.json else f"FAIL {code}: {exc}")
        return EXIT_USAGE

    payload = {
        "status": "pass",
        "contract": schema_name,
        "kind": document.get("kind"),
        "schema_version": document.get("schema_version"),
        "input": path.name,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"PASS {payload['kind']} {payload['schema_version']} ({schema_name}) · {path.name}")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="phuthanhai", description=f"PhuThanhAI {PRODUCT_VERSION}")
    parser.add_argument("--version", action="version", version=f"PhuThanhAI {PRODUCT_VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="verify environment, models, runtime and security")
    doctor.add_argument("--offline", action="store_true", help="enforce and verify the offline guard")
    doctor.add_argument("--json", action="store_true", help="machine-readable report")
    doctor.add_argument(
        "--deep",
        action="store_true",
        help="also verify model artifact SHA-256 against the install manifest (slow, reads ~3 GiB)",
    )
    doctor.set_defaults(func=_cmd_doctor)

    serve = sub.add_parser("serve", help="run the loopback FastAPI backend")
    serve.add_argument("--host", default=DEFAULT_HOST, help="loopback host (default 127.0.0.1)")
    serve.add_argument("--port", type=int, default=DEFAULT_PORT, help="port; 0 picks a free one")
    serve.set_defaults(func=_cmd_serve)

    validate = sub.add_parser("validate", help="validate a portable report or JSON contract object")
    validate.add_argument("--input", required=True, help="path to .md or .json")
    validate.add_argument("--json", action="store_true")
    validate.set_defaults(func=_cmd_validate)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ConfigError as exc:
        print(f"{exc.code}: {exc}", file=sys.stderr)
        return EXIT_GATE_FAIL
    except KeyboardInterrupt:
        return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
