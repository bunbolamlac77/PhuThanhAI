"""Self-check of the shipped V2/V3 contracts and fixtures.

Runs the *same* validators the CLI and API use, so the application cannot drift
from ``tools/validate_specs.py``. This checks data shapes only; it makes no claim
about model quality or application behaviour.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from phuthanhai.contracts.migrate import empty_extension
from phuthanhai.contracts.validators import (
    ContractError,
    SchemaSet,
    load_schemas,
    parse_portable_workspace_md,
    payload_sha256,
    semantic_validate_package,
    validate_document,
)

GOLDEN_V2 = "golden_v2.project_bundle.json"
GOLDEN_V3 = "golden_v3.workspace_package.json"
GOLDEN_V3_PORTABLE = "golden_v3.workspace_report_portable.md"
NEGATIVE_FIXTURES = ("invalid_v3.cross_project.json", "invalid_v3.out_of_bounds_span.json")


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def run_contract_selfcheck(contracts_dir: Path, fixtures_dir: Path) -> dict[str, Any]:
    """Return a JSON-serialisable report; never raises for data problems."""

    report: dict[str, Any] = {
        "status": "fail",
        "contracts_dir_present": contracts_dir.is_dir(),
        "kinds": [],
        "portable_roundtrip_ok": False,
        "checksum_tamper_rejected": False,
        "error": None,
    }
    try:
        schemas: SchemaSet = load_schemas(contracts_dir)
    except ContractError as exc:
        report["error"] = f"{exc.code}: {exc}"
        return report

    fixture_failures: list[str] = []

    # --- V2 legacy bundle -------------------------------------------------- #
    v2_report = {
        "name": "project_bundle",
        "schema_valid": False,
        "semantic_valid": False,
        "fixtures_checked": [],
        "negative_fixtures_rejected": [],
        "error": None,
    }
    try:
        bundle = _read_json(fixtures_dir / GOLDEN_V2)
        validate_document(schemas, bundle, semantic=False)
        v2_report["schema_valid"] = True
        v2_report["fixtures_checked"].append(GOLDEN_V2)
        # Semantic meaning of a V2 bundle is exercised through an empty V3 envelope.
        validate_document(schemas, bundle, semantic=False)
        package = {
            "kind": "phuthanhai.workspace_package",
            "schema_version": "1.0.0",
            "package_id": "00000000-0000-4000-8000-000000000000",
            "created_at": bundle["created_at"],
            "project_id": bundle["project_id"],
            "legacy_bundle": bundle,
            "extension": empty_extension(bundle["project_id"], created_at=bundle["created_at"]),
            "warnings": [],
        }
        semantic_validate_package(package)
        v2_report["semantic_valid"] = True
    except (ContractError, OSError, KeyError, ValueError) as exc:
        fixture_failures.append(f"V2: {exc}")
        v2_report["error"] = str(exc)
    report["kinds"].append(v2_report)

    # --- V3 package -------------------------------------------------------- #
    v3_report = {
        "name": "workspace_package",
        "schema_valid": False,
        "semantic_valid": False,
        "fixtures_checked": [],
        "negative_fixtures_rejected": [],
        "error": None,
    }
    package: dict[str, Any] | None = None
    try:
        package = _read_json(fixtures_dir / GOLDEN_V3)
        validate_document(schemas, package, semantic=False)
        v3_report["schema_valid"] = True
        v3_report["fixtures_checked"].append(GOLDEN_V3)
        validate_document(schemas, package, semantic=True)
        v3_report["semantic_valid"] = True
    except (ContractError, OSError, KeyError, ValueError) as exc:
        fixture_failures.append(f"V3: {exc}")
        v3_report["error"] = str(exc)

    for name in NEGATIVE_FIXTURES:
        try:
            bad = _read_json(fixtures_dir / name)
            validate_document(schemas, bad, semantic=True)
        except ContractError as exc:
            v3_report["negative_fixtures_rejected"].append(f"{name}: {exc.code}")
        except (OSError, ValueError) as exc:
            fixture_failures.append(f"{name}: {exc}")
        else:
            fixture_failures.append(f"{name}: negative fixture was accepted")

    report["kinds"].append(v3_report)

    # --- Portable Markdown roundtrip --------------------------------------- #
    if package is not None:
        try:
            portable_text = (fixtures_dir / GOLDEN_V3_PORTABLE).read_text(encoding="utf-8")
            parsed = parse_portable_workspace_md(portable_text)
            report["portable_roundtrip_ok"] = payload_sha256(parsed) == payload_sha256(package)
            tampered = portable_text.replace(
                f"payload_sha256: {payload_sha256(package)}",
                f"payload_sha256: {'0' * 64}",
                1,
            )
            try:
                parse_portable_workspace_md(tampered)
            except ContractError as exc:
                report["checksum_tamper_rejected"] = exc.code == "PORTABLE_CHECKSUM_MISMATCH"
            else:
                fixture_failures.append("tampered portable report was accepted")
        except (ContractError, OSError) as exc:
            fixture_failures.append(f"portable: {exc}")

    if fixture_failures:
        report["error"] = "; ".join(fixture_failures)
    else:
        report["status"] = "pass"
    return report
