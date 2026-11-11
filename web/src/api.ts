export interface Health {
  status: "ok" | "degraded";
  product_version: string;
  contract_version: string;
  pid: number;
  uptime_s: number;
  bound_host: string;
  loopback_only: boolean;
  heavy_slot_busy: boolean;
  model_loaded_in_process: false;
  data_root_configured: boolean;
  web_bundle_present: boolean;
  offline_enforced: boolean;
  time: string;
}

export interface SessionInfo {
  capability: string;
  header_name: string;
  product_version: string;
}

export interface ContractCheck {
  status: "pass" | "fail";
  contracts_dir_present: boolean;
  kinds: Array<{
    name: string;
    schema_valid: boolean;
    semantic_valid: boolean;
    fixtures_checked: string[];
    negative_fixtures_rejected: string[];
    error: string | null;
  }>;
  portable_roundtrip_ok: boolean;
  checksum_tamper_rejected: boolean;
}

export interface Project {
  project_id: string;
  name: string;
  created_at: string;
  updated_at: string;
  revision: number;
  asset_count?: number | null;
  analysis_profile?: string;
  profile_revision?: number;
}

export interface Review {
  status: "unreviewed" | "in_review" | "reviewed" | "needs_attention";
  rating: number | null;
  favorite: boolean;
  labels: string[];
  selected_ranges: Array<{ start_ms: number; end_ms: number }>;
  last_playhead_ms: number | null;
  revision: number;
}

export interface Asset {
  asset_id: string;
  project_id: string;
  filename: string;
  duration_ms: number | null;
  width: number | null;
  height: number | null;
  rotation: number;
  fps_nominal: string | null;
  codec: string | null;
  has_audio: boolean;
  vfr_hint: boolean;
  color_transfer: string | null;
  preview_status: string;
  analysis_status: string;
  sampling_status: string;
  sampling_profile: string | null;
  sampled_at: string | null;
  sampling_coverage: string | null;
  analysis_id: string | null;
  analysis_coverage: string | null;
  analysis_updated_at: string | null;
  source_state: string;
  error_code?: string | null;
  size_bytes: number;
  created_at: string;
  updated_at: string;
  review?: Review;
  note_count?: number;
}

export interface AssetPage {
  items: Asset[];
  next_cursor: string | null;
  total: number;
}

export interface Annotation {
  annotation_id: string;
  project_id: string;
  asset_id: string | null;
  scope: "project" | "clip" | "time_range";
  start_ms: number | null;
  end_ms: number | null;
  text: string;
  note_type: string;
  source: string;
  assertion_status: string;
  wall_clock: { time_of_day: string; date: string | null; timezone: string | null; precision: string } | null;
  revision: number;
  created_at: string;
  updated_at: string;
}

export interface AnnotationMutation {
  annotation: Annotation;
  wall_clock_suggestion: { time_of_day: string; precision: string } | null;
}

export interface ScanResult {
  project_id: string;
  root: string;
  scanned_dirs: number;
  candidates: number;
  added: number;
  updated: number;
  unchanged: number;
  skipped_symlinks: number;
  skipped_non_video: number;
  errors: Array<{ path: string; code: string; message: string }>;
  truncated: boolean;
  elapsed_s: number;
}

export interface PreviewInfo {
  asset_id: string;
  mode: "direct" | "proxy";
  source_available: boolean;
  codec: string | null;
  rotation: number;
  duration_ms: number | null;
  stream_url: string;
  thumbnail_url: string;
  proxy_available: boolean;
  proxy_url: string | null;
}

const API = "/api/v1";

let capability: string | null = null;
let capabilityHeader = "X-PhuThanhAI-Capability";

export function setSession(info: SessionInfo | null): void {
  capability = info?.capability ?? null;
  if (info?.header_name) capabilityHeader = info.header_name;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API}${path}`, {
    ...init,
    headers: { Accept: "application/json", ...(init?.headers ?? {}) },
    credentials: "same-origin",
  });
  if (!response.ok) {
    let code = `HTTP_${response.status}`;
    let message = "";
    try {
      const body = (await response.json()) as { code?: string; user_message?: string };
      if (body.code) code = body.code;
      if (body.user_message) message = body.user_message;
    } catch {
      // Non-JSON body; keep the status code.
    }
    throw new ApiError(code, message || code, response.status);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export class ApiError extends Error {
  readonly code: string;
  readonly status: number;
  constructor(code: string, message: string, status: number) {
    super(message);
    this.code = code;
    this.status = status;
  }
}

async function mutate<T>(path: string, method: string, body?: unknown): Promise<T> {
  if (!capability) throw new ApiError("NO_SESSION", "Chưa có capability phiên làm việc", 401);
  const init: RequestInit = {
    method,
    headers: {
      "Content-Type": "application/json",
      [capabilityHeader]: capability,
    },
  };
  if (body !== undefined) init.body = JSON.stringify(body);
  return request<T>(path, init);
}

export function fetchHealth(): Promise<Health> {
  return request<Health>("/health");
}

export function fetchSession(): Promise<SessionInfo> {
  return request<SessionInfo>("/session");
}

export function fetchContracts(): Promise<ContractCheck> {
  return request<ContractCheck>("/contracts");
}

export function recheckContracts(): Promise<ContractCheck> {
  return mutate<ContractCheck>("/contracts:recheck", "POST");
}

export function listProjects(): Promise<Project[]> {
  return request<Project[]>("/projects");
}

export function createProject(name: string, rootPath: string): Promise<Project> {
  return mutate<Project>("/projects", "POST", { name, root_path: rootPath });
}

export function scanProject(projectId: string, directory?: string): Promise<ScanResult> {
  return mutate<ScanResult>(`/projects/${projectId}/assets:scan`, "POST", directory ? { directory } : {});
}

export interface AssetQuery {
  projectId: string;
  q?: string | undefined;
  cursor?: string | null | undefined;
  limit?: number | undefined;
  sort?: string | undefined;
  favorite?: boolean | undefined;
  minRating?: number | undefined;
  hasNote?: boolean | undefined;
  sourceState?: string | undefined;
  label?: string | undefined;
}

export function listAssets(query: AssetQuery): Promise<AssetPage> {
  const params = new URLSearchParams({ project_id: query.projectId });
  if (query.q) params.set("q", query.q);
  if (query.cursor) params.set("cursor", query.cursor);
  if (query.limit) params.set("limit", String(query.limit));
  if (query.sort) params.set("sort", query.sort);
  if (query.favorite !== undefined) params.set("favorite", String(query.favorite));
  if (query.minRating !== undefined) params.set("min_rating", String(query.minRating));
  if (query.hasNote !== undefined) params.set("has_note", String(query.hasNote));
  if (query.sourceState) params.set("source_state", query.sourceState);
  if (query.label) params.set("label", query.label);
  return request<AssetPage>(`/assets?${params.toString()}`);
}

export function getAsset(assetId: string): Promise<Asset> {
  return request<Asset>(`/assets/${assetId}`);
}

export function listAnnotations(projectId: string, assetId?: string): Promise<Annotation[]> {
  const suffix = assetId ? `?asset_id=${encodeURIComponent(assetId)}` : "";
  return request<Annotation[]>(`/projects/${projectId}/annotations${suffix}`);
}

export function createAssetAnnotation(
  projectId: string,
  payload: {
    scope: "clip" | "time_range";
    asset_id: string;
    start_ms?: number | null | undefined;
    end_ms?: number | null | undefined;
    text: string;
    note_type?: string | undefined;
    wall_clock?: { time_of_day: string; precision: "time_of_day" | "date_time" } | null | undefined;
  },
): Promise<AnnotationMutation> {
  return mutate<AnnotationMutation>(`/projects/${projectId}/annotations`, "POST", payload);
}

export function createProjectAnnotation(
  projectId: string,
  text: string,
): Promise<AnnotationMutation> {
  return mutate<AnnotationMutation>(`/projects/${projectId}/annotations`, "POST", {
    scope: "project",
    text,
  });
}

export function updateAnnotation(
  annotationId: string,
  payload: {
    expected_revision: number;
    text: string;
    note_type?: string | undefined;
    start_ms?: number | null | undefined;
    end_ms?: number | null | undefined;
    wall_clock?: { time_of_day: string; precision: "time_of_day" | "date_time" } | null | undefined;
  },
): Promise<AnnotationMutation> {
  return mutate<AnnotationMutation>(`/annotations/${annotationId}`, "PUT", payload);
}

export function deleteAnnotation(annotationId: string, expectedRevision: number): Promise<void> {
  return mutate<void>(`/annotations/${annotationId}?expected_revision=${expectedRevision}`, "DELETE");
}

/** Best-effort save on window close. Uses `keepalive` so the request survives unload. */
export function recordAnnotationUpdateKeepalive(
  annotationId: string,
  payload: { expected_revision: number; text: string },
): void {
  if (!capability) return;
  void fetch(`${API}/annotations/${annotationId}`, {
    method: "PUT",
    keepalive: true,
    headers: { "Content-Type": "application/json", [capabilityHeader]: capability },
    body: JSON.stringify(payload),
  }).catch(() => undefined);
}

export function getReview(assetId: string): Promise<Review> {
  return request<Review>(`/assets/${assetId}/review`);
}

export function putReview(
  assetId: string,
  payload: {
    expected_revision?: number | undefined;
    status?: Review["status"] | undefined;
    rating?: number | null | undefined;
    favorite?: boolean | undefined;
    labels?: string[] | undefined;
    selected_ranges?: Array<{ start_ms: number; end_ms: number }> | undefined;
    last_playhead_ms?: number | null | undefined;
  },
): Promise<Review> {
  return mutate<Review>(`/assets/${assetId}/review`, "PUT", payload);
}

export function previewInfo(assetId: string): Promise<PreviewInfo> {
  return request<PreviewInfo>(`/assets/${assetId}/preview`);
}

export function createProxy(assetId: string): Promise<{ asset_id: string; created: boolean; url: string }> {
  return mutate<{ asset_id: string; created: boolean; url: string }>(`/assets/${assetId}/proxy`, "POST");
}

export function assetStreamUrl(assetId: string): string {
  return `${API}/assets/${assetId}/stream`;
}

export function proxyStreamUrl(assetId: string): string {
  return `${API}/assets/${assetId}/proxy-stream`;
}

export function thumbnailUrl(assetId: string): string {
  return `${API}/assets/${assetId}/thumbnail`;
}

export function cacheStats(): Promise<{ quota_bytes: number; kinds: Record<string, { entries: number; bytes: number }> }> {
  return request("/cache");
}


export type SamplingProfile = "quick" | "detailed" | "thorough";

export interface SamplingJob {
  job_id: string;
  project_id: string;
  asset_id: string | null;
  kind: string;
  state: string;
  progress: number;
  error_code: string | null;
  created_at: string;
  updated_at: string;
  running: boolean;
}

export interface SamplingSummary {
  asset_id: string;
  sampling_status: string;
  sampling_profile: string | null;
  sampled_at: string | null;
  sampling_coverage: string | null;
  analysis_id: string | null;
  analysis_coverage: string | null;
  analysis_updated_at: string | null;
  fingerprint: string | null;
  manifest: {
    frame_count: number;
    scene_count: number;
    observed_max_gap_ms: number | null;
    expected_max_gap_ms: number | null;
    coverage: Record<string, unknown> | null;
    warnings: string[] | null;
  } | null;
}

export function startSampling(assetId: string, profile: SamplingProfile): Promise<SamplingJob> {
  return mutate<SamplingJob>(`/assets/${assetId}:sample`, "POST", { profile });
}

export function getJob(jobId: string): Promise<SamplingJob> {
  return request<SamplingJob>(`/jobs/${jobId}`);
}

export function cancelJob(jobId: string): Promise<SamplingJob> {
  return mutate<SamplingJob>(`/jobs/${jobId}:cancel`, "POST");
}

export function fetchSampling(assetId: string): Promise<SamplingSummary> {
  return request<SamplingSummary>(`/assets/${assetId}/sampling`);
}


export type AnalysisMethod = "auto" | "single_frame" | "sequential_frames" | "multi_frame";
export type SelectionMode = "smart" | "balanced" | "full";

export interface AnalysisSummary {
  asset_id: string;
  analysis_status: string;
  analysis_id: string | null;
  coverage_status: string | null;
  method: string | null;
  frames_total: number;
  frames_observed: number;
  frames_unknown: number;
  sampling_frames?: number;
  selected_frames?: number;
  selection_mode?: SelectionMode;
  visual_coverage?: {
    selected: number;
    sampling: number;
    fraction: number;
    partial: boolean;
    mode?: SelectionMode;
    scenes_missing?: string[];
  };
  warnings: string[];
  evidence: {
    analysis_method: string;
    coverage_status: string;
    prompt_version: string;
    frames: Array<{
      frame_id: string;
      scene_id: string;
      normalized_pts_ms: number;
      interpretation_level: "observed" | "hypothesis" | "unknown";
      observation: {
        description: string;
        objects: string[];
        actions: string[];
        camera: string | null;
        lighting: string | null;
        visible_text: string[];
        uncertainty: string[];
      } | null;
      uncertainty: string[];
    }>;
  } | null;
}

export function startAnalysis(
  assetId: string,
  method: AnalysisMethod,
  selectionMode: SelectionMode = "full",
  userRanges?: Array<[number, number]>,
): Promise<SamplingJob> {
  return mutate<SamplingJob>(`/assets/${assetId}:analyze`, "POST", {
    method,
    selection_mode: selectionMode,
    ...(userRanges && userRanges.length > 0 ? { user_ranges: userRanges } : {}),
  });
}

export function fetchAnalysis(assetId: string): Promise<AnalysisSummary> {
  return request<AnalysisSummary>(`/assets/${assetId}/analysis`);
}


export type ContextLabelStatus = "confirmed" | "corrected" | "dismissed";

export interface ContextCandidate {
  label_key: string;
  label_vi: string;
  category: string;
  status: string;
  interpretation_level: "hypothesis";
  description_vi: string;
  uncertainty_vi: string;
  signals: string[];
  evidence_frames: string[];
  evidence_scenes: string[];
  evidence_times_ms: number[];
}

export interface ContextUserLabel {
  label_key: string;
  label_vi: string;
  status: "user_confirmed" | "user_corrected";
  annotation_id: string;
  revision: number;
  scene_id: string | null;
  frame_id: string | null;
  evidence_frames: string[];
  evidence_times_ms: number[];
  original_candidate: string | null;
}

export interface ContextScene {
  scene_id: string;
  start_ms: number;
  end_ms: number;
  frame_ids: string[];
  candidates: ContextCandidate[];
  user_labels: ContextUserLabel[];
}

export interface ContextOverlay {
  profile: string;
  profile_label_vi: string;
  profile_version: string;
  profile_revision: number | null;
  raw_evidence_sha256: string;
  coverage_status: string | null;
  guardrails: string[];
  candidates: ContextCandidate[];
  unresolved: Array<{
    label_key: string;
    label_vi: string;
    reason_vi: string;
    evidence_frames: string[];
  }>;
  user_labels: ContextUserLabel[];
  dismissed_label_keys: string[];
  scenes: ContextScene[];
}

export function fetchProjectProfile(projectId: string): Promise<{
  project_id: string;
  analysis_profile: string;
  profile_revision: number;
}> {
  return request(`/projects/${projectId}/profile`);
}

export function updateProjectProfile(
  projectId: string,
  profile: string,
  expectedRevision: number
): Promise<{ project_id: string; analysis_profile: string; profile_revision: number }> {
  return mutate(`/projects/${projectId}/profile`, "PUT", {
    profile,
    expected_revision: expectedRevision,
  });
}

export function fetchContext(assetId: string): Promise<ContextOverlay> {
  return request<ContextOverlay>(`/assets/${assetId}/context`);
}

export function confirmContextLabel(
  assetId: string,
  payload: {
    label_key: string;
    label_vi?: string | null;
    status: ContextLabelStatus;
    scene_id?: string | null;
    frame_id?: string | null;
  }
): Promise<ContextOverlay> {
  return mutate<ContextOverlay>(`/assets/${assetId}/labels`, "POST", payload);
}

export function buildReports(projectId: string): Promise<{
  project_id: string;
  files: string[];
  elapsed_s: number;
  analysed_assets: number;
  candidate_labels: number;
  user_labels: number;
}> {
  return mutate(`/projects/${projectId}/reports`, "POST", { include_private_notes: false });
}

export function assetFrameUrl(assetId: string, frameId: string): string {
  return `/api/v1/assets/${assetId}/frames/${frameId}`;
}

export function reportUrl(projectId: string, name: string): string {
  return `/api/v1/projects/${projectId}/reports/${encodeURIComponent(name)}`;
}


export function contextReview(
  assetId: string,
  profile: string,
  frameIds: string[]
): Promise<{
  review_id: string;
  profile: string;
  cached: boolean;
  note_vi: string;
  frames: Array<{
    frame_id: string;
    scene_id: string;
    normalized_pts_ms: number;
    observation: { description: string };
    candidates: ContextCandidate[];
  }>;
}> {
  return mutate(`/assets/${assetId}:context-review`, "POST", {
    profile,
    frame_ids: frameIds,
  });
}
