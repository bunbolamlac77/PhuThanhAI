"""Pydantic models for the local API surface.

Only endpoints that actually exist at S00 are modelled. Adding speculative models
for S01 endpoints would create a fake contract, so asset/job/annotation models are
introduced together with their implementations.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


class ErrorBody(BaseModel):
    """Stable error envelope. UI renders ``code`` + ``user_message`` only."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(description="Stable machine code, e.g. SECURITY_DENIED")
    user_message: str = Field(description="Short, non-technical message safe to display")
    retryable: bool = False
    next_action: str | None = None
    details: dict[str, Any] | None = Field(
        default=None, description="Redacted diagnostics; never contains absolute private paths"
    )


class HealthResponse(BaseModel):
    """Liveness + honest capability declaration for the running backend."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "degraded"]
    product_version: str
    contract_version: str
    pid: int
    uptime_s: float
    bound_host: str
    loopback_only: bool
    heavy_slot_busy: bool
    model_loaded_in_process: Literal[False] = False
    data_root_configured: bool
    web_bundle_present: bool
    offline_enforced: bool
    time: datetime = Field(default_factory=utc_now)


class SessionResponse(BaseModel):
    """Loopback session capability handed to the same-origin UI only.

    The value is delivered in a response body, never in a URL, query string or log
    line, and is required as a header on state-changing requests (CSRF defence).
    """

    model_config = ConfigDict(extra="forbid")

    capability: str
    header_name: str
    product_version: str


class ContractKindReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    schema_valid: bool
    semantic_valid: bool
    fixtures_checked: list[str] = Field(default_factory=list)
    negative_fixtures_rejected: list[str] = Field(default_factory=list)
    error: str | None = None


class ContractCheckReport(BaseModel):
    """Live self-check of the shipped contracts and fixtures."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["pass", "fail"]
    checked_at: datetime = Field(default_factory=utc_now)
    contracts_dir_present: bool
    kinds: list[ContractKindReport]
    portable_roundtrip_ok: bool
    checksum_tamper_rejected: bool
    error: str | None = None
