"""Pydantic request/response models for the S01 library API.

These are transport objects, not the portable V2/V3 contracts. The portable
contracts remain those in ``contracts/*.schema.json`` and are validated by
:mod:`phuthanhai.contracts.validators`.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Scope = Literal["project", "clip", "time_range"]
NoteType = Literal["event", "context", "location", "clock_time", "person", "other"]
ReviewStatus = Literal["unreviewed", "in_review", "reviewed", "needs_attention"]
SamplingProfile = Literal["quick", "detailed", "thorough"]


class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    root_path: str = Field(min_length=1)


class ProjectOut(BaseModel):
    # ``extra="ignore"`` drops the local ``root_path`` from API responses; the
    # filesystem layout stays server-side.
    model_config = ConfigDict(extra="ignore")

    project_id: str
    name: str
    created_at: str
    updated_at: str
    revision: int
    asset_count: int | None = None
    created: bool | None = None
    analysis_profile: str = "general"
    profile_revision: int = 1


class ScanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    directory: str | None = None
    max_files: int = Field(default=50_000, ge=1, le=200_000)


class ScanResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    root: str
    scanned_dirs: int
    candidates: int
    added: int
    updated: int
    unchanged: int
    skipped_symlinks: int
    skipped_non_video: int
    errors: list[dict[str, str]]
    truncated: bool
    elapsed_s: float


class WallClock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    time_of_day: str = Field(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
    date: str | None = None
    timezone: str | None = None
    precision: Literal["time_of_day", "date_time"] = "time_of_day"


class AnnotationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: Scope
    asset_id: str | None = None
    start_ms: int | None = Field(default=None, ge=0)
    end_ms: int | None = Field(default=None, ge=1)
    text: str = Field(min_length=1, max_length=4000)
    note_type: NoteType | None = None
    wall_clock: WallClock | None = None


class AnnotationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    text: str = Field(min_length=1, max_length=4000)
    note_type: NoteType | None = None
    start_ms: int | None = Field(default=None, ge=0)
    end_ms: int | None = Field(default=None, ge=1)
    wall_clock: WallClock | None = None


class AnnotationOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    annotation_id: str
    project_id: str
    asset_id: str | None
    scope: Scope
    start_ms: int | None
    end_ms: int | None
    text: str
    note_type: NoteType
    source: str
    assertion_status: str
    wall_clock: dict[str, Any] | None
    revision: int
    created_at: str
    updated_at: str


class AnnotationMutation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    annotation: AnnotationOut
    wall_clock_suggestion: dict[str, Any] | None = None


class Span(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=1)


class ReviewUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int | None = Field(default=None, ge=1)
    status: ReviewStatus = "reviewed"
    rating: int | None = Field(default=None, ge=1, le=5)
    favorite: bool = False
    labels: list[str] = Field(default_factory=list, max_length=50)
    selected_ranges: list[Span] = Field(default_factory=list, max_length=50)
    last_playhead_ms: int | None = Field(default=None, ge=0)


class ReviewOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: str
    status: ReviewStatus
    rating: int | None
    favorite: bool
    labels: list[str]
    selected_ranges: list[dict[str, int]]
    last_playhead_ms: int | None
    revision: int


class AssetOut(BaseModel):
    # Absolute filesystem paths stay server-side; the UI addresses assets by id.
    model_config = ConfigDict(extra="ignore")

    asset_id: str
    project_id: str
    filename: str
    duration_ms: int | None
    width: int | None
    height: int | None
    rotation: int
    fps_nominal: str | None
    codec: str | None
    has_audio: bool
    vfr_hint: bool
    color_transfer: str | None
    preview_status: str
    analysis_status: str
    sampling_status: str = "not_sampled"
    sampling_profile: str | None = None
    sampled_at: str | None = None
    sampling_coverage: str | None = None
    analysis_id: str | None = None
    analysis_coverage: str | None = None
    analysis_updated_at: str | None = None
    source_state: str
    error_code: str | None = None
    size_bytes: int
    created_at: str
    updated_at: str
    review: dict[str, Any] | None = None
    note_count: int | None = None


class AssetListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[AssetOut]
    next_cursor: str | None
    total: int


class PreviewInfo(BaseModel):
    model_config = ConfigDict(extra="allow")

    asset_id: str
    mode: Literal["direct", "proxy"]
    source_available: bool
    codec: str | None
    rotation: int
    duration_ms: int | None
    stream_url: str
    thumbnail_url: str
    proxy_available: bool
    proxy_url: str | None


class ProxyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: str
    created: bool
    duration_ms: int | None
    mapping_delta_ms: int | None
    url: str
    mode: Literal["proxy"]


class CacheStats(BaseModel):
    model_config = ConfigDict(extra="allow")

    quota_bytes: int
    kinds: dict[str, dict[str, int]]


class SampleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: SamplingProfile = "detailed"


class JobOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    job_id: str
    project_id: str
    asset_id: str | None
    kind: str
    state: str
    progress: float
    error_code: str | None = None
    created_at: str
    updated_at: str
    running: bool = False


class SamplingSummary(BaseModel):
    model_config = ConfigDict(extra="ignore")

    asset_id: str
    sampling_status: str
    sampling_profile: str | None
    sampled_at: str | None
    coverage_status: str | None
    fingerprint: str | None
    manifest: dict[str, Any] | None = None


class AnalysisRunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_ids: list[str] | None = None
    selection_mode: Literal["smart", "balanced", "full"] = "smart"
    sampling_profile: Literal["quick", "detailed", "thorough"] = "detailed"
    skip_existing: bool = True
    idempotency_key: str | None = None


class AnalysisRunItemOut(BaseModel):
    item_id: str
    run_id: str
    asset_id: str
    position: int
    status: str
    sampling_status: str | None = None
    vision_status: str | None = None
    cache_reused: int = 0
    frames_selected: int = 0
    frames_committed: int = 0
    error_code: str | None = None
    retry_count: int = 0
    updated_at: str


class AnalysisRunOut(BaseModel):
    run_id: str
    project_id: str
    status: str
    config: dict[str, Any]
    total_items: int
    counts: dict[str, int]
    completed: int
    skipped: int
    failed: int
    running: int
    waiting: int
    created_at: str
    updated_at: str


class AnalyzeAutoRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selection_mode: Literal["smart", "balanced", "full"] = "smart"
    sampling_profile: Literal["quick", "detailed", "thorough"] = "detailed"
    skip_existing: bool = True
    method: Literal["auto", "single_frame", "sequential_frames", "multi_frame"] = "auto"
    user_ranges: list[list[int]] | None = None


class AnalyzeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: Literal["auto", "single_frame", "sequential_frames", "multi_frame"] = "auto"
    selection_mode: Literal["smart", "balanced", "full"] = "full"
    user_ranges: list[list[int]] | None = None


class AnalysisSummary(BaseModel):
    model_config = ConfigDict(extra="ignore")

    asset_id: str
    analysis_status: str
    analysis_id: str | None
    coverage_status: str | None
    method: str | None
    frames_total: int
    frames_observed: int
    frames_unknown: int
    sampling_frames: int = 0
    selected_frames: int = 0
    selection_mode: str = "full"
    execution_status: str = "complete"
    visual_coverage_status: str = "complete"
    visual_coverage: dict[str, Any] = {}
    warnings: list[str]
    evidence: dict[str, Any] | None = None


class ProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: Literal[
        "general",
        "wedding_traditional",
        "event",
        "vlog_travel",
        "documentary",
        "pov_footage",
        "custom",
    ]
    expected_revision: int


class ProfileOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    project_id: str
    analysis_profile: str
    profile_revision: int


class ContextLabelCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label_key: str = Field(min_length=1, max_length=80)
    label_vi: str | None = Field(default=None, max_length=200)
    status: Literal["confirmed", "corrected", "dismissed"]
    scene_id: str | None = None
    frame_id: str | None = None


class ReportBuildOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    project_id: str
    profile: str | None
    analysed_assets: int
    candidate_labels: int
    user_labels: int
    files: list[str]
    dir: str
    elapsed_s: float
    validated: bool


class ReportFilesOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    project_id: str
    files: list[str]


class ReportBuildRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    include_private_notes: bool = False


class ContextReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: Literal[
        "general",
        "wedding_traditional",
        "event",
        "vlog_travel",
        "documentary",
        "pov_footage",
        "custom",
    ]
    frame_ids: list[str]
