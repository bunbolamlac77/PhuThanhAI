"""Data-contract package: structural + semantic validation, migration, API models.

``contracts/*.schema.json`` are the structural authority and are loaded from the
repository, never from a URL. Semantic invariants that JSON Schema cannot express
(identity linkage, span containment, provenance separation) live in
``validators.py`` and are shared by the CLI, the API and the tests - there is
exactly one meaning of a timecode in this codebase.
"""

from __future__ import annotations

from phuthanhai.contracts.models import (
    ContractCheckReport,
    ContractKindReport,
    ErrorBody,
    HealthResponse,
    SessionResponse,
)

__all__ = [
    "ContractCheckReport",
    "ContractKindReport",
    "ErrorBody",
    "HealthResponse",
    "SessionResponse",
]
