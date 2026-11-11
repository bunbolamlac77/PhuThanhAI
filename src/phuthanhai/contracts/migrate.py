"""Backward-compatible migration from ProjectBundle/1.0.0 to WorkspacePackage/1.0.0.

The V2 objects are frozen (``additionalProperties: false``), so V3 never adds
fields to them. It wraps: ``legacy_bundle`` keeps the V2 object untouched and
``extension`` carries reviews, evidence, corrections and conflicts.

``upgrade`` is idempotent: applying it twice must not duplicate annotations,
change identifiers or move timestamps. That property is covered by tests.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from phuthanhai.contracts.validators import (
    ContractError,
    SchemaSet,
    load_schemas,
    semantic_validate_package,
    validate_document,
)


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).astimezone().isoformat(timespec="seconds")


def new_uuid4() -> str:
    return str(uuid.uuid4())


def empty_extension(project_id: str, *, created_at: str | None = None) -> dict[str, Any]:
    """A valid WorkspaceExtension/1.0.0 with no review data.

    Choosing empty arrays (not absent keys) is deliberate: an imported V2 project
    is *known to have no V3 overlay yet*, which is different from an unknown state.
    """

    return {
        "kind": "phuthanhai.workspace_extension",
        "schema_version": "1.0.0",
        "project_id": project_id,
        "revision": 0,
        "created_at": created_at or _now_iso(),
        "reviews": [],
        "evidence": [],
        "corrections": [],
        "conflicts": [],
    }


def wrap_bundle(
    bundle: dict[str, Any],
    *,
    schemas: SchemaSet | None = None,
    package_id: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Validate a V2 bundle and wrap it in an empty V3 package."""

    schemas = schemas or load_schemas(_default_contracts_dir())
    if bundle.get("kind") != "phuthanhai.project_bundle":
        raise ContractError(
            "UNSUPPORTED_CONTRACT_KIND",
            f"expected phuthanhai.project_bundle, got {bundle.get('kind')!r}",
        )
    # Structural validation of the bundle itself.
    validate_document(schemas, bundle, semantic=False)

    project_id = bundle["project_id"]
    package = {
        "kind": "phuthanhai.workspace_package",
        "schema_version": "1.0.0",
        "package_id": package_id or new_uuid4(),
        "created_at": created_at or _now_iso(),
        "project_id": project_id,
        "legacy_bundle": bundle,
        "extension": empty_extension(project_id, created_at=created_at),
        "warnings": ["migrated_from_project_bundle_1.0.0"],
    }
    # Full semantic validation of the combined object.
    semantic_validate_package(package)
    return package


def upgrade(document: dict[str, Any], *, schemas: SchemaSet | None = None) -> dict[str, Any]:
    """Idempotently upgrade a ProjectBundle or WorkspacePackage to a package.

    * ``workspace_package`` -> returned unchanged (already upgraded).
    * ``project_bundle``    -> wrapped once, empty extension.
    * anything else         -> ``UNSUPPORTED_CONTRACT_KIND``.
    """

    schemas = schemas or load_schemas(_default_contracts_dir())
    kind = document.get("kind")

    if kind == "phuthanhai.workspace_package":
        validate_document(schemas, document, semantic=True)
        return document
    if kind == "phuthanhai.project_bundle":
        return wrap_bundle(document, schemas=schemas)
    if kind == "phuthanhai.video_analysis":
        raise ContractError(
            "MIGRATION_NEEDS_PROJECT",
            "an AnalysisReport must be wrapped in a ProjectBundle before upgrade; "
            "per-clip import lands in S04",
        )
    raise ContractError("UNSUPPORTED_CONTRACT_KIND", f"cannot upgrade kind {kind!r}")


def _default_contracts_dir():
    from phuthanhai.config import Settings

    return Settings.load().contracts_dir
