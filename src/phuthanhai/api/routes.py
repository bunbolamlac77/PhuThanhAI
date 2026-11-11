"""API routes for S01: projects, assets, streaming, previews, notes, reviews.

Every filesystem access goes through :class:`LibraryService.resolve_asset_file`,
which only accepts a registered ``asset_id`` and re-checks containment inside the
project root. No route accepts a filesystem path for playback.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import FileResponse, StreamingResponse

from phuthanhai import PRODUCT_VERSION
from phuthanhai.api.schemas import (
    AnnotationCreate,
    AnnotationMutation,
    AnnotationOut,
    AnnotationUpdate,
    AssetListResponse,
    AssetOut,
    CacheStats,
    PreviewInfo,
    ProjectCreate,
    ProjectOut,
    ProxyResponse,
    ReviewOut,
    ReviewUpdate,
    AnalysisSummary,
    AnalyzeRequest,
    JobOut,
    SampleRequest,
    SamplingSummary,
    ContextLabelCreate,
    ContextReviewRequest,
    ProfileOut,
    ProfileUpdate,
    ReportBuildOut,
    ReportBuildRequest,
    ReportFilesOut,
    ScanRequest,
    ScanResponse,
)
from phuthanhai.api.security import CAPABILITY_HEADER
from phuthanhai.config import CONTRACT_VERSION, OFFLINE_ENVIRONMENT
from phuthanhai.contracts.models import ContractCheckReport, HealthResponse, SessionResponse
from phuthanhai.contracts.selfcheck import run_contract_selfcheck
from phuthanhai.library.preview import RangeNotSatisfiable, iter_file_range, parse_range_header
from phuthanhai.library.store import StoreError
from phuthanhai.runtime.scheduler import HEAVY_SLOT

router = APIRouter(prefix="/api/v1")

_MEDIA_TYPES = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
    ".avi": "video/x-msvideo",
}


def _library(request: Request) -> Any:
    return request.app.state.library


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
    policy = request.app.state.policy
    return SessionResponse(
        capability=policy.capability,
        header_name=CAPABILITY_HEADER,
        product_version=PRODUCT_VERSION,
    )


# --------------------------------------------------------------------------- #
# Projects                                                                     #
# --------------------------------------------------------------------------- #


@router.post("/projects", response_model=ProjectOut, status_code=201)
def create_project(payload: ProjectCreate, request: Request, response: Response) -> ProjectOut:
    project = _library(request).create_project(payload.name, payload.root_path)
    if not project.get("created"):
        response.status_code = 200
    return ProjectOut.model_validate(project)


@router.get("/projects", response_model=list[ProjectOut])
def list_projects(request: Request) -> list[ProjectOut]:
    return [ProjectOut.model_validate(project) for project in _library(request).list_projects()]


@router.get("/projects/{project_id}", response_model=ProjectOut)
def get_project(project_id: str, request: Request) -> ProjectOut:
    return ProjectOut.model_validate(_library(request).get_project(project_id))


@router.post("/projects/{project_id}/assets:scan", response_model=ScanResponse)
def scan_project(project_id: str, payload: ScanRequest, request: Request) -> ScanResponse:
    stats = _library(request).scan(project_id, payload.directory, max_files=payload.max_files)
    return ScanResponse.model_validate(stats.to_dict())


@router.post("/projects/{project_id}/assets:refresh")
def refresh_project(project_id: str, request: Request) -> dict[str, int]:
    return _library(request).refresh_sources(project_id)


# --------------------------------------------------------------------------- #
# Assets                                                                       #
# --------------------------------------------------------------------------- #


@router.get("/assets", response_model=AssetListResponse)
def list_assets(
    request: Request,
    project_id: str = Query(min_length=1),
    q: str | None = None,
    cursor: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    sort: str = "natural",
    source_state: str | None = None,
    analysis_status: str | None = None,
    favorite: bool | None = None,
    min_rating: int | None = Query(default=None, ge=1, le=5),
    has_note: bool | None = None,
    label: str | None = None,
) -> AssetListResponse:
    page = _library(request).list_assets(
        project_id,
        q=q,
        cursor=cursor,
        limit=limit,
        sort=sort,
        source_state=source_state,
        analysis_status=analysis_status,
        favorite=favorite,
        min_rating=min_rating,
        has_note=has_note,
        label=label,
    )
    return AssetListResponse.model_validate(page)


@router.get("/assets/{asset_id}", response_model=AssetOut)
def get_asset(asset_id: str, request: Request) -> AssetOut:
    return AssetOut.model_validate(_library(request).get_asset(asset_id))


def _serve_file(request: Request, path: Path, media_type: str) -> Response:
    size = path.stat().st_size
    try:
        byte_range = parse_range_header(request.headers.get("range"), size)
    except RangeNotSatisfiable:
        return Response(
            status_code=416,
            headers={"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"},
        )

    common_headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    }
    if byte_range is None:
        return StreamingResponse(
            iter_file_range(path, 0, size),
            media_type=media_type,
            headers={**common_headers, "Content-Length": str(size)},
        )

    start, end = byte_range
    headers = {
        **common_headers,
        "Content-Range": f"bytes {start}-{end - 1}/{size}",
        "Content-Length": str(end - start),
    }
    return StreamingResponse(
        iter_file_range(path, start, end),
        status_code=206,
        media_type=media_type,
        headers=headers,
    )


@router.get("/assets/{asset_id}/stream")
def stream_asset(asset_id: str, request: Request) -> Response:
    _, path = _library(request).resolve_asset_file(asset_id)
    media_type = _MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")
    return _serve_file(request, path, media_type)


@router.get("/assets/{asset_id}/proxy-stream")
def stream_proxy(asset_id: str, request: Request) -> Response:
    path = _library(request).proxy_file(asset_id)
    return _serve_file(request, path, "video/mp4")


@router.get("/assets/{asset_id}/thumbnail")
def thumbnail(asset_id: str, request: Request) -> Response:
    path, _asset = _library(request).thumbnail(asset_id)
    return FileResponse(
        path,
        media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff"},
    )


@router.post("/assets/{asset_id}/proxy", response_model=ProxyResponse)
def create_proxy(asset_id: str, request: Request) -> ProxyResponse:
    return ProxyResponse.model_validate(_library(request).proxy(asset_id))


@router.get("/assets/{asset_id}/preview", response_model=PreviewInfo)
def preview_info(asset_id: str, request: Request) -> PreviewInfo:
    return PreviewInfo.model_validate(_library(request).preview_info(asset_id))


# --------------------------------------------------------------------------- #
# S02 sampling jobs                                                            #
# --------------------------------------------------------------------------- #


@router.post("/assets/{asset_id}:sample", response_model=JobOut, status_code=202)
def start_sampling(asset_id: str, payload: SampleRequest, request: Request) -> JobOut:
    return JobOut.model_validate(_library(request).start_sampling(asset_id, payload.profile))


@router.post("/assets/{asset_id}:analyze", response_model=JobOut, status_code=202)
def start_analysis(asset_id: str, payload: AnalyzeRequest, request: Request) -> JobOut:
    return JobOut.model_validate(
        _library(request).start_analysis(
            asset_id, payload.method, payload.selection_mode, payload.user_ranges
        )
    )


@router.get("/assets/{asset_id}/analysis", response_model=AnalysisSummary)
def analysis_summary(asset_id: str, request: Request) -> AnalysisSummary:
    return AnalysisSummary.model_validate(_library(request).analysis_summary(asset_id))


@router.get("/assets/{asset_id}/sampling", response_model=SamplingSummary)
def sampling_summary(asset_id: str, request: Request) -> SamplingSummary:
    return SamplingSummary.model_validate(_library(request).sampling_summary(asset_id))


@router.get("/projects/{project_id}/profile", response_model=ProfileOut)
def get_project_profile(project_id: str, request: Request) -> ProfileOut:
    project = _library(request).get_project(project_id)
    return ProfileOut.model_validate(project)


@router.put("/projects/{project_id}/profile", response_model=ProfileOut)
def set_project_profile(project_id: str, payload: ProfileUpdate, request: Request) -> ProfileOut:
    project = _library(request).set_project_profile(
        project_id, payload.profile, payload.expected_revision
    )
    return ProfileOut.model_validate(project)


@router.get("/assets/{asset_id}/context")
def asset_context(asset_id: str, request: Request) -> dict[str, Any]:
    return _library(request).context_overlay(asset_id)


@router.post("/assets/{asset_id}/labels")
def confirm_context_label(
    asset_id: str, payload: ContextLabelCreate, request: Request
) -> dict[str, Any]:
    return _library(request).confirm_label(
        asset_id,
        label_key=payload.label_key,
        label_vi=payload.label_vi,
        status=payload.status,
        scene_id=payload.scene_id,
        frame_id=payload.frame_id,
    )


@router.post("/assets/{asset_id}:context-review")
def context_review(
    asset_id: str, payload: ContextReviewRequest, request: Request
) -> dict[str, Any]:
    return _library(request).targeted_context_review(asset_id, payload.profile, payload.frame_ids)


@router.get("/assets/{asset_id}/frames/{frame_id}")
def asset_frame(asset_id: str, frame_id: str, request: Request) -> FileResponse:
    library = _library(request)
    asset = library.get_asset(asset_id)
    manifest = (
        library.pipeline.load_manifest(asset_id, asset["sampling_fingerprint"])
        if asset.get("sampling_fingerprint")
        else None
    )
    if manifest is None:
        raise StoreError("SAMPLING_REQUIRED", "sampled frames are not available", status=404)
    frame = next((item for item in manifest["frames"] if item["frame_id"] == frame_id), None)
    if frame is None:
        raise StoreError("FRAME_NOT_FOUND", "frame does not exist", status=404)
    path = Path(frame["path"])
    if not path.is_file():
        raise StoreError("FRAME_NOT_FOUND", "frame image is missing", status=404)
    return FileResponse(path, media_type="image/jpeg", filename=f"{frame_id}.jpg")


@router.post("/projects/{project_id}/reports", response_model=ReportBuildOut)
def build_reports(project_id: str, payload: ReportBuildRequest, request: Request) -> ReportBuildOut:
    return ReportBuildOut.model_validate(
        _library(request).build_project_reports(
            project_id, include_private_notes=payload.include_private_notes
        )
    )


@router.get("/projects/{project_id}/reports", response_model=ReportFilesOut)
def list_reports(project_id: str, request: Request) -> ReportFilesOut:
    return ReportFilesOut.model_validate(_library(request).list_report_files(project_id))


@router.get("/projects/{project_id}/reports/{name}")
def get_report_file(project_id: str, name: str, request: Request) -> Response:
    content, media_type = _library(request).read_report_file(project_id, name)
    return Response(content=content, media_type=media_type)


@router.get("/projects/{project_id}/jobs", response_model=list[JobOut])
def list_jobs(project_id: str, request: Request) -> list[JobOut]:
    return [JobOut.model_validate(job) for job in _library(request).list_jobs(project_id)]


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: str, request: Request) -> JobOut:
    return JobOut.model_validate(_library(request).get_job(job_id))


@router.post("/jobs/{job_id}:cancel", response_model=JobOut)
def cancel_job(job_id: str, request: Request) -> JobOut:
    return JobOut.model_validate(_library(request).cancel_job(job_id))


# --------------------------------------------------------------------------- #
# Annotations                                                                  #
# --------------------------------------------------------------------------- #


@router.get("/projects/{project_id}/annotations", response_model=list[AnnotationOut])
def list_annotations(
    project_id: str, request: Request, asset_id: str | None = None
) -> list[AnnotationOut]:
    rows = _library(request).list_annotations(project_id, asset_id=asset_id)
    return [AnnotationOut.model_validate(row) for row in rows]


@router.post("/projects/{project_id}/annotations", response_model=AnnotationMutation, status_code=201)
def create_annotation(
    project_id: str, payload: AnnotationCreate, request: Request
) -> AnnotationMutation:
    result = _library(request).create_annotation(project_id, payload.model_dump())
    return AnnotationMutation.model_validate(result)


@router.put("/annotations/{annotation_id}", response_model=AnnotationMutation)
def update_annotation(
    annotation_id: str, payload: AnnotationUpdate, request: Request
) -> AnnotationMutation:
    result = _library(request).update_annotation(annotation_id, payload.model_dump())
    return AnnotationMutation.model_validate(result)


@router.delete("/annotations/{annotation_id}", status_code=204)
def delete_annotation(
    annotation_id: str, request: Request, expected_revision: int = Query(ge=1)
) -> Response:
    _library(request).delete_annotation(annotation_id, expected_revision=expected_revision)
    return Response(status_code=204)


# --------------------------------------------------------------------------- #
# Reviews                                                                      #
# --------------------------------------------------------------------------- #


@router.get("/assets/{asset_id}/review", response_model=ReviewOut)
def get_review(asset_id: str, request: Request) -> ReviewOut:
    return ReviewOut.model_validate(_library(request).get_review(asset_id))


@router.put("/assets/{asset_id}/review", response_model=ReviewOut)
def update_review(asset_id: str, payload: ReviewUpdate, request: Request) -> ReviewOut:
    return ReviewOut.model_validate(_library(request).update_review(asset_id, payload.model_dump()))


# --------------------------------------------------------------------------- #
# Cache                                                                        #
# --------------------------------------------------------------------------- #


@router.get("/cache", response_model=CacheStats)
def cache_stats(request: Request) -> CacheStats:
    return CacheStats.model_validate(_library(request).cache_stats())


@router.post("/cache:prune")
def prune_cache(request: Request) -> dict[str, int]:
    return _library(request).prune_cache()


# --------------------------------------------------------------------------- #
# Contracts self-check (unchanged from S00)                                    #
# --------------------------------------------------------------------------- #


def _contract_report(request: Request) -> dict[str, Any]:
    settings = request.app.state.settings
    return run_contract_selfcheck(settings.contracts_dir, settings.fixtures_dir)


@router.get("/contracts", response_model=ContractCheckReport)
def contracts(request: Request) -> ContractCheckReport:
    return ContractCheckReport.model_validate(_contract_report(request))


@router.post("/contracts:recheck", response_model=ContractCheckReport)
def recheck_contracts(request: Request, response: Response) -> ContractCheckReport:
    report = _contract_report(request)
    response.headers["X-PhuThanhAI-Checked"] = "1"
    return ContractCheckReport.model_validate(report)


__all__ = ["router", "StoreError"]
