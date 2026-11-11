"""API routes. Only endpoints that are actually implemented at S00 are exposed."""

from __future__ import annotations

import os
import time
from typing import Any

from fastapi import APIRouter, Request, Response

from phuthanhai import PRODUCT_VERSION
from phuthanhai.api.security import CAPABILITY_HEADER
from phuthanhai.config import CONTRACT_VERSION, OFFLINE_ENVIRONMENT
from phuthanhai.contracts.models import (
    ContractCheckReport,
    HealthResponse,
    SessionResponse,
)
from phuthanhai.contracts.selfcheck import run_contract_selfcheck
from phuthanhai.runtime.scheduler import HEAVY_SLOT

router = APIRouter(prefix="/api/v1")


@router.get("/health", response_model=HealthResponse)
def health(request: Request) -> HealthResponse:
    state = request.app.state
    settings = state.settings
    return HealthResponse(
        status="ok",
        product_version=PRODUCT_VERSION,
        contract_version=CONTRACT_VERSION,
        pid=state.pid,
        uptime_s=round(time.monotonic() - state.started_at, 3),
        bound_host=settings.host,
        loopback_only=settings.host in {"127.0.0.1", "::1", "localhost"},
        heavy_slot_busy=HEAVY_SLOT.busy,
        data_root_configured=settings.data.root.is_dir(),
        web_bundle_present=(settings.web_dist / "index.html").is_file(),
        offline_enforced=all(OFFLINE_ENVIRONMENT.get(key) == os.environ.get(key) for key in OFFLINE_ENVIRONMENT),
    )


@router.get("/session", response_model=SessionResponse)
def session(request: Request) -> SessionResponse:
    """Hand the capability to the same-origin UI only.

    Cross-origin pages cannot read this body (no CORS grant), and the token is
    never accepted via URL/query parameters.
    """

    policy = request.app.state.policy
    return SessionResponse(
        capability=policy.capability,
        header_name=CAPABILITY_HEADER,
        product_version=PRODUCT_VERSION,
    )


def _contract_report(request: Request) -> dict[str, Any]:
    settings = request.app.state.settings
    return run_contract_selfcheck(settings.contracts_dir, settings.fixtures_dir)


@router.get("/contracts", response_model=ContractCheckReport)
def contracts(request: Request) -> ContractCheckReport:
    return ContractCheckReport.model_validate(_contract_report(request))


@router.post("/contracts:recheck", response_model=ContractCheckReport)
def recheck_contracts(request: Request, response: Response) -> ContractCheckReport:
    """State-changing action: re-validate shipped contracts (capability required)."""

    report = _contract_report(request)
    response.headers["X-PhuThanhAI-Checked"] = "1"
    return ContractCheckReport.model_validate(report)
