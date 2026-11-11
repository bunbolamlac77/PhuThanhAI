import { useCallback, useEffect, useRef, useState } from "react";
import type { Annotation, Asset, Health, Project, ScanResult, SessionInfo } from "../api";
import {
  ApiError,
  createAssetAnnotation,
  createProject,
  createProjectAnnotation,
  deleteAnnotation,
  getAsset,
  listAnnotations,
  listAssets,
  listProjects,
  putReview,
  recordAnnotationUpdateKeepalive,
  scanProject,
  updateAnnotation,
} from "../api";
import AssetGrid from "../components/AssetGrid";
import NotesPanel, { type SaveStatus } from "../components/NotesPanel";
import Player, { type PlayerController } from "../components/Player";
import SamplingPanel from "../components/SamplingPanel";
import StarRating from "../components/StarRating";
import { formatBytes } from "../lib/format";

interface Props {
  online: boolean;
  health: Health | null;
  session: SessionInfo | null;
}

export default function LibraryTab({ online, session }: Props) {
  const [projects, setProjects] = useState<Project[]>([]);
  const [project, setProject] = useState<Project | null>(null);
  const [assets, setAssets] = useState<Asset[]>([]);
  const [total, setTotal] = useState(0);
  const [listLoading, setListLoading] = useState(false);
  const [listError, setListError] = useState<string | null>(null);

  const [queryInput, setQueryInput] = useState("");
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState("natural");
  const [favoriteOnly, setFavoriteOnly] = useState(false);
  const [hasNoteOnly, setHasNoteOnly] = useState(false);
  const [minRating, setMinRating] = useState(0);
  const [tagFilter, setTagFilter] = useState("");
  const [labelDraft, setLabelDraft] = useState("");

  const [activeAsset, setActiveAsset] = useState<Asset | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [bulkBusy, setBulkBusy] = useState(false);

  const [projectNotes, setProjectNotes] = useState<Annotation[]>([]);
  const [clipNote, setClipNote] = useState<Annotation | null>(null);
  const [rangeNotes, setRangeNotes] = useState<Annotation[]>([]);
  const [draft, setDraft] = useState("");
  const [saveStatus, setSaveStatus] = useState<SaveStatus>("idle");
  const [saveError, setSaveError] = useState<string | null>(null);
  const [suggestion, setSuggestion] = useState<{ time_of_day: string } | null>(null);
  const [inMs, setInMs] = useState<number | null>(null);
  const [outMs, setOutMs] = useState<number | null>(null);

  const [scanning, setScanning] = useState(false);
  const [scanResult, setScanResult] = useState<ScanResult | null>(null);
  const [formName, setFormName] = useState("");
  const [formPath, setFormPath] = useState("");
  const [showForm, setShowForm] = useState(false);

  const cursorRef = useRef<string | null>(null);
  const loadingRef = useRef(false);
  const controllerRef = useRef<PlayerController | null>(null);
  const draftRef = useRef(draft);
  const clipNoteRef = useRef(clipNote);
  const statusRef = useRef(saveStatus);
  const activeRef = useRef(activeAsset);

  useEffect(() => {
    draftRef.current = draft;
  }, [draft]);
  useEffect(() => {
    clipNoteRef.current = clipNote;
  }, [clipNote]);
  useEffect(() => {
    statusRef.current = saveStatus;
  }, [saveStatus]);
  useEffect(() => {
    activeRef.current = activeAsset;
  }, [activeAsset]);

  // Debounce the search box.
  useEffect(() => {
    const timer = setTimeout(() => setQuery(queryInput.trim()), 300);
    return () => clearTimeout(timer);
  }, [queryInput]);

  const loadProjects = useCallback(async () => {
    if (!session) return;
    try {
      const list = await listProjects();
      setProjects(list);
      setProject((current) => current ?? list[0] ?? null);
      if (list.length === 0) setShowForm(true);
    } catch (cause) {
      setListError(cause instanceof Error ? cause.message : "Không tải được dự án");
    }
  }, [session]);

  useEffect(() => {
    void loadProjects();
  }, [loadProjects]);

  const loadPage = useCallback(
    async (reset: boolean) => {
      if (!project || loadingRef.current) return;
      loadingRef.current = true;
      setListLoading(true);
      setListError(null);
      try {
        const page = await listAssets({
          projectId: project.project_id,
          q: query || undefined,
          sort,
          cursor: reset ? null : cursorRef.current,
          limit: 100,
          favorite: favoriteOnly ? true : undefined,
          hasNote: hasNoteOnly ? true : undefined,
          minRating: minRating > 0 ? minRating : undefined,
          label: tagFilter.trim() || undefined,
        });
        setAssets((previous) => (reset ? page.items : [...previous, ...page.items]));
        cursorRef.current = page.next_cursor;
        setTotal(page.total);
      } catch (cause) {
        setListError(cause instanceof Error ? cause.message : "Không tải được thư viện");
      } finally {
        loadingRef.current = false;
        setListLoading(false);
      }
    },
    [project, query, sort, favoriteOnly, hasNoteOnly, minRating, tagFilter],
  );

  useEffect(() => {
    if (!project) return;
    cursorRef.current = null;
    void loadPage(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [project?.project_id, query, sort, favoriteOnly, hasNoteOnly, minRating, tagFilter]);

  const loadMore = useCallback(() => {
    if (cursorRef.current && !loadingRef.current) void loadPage(false);
  }, [loadPage]);

  // Project-scope notes.
  const reloadProjectNotes = useCallback(async () => {
    if (!project) return;
    try {
      const all = await listAnnotations(project.project_id);
      setProjectNotes(all.filter((note) => note.scope === "project"));
    } catch {
      // Notes list failure must not break playback.
    }
  }, [project]);

  useEffect(() => {
    void reloadProjectNotes();
  }, [reloadProjectNotes]);

  // Asset-scope notes.
  useEffect(() => {
    if (!project || !activeAsset) {
      setClipNote(null);
      setRangeNotes([]);
      setDraft("");
      return;
    }
    let cancelled = false;
    listAnnotations(project.project_id, activeAsset.asset_id)
      .then((notes) => {
        if (cancelled) return;
        const clip = notes.find((note) => note.scope === "clip" && note.asset_id === activeAsset.asset_id) ?? null;
        setClipNote(clip);
        setRangeNotes(
          notes
            .filter((note) => note.scope === "time_range" && note.asset_id === activeAsset.asset_id)
            .sort((a, b) => (a.start_ms ?? 0) - (b.start_ms ?? 0)),
        );
        setDraft(clip?.text ?? "");
        setSuggestion(null);
        setSaveStatus("idle");
        setSaveError(null);
      })
      .catch(() => setListError("Không tải được ghi chú"));
    return () => {
      cancelled = true;
    };
    // Keyed by ids on purpose: a review/note_count update changes the asset object
    // identity and must not reload notes or reset the "Đã lưu" status.
  }, [project?.project_id, activeAsset?.asset_id]);

  const patchAsset = useCallback((assetId: string, updater: (asset: Asset) => Asset) => {
    setAssets((previous) => previous.map((asset) => (asset.asset_id === assetId ? updater(asset) : asset)));
    setActiveAsset((previous) => (previous && previous.asset_id === assetId ? updater(previous) : previous));
  }, []);

  const persist = useCallback(
    async (text: string, wallClock?: { time_of_day: string; precision: "time_of_day" }) => {
      const asset = activeRef.current;
      if (!asset || !project) return;
      const trimmed = text.trim();
      if (!trimmed) {
        setSaveStatus("idle");
        return;
      }
      setSaveStatus("saving");
      setSaveError(null);
      try {
        const existing = clipNoteRef.current;
        const persistedWall = existing?.wall_clock
          ? {
              time_of_day: existing.wall_clock.time_of_day,
              precision:
                existing.wall_clock.precision === "date_time"
                  ? ("date_time" as const)
                  : ("time_of_day" as const),
            }
          : null;
        let result;
        if (existing) {
          result = await updateAnnotation(existing.annotation_id, {
            expected_revision: existing.revision,
            text: trimmed,
            wall_clock: wallClock ?? persistedWall,
          });
        } else {
          result = await createAssetAnnotation(project.project_id, {
            scope: "clip",
            asset_id: asset.asset_id,
            text: trimmed,
            ...(wallClock ? { wall_clock: wallClock } : {}),
          });
        }
        setClipNote(result.annotation);
        clipNoteRef.current = result.annotation;
        setSuggestion(result.wall_clock_suggestion);
        setSaveStatus("saved");
        patchAsset(asset.asset_id, (current) => ({
          ...current,
          note_count: existing ? current.note_count ?? 1 : (current.note_count ?? 0) + 1,
        }));
      } catch (cause) {
        const apiError = cause instanceof ApiError ? cause : null;
        setSaveError(apiError ? apiError.message : "Không lưu được ghi chú");
        setSaveStatus(apiError?.code === "ANNOTATION_CONFLICT" ? "conflict" : "error");
      }
    },
    [project, patchAsset],
  );

  const flushNow = useCallback(async () => {
    const current = draftRef.current;
    const existing = clipNoteRef.current;
    if (statusRef.current !== "dirty") return;
    if (current.trim() === (existing?.text ?? "")) {
      setSaveStatus(existing ? "saved" : "idle");
      return;
    }
    await persist(current);
  }, [persist]);

  // Autosave 450 ms after typing stops.
  useEffect(() => {
    if (!activeAsset) return;
    const baseline = clipNote?.text ?? "";
    if (draft === baseline) {
      if (saveStatus === "dirty") setSaveStatus(baseline ? "saved" : "idle");
      return;
    }
    if (!draft.trim()) {
      setSaveStatus("dirty");
      return;
    }
    setSaveStatus("dirty");
    const timer = setTimeout(() => void persist(draft), 450);
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draft, activeAsset?.asset_id, clipNote?.text]);

  // Best-effort flush when the window closes; the server still has the last commit.
  useEffect(() => {
    const handler = () => {
      if (statusRef.current === "dirty" && activeRef.current && clipNoteRef.current) {
        recordAnnotationUpdateKeepalive(clipNoteRef.current.annotation_id, {
          expected_revision: clipNoteRef.current.revision,
          text: draftRef.current,
        });
      }
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, []);

  const resetNoteState = useCallback(() => {
    setClipNote(null);
    clipNoteRef.current = null;
    setDraft("");
    draftRef.current = "";
    setSaveStatus("idle");
    setSaveError(null);
    setRangeNotes([]);
    setSuggestion(null);
  }, []);

  const selectAsset = useCallback(
    async (asset: Asset) => {
      if (asset.asset_id === activeRef.current?.asset_id) return;
      // Persist the outgoing clip first, then clear the editor synchronously.
      // Without this clear, the previous draft would briefly belong to the new
      // asset and autosave could write clip A's note onto clip B.
      await flushNow();
      resetNoteState();
      setActiveAsset(asset);
      setInMs(null);
      setOutMs(null);
      setLabelDraft("");
    },
    [flushNow, resetNoteState],
  );

  const moveSelection = useCallback(
    (delta: number) => {
      if (!activeAsset) return;
      const index = assets.findIndex((asset) => asset.asset_id === activeAsset.asset_id);
      const next = assets[index + delta];
      if (next) void selectAsset(next);
    },
    [activeAsset, assets, selectAsset],
  );

  const applyReview = useCallback(
    async (patch: {
      rating?: number | null;
      favorite?: boolean;
      status?: "reviewed" | "in_review" | "needs_attention";
      labels?: string[];
    }) => {
      const asset = activeRef.current;
      if (!asset) return;
      try {
        const review = await putReview(asset.asset_id, {
          expected_revision: asset.review?.revision,
          favorite: patch.favorite ?? asset.review?.favorite ?? false,
          rating: patch.rating !== undefined ? patch.rating : asset.review?.rating ?? null,
          status: patch.status ?? asset.review?.status ?? "reviewed",
          labels: patch.labels ?? asset.review?.labels ?? [],
          selected_ranges: asset.review?.selected_ranges ?? [],
          last_playhead_ms: asset.review?.last_playhead_ms ?? null,
        });
        patchAsset(asset.asset_id, (current) => ({ ...current, review }));
      } catch (cause) {
        setSaveError(cause instanceof Error ? cause.message : "Không lưu được đánh giá");
      }
    },
    [patchAsset],
  );

  const bulkFavorite = useCallback(async () => {
    if (selected.size === 0 || bulkBusy) return;
    setBulkBusy(true);
    try {
      for (const assetId of selected) {
        const asset = assets.find((item) => item.asset_id === assetId);
        await putReview(assetId, {
          expected_revision: asset?.review?.revision,
          favorite: true,
          rating: asset?.review?.rating ?? null,
          status: asset?.review?.status ?? "reviewed",
          labels: asset?.review?.labels ?? [],
          selected_ranges: asset?.review?.selected_ranges ?? [],
          last_playhead_ms: asset?.review?.last_playhead_ms ?? null,
        });
      }
      cursorRef.current = null;
      await loadPage(true);
    } catch (cause) {
      setListError(cause instanceof Error ? cause.message : "Không cập nhật được lựa chọn");
    } finally {
      setBulkBusy(false);
    }
  }, [assets, bulkBusy, loadPage, selected]);

  const runScan = useCallback(async () => {
    if (!project) return;
    setScanning(true);
    setScanResult(null);
    try {
      const result = await scanProject(project.project_id);
      setScanResult(result);
      cursorRef.current = null;
      await loadPage(true);
      await loadProjects();
    } catch (cause) {
      setListError(cause instanceof Error ? cause.message : "Quét thất bại");
    } finally {
      setScanning(false);
    }
  }, [project, loadPage, loadProjects]);

  const submitProject = useCallback(async () => {
    if (!formName.trim() || !formPath.trim()) return;
    try {
      const created = await createProject(formName.trim(), formPath.trim());
      setProject(created);
      setShowForm(false);
      await loadProjects();
    } catch (cause) {
      setListError(cause instanceof Error ? cause.message : "Không tạo được dự án");
    }
  }, [formName, formPath, loadProjects]);

  const createRangeNote = useCallback(
    async (text: string) => {
      if (!project || !activeAsset || inMs === null || outMs === null) return;
      try {
        const result = await createAssetAnnotation(project.project_id, {
          scope: "time_range",
          asset_id: activeAsset.asset_id,
          start_ms: inMs,
          end_ms: outMs,
          text,
        });
        setRangeNotes((previous) =>
          [...previous, result.annotation].sort((a, b) => (a.start_ms ?? 0) - (b.start_ms ?? 0)),
        );
        patchAsset(activeAsset.asset_id, (current) => ({
          ...current,
          note_count: (current.note_count ?? 0) + 1,
        }));
      } catch (cause) {
        setSaveError(cause instanceof Error ? cause.message : "Không lưu được ghi chú đoạn");
      }
    },
    [project, activeAsset, inMs, outMs, patchAsset],
  );

  const createProjectNote = useCallback(
    async (text: string) => {
      if (!project) return;
      try {
        const result = await createProjectAnnotation(project.project_id, text);
        setProjectNotes((previous) => [result.annotation, ...previous]);
      } catch (cause) {
        setSaveError(cause instanceof Error ? cause.message : "Không lưu được ghi chú dự án");
      }
    },
    [project],
  );

  const removeNote = useCallback(
    async (note: Annotation) => {
      if (!window.confirm("Xoá ghi chú này?")) return;
      try {
        await deleteAnnotation(note.annotation_id, note.revision);
        setRangeNotes((previous) => previous.filter((item) => item.annotation_id !== note.annotation_id));
        if (activeAsset) {
          patchAsset(activeAsset.asset_id, (current) => ({
            ...current,
            note_count: Math.max(0, (current.note_count ?? 1) - 1),
          }));
        }
      } catch (cause) {
        setSaveError(cause instanceof Error ? cause.message : "Không xoá được ghi chú");
      }
    },
    [activeAsset, patchAsset],
  );

  const useSuggestion = useCallback(async () => {
    if (!suggestion) return;
    await persist(draftRef.current, { time_of_day: suggestion.time_of_day, precision: "time_of_day" });
    setSuggestion(null);
  }, [persist, suggestion]);

  const reloadConflict = useCallback(async () => {
    if (!project || !activeAsset) return;
    const notes = await listAnnotations(project.project_id, activeAsset.asset_id);
    const clip = notes.find((note) => note.scope === "clip" && note.asset_id === activeAsset.asset_id) ?? null;
    setClipNote(clip);
    clipNoteRef.current = clip;
    setDraft(clip?.text ?? "");
    setSaveStatus("idle");
    setSaveError(null);
  }, [project, activeAsset]);

  // Keyboard shortcuts, disabled while typing and during IME composition.
  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if (event.isComposing || event.keyCode === 229) return;
      const target = event.target as HTMLElement | null;
      const tag = target?.tagName?.toLowerCase();
      if (tag === "input" || tag === "textarea" || tag === "select" || target?.isContentEditable) return;
      const controller = controllerRef.current;
      if (!controller) return;

      if (event.key === " ") {
        event.preventDefault();
        controller.toggle();
      } else if (event.key === "j") {
        controller.seekBy(-10);
      } else if (event.key === "k") {
        controller.seekBy(0);
        if (controller.isPlaying()) controller.toggle();
      } else if (event.key === "l") {
        controller.seekBy(10);
      } else if (event.key === "ArrowLeft") {
        controller.seekBy(-5);
      } else if (event.key === "ArrowRight") {
        controller.seekBy(5);
      } else if (event.key === "i") {
        setInMs(controller.currentMs());
      } else if (event.key === "o") {
        setOutMs(controller.currentMs());
      } else if (event.key === "n") {
        event.preventDefault();
        document.getElementById("note-input")?.focus();
      } else if (event.key === "[") {
        moveSelection(-1);
      } else if (event.key === "]") {
        moveSelection(1);
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [moveSelection]);

  return (
    <div className="library-grid">
      <section className="panel" aria-label="Thư viện media">
        <div className="panel__head">
          <h2>1 · Thư viện</h2>
          <span className="badge">{total} clip</span>
        </div>

        <select
          className="project-select"
          value={project?.project_id ?? ""}
          aria-label="Dự án"
          onChange={(event) => {
            const next = projects.find((item) => item.project_id === event.target.value) ?? null;
            setProject(next);
            setActiveAsset(null);
            resetNoteState();
            setSelected(new Set());
            cursorRef.current = null;
          }}
        >
          {projects.map((item) => (
            <option key={item.project_id} value={item.project_id}>
              {item.name}
            </option>
          ))}
        </select>

        <input
          className="search"
          type="search"
          placeholder="Tìm tên file hoặc nội dung ghi chú…"
          value={queryInput}
          aria-label="Tìm video"
          onChange={(event) => setQueryInput(event.target.value)}
        />

        <div className="toolbar toolbar--muted">
          <select value={sort} aria-label="Sắp xếp" onChange={(event) => setSort(event.target.value)}>
            <option value="natural">Tên tự nhiên</option>
            <option value="duration">Thời lượng</option>
            <option value="imported">Mới nhập</option>
          </select>
          <select
            value={minRating}
            aria-label="Lọc theo số sao"
            onChange={(event) => setMinRating(Number(event.target.value))}
          >
            <option value={0}>Mọi đánh giá</option>
            {[1, 2, 3, 4, 5].map((value) => (
              <option key={value} value={value}>
                ≥ {value} sao
              </option>
            ))}
          </select>
          <label className="checkbox">
            <input type="checkbox" checked={favoriteOnly} onChange={(event) => setFavoriteOnly(event.target.checked)} />
            Yêu thích
          </label>
          <label className="checkbox">
            <input type="checkbox" checked={hasNoteOnly} onChange={(event) => setHasNoteOnly(event.target.checked)} />
            Có ghi chú
          </label>
          <input
            className="search search--inline"
            type="search"
            placeholder="Tag…"
            value={tagFilter}
            aria-label="Lọc theo tag"
            onChange={(event) => setTagFilter(event.target.value)}
          />
        </div>

        <div className="toolbar">
          <button type="button" disabled={!online || scanning || !project} onClick={() => void runScan()}>
            {scanning ? "Đang quét…" : "Quét thư mục"}
          </button>
          <button type="button" disabled={!online} onClick={() => setShowForm((value) => !value)}>
            + Dự án
          </button>
          <button type="button" disabled={selected.size === 0 || bulkBusy} onClick={() => void bulkFavorite()}>
            ★ Yêu thích ({selected.size})
          </button>
          <button
            type="button"
            disabled={selected.size === 0}
            onClick={() => setSelected(new Set())}
          >
            Bỏ chọn
          </button>
        </div>

        {showForm ? (
          <div className="project-form">
            <input
              type="text"
              placeholder="Tên dự án"
              value={formName}
              onChange={(event) => setFormName(event.target.value)}
            />
            <input
              type="text"
              placeholder="/Users/…/footage"
              value={formPath}
              onChange={(event) => setFormPath(event.target.value)}
            />
            <button type="button" disabled={!formName.trim() || !formPath.trim()} onClick={() => void submitProject()}>
              Tạo dự án
            </button>
          </div>
        ) : null}

        {scanResult ? (
          <p className="hint">
            Quét xong: +{scanResult.added} mới · {scanResult.unchanged} không đổi · {scanResult.errors.length} lỗi ·{" "}
            {scanResult.elapsed_s}s
          </p>
        ) : null}
        {listError ? <p className="result result--bad">{listError}</p> : null}

        <AssetGrid
          assets={assets}
          activeId={activeAsset?.asset_id ?? null}
          selected={selected}
          loading={listLoading}
          hasMore={cursorRef.current !== null}
          onActivate={(asset) => void selectAsset(asset)}
          onToggleSelect={(asset) =>
            setSelected((previous) => {
              const next = new Set(previous);
              if (next.has(asset.asset_id)) next.delete(asset.asset_id);
              else next.add(asset.asset_id);
              return next;
            })
          }
          onLoadMore={loadMore}
        />
      </section>

      <section className="panel panel--center" aria-label="Video player">
        <div className="panel__head">
          <h2>2 · Video player</h2>
          {activeAsset ? (
            <span className="badge">
              {activeAsset.codec ?? "—"} · {formatBytes(activeAsset.size_bytes)}
            </span>
          ) : null}
        </div>

        {activeAsset ? (
          <>
            <Player
              key={activeAsset.asset_id}
              asset={activeAsset}
              onTime={() => undefined}
              onController={(controller) => {
                controllerRef.current = controller;
              }}
            />
            <div className="review-row">
              <StarRating value={activeAsset.review?.rating ?? null} onChange={(rating) => void applyReview({ rating })} />
              <button
                type="button"
                className={activeAsset.review?.favorite ? "chip chip--active" : "chip"}
                onClick={() => void applyReview({ favorite: !(activeAsset.review?.favorite ?? false) })}
              >
                ★ Yêu thích
              </button>
              <div className="tag-editor">
                {(activeAsset.review?.labels ?? []).map((label) => (
                  <span className="tag-chip" key={label}>
                    {label}
                    <button
                      type="button"
                      aria-label={`Bỏ tag ${label}`}
                      onClick={() =>
                        void applyReview({
                          labels: (activeAsset.review?.labels ?? []).filter((item) => item !== label),
                        })
                      }
                    >
                      ×
                    </button>
                  </span>
                ))}
                <input
                  type="text"
                  value={labelDraft}
                  placeholder="thêm tag"
                  aria-label="Thêm tag cho clip"
                  onChange={(event) => setLabelDraft(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" && labelDraft.trim() && activeAsset) {
                      const labels = activeAsset.review?.labels ?? [];
                      if (!labels.includes(labelDraft.trim())) {
                        void applyReview({ labels: [...labels, labelDraft.trim()] });
                      }
                      setLabelDraft("");
                    }
                  }}
                />
              </div>
            </div>
            <SamplingPanel
              asset={activeAsset}
              online={online}
              onCompleted={(assetId) => {
                void (async () => {
                  try {
                    const fresh = await getAsset(assetId);
                    patchAsset(assetId, () => fresh);
                  } catch {
                    // A refresh failure must not hide the completed state already shown.
                  }
                })();
              }}
            />
            <p className="hint">
              Space play/pause · J/K/L · ←/→ 5s · I/O đặt In/Out · N ghi chú · [ ] clip trước/sau
            </p>
          </>
        ) : (
          <div className="empty-state">
            <p className="empty-state__title">Chưa chọn clip</p>
            <p>Chọn một clip ở thư viện để xem, tua và ghi chú. Không cần chạy AI.</p>
          </div>
        )}
      </section>

      <NotesPanel
        asset={activeAsset}
        projectNotes={projectNotes}
        clipNote={clipNote}
        rangeNotes={rangeNotes}
        draft={draft}
        status={saveStatus}
        errorMessage={saveError}
        suggestion={suggestion}
        inMs={inMs}
        outMs={outMs}
        onChangeDraft={setDraft}
        onFlush={() => void flushNow()}
        onReload={() => void reloadConflict()}
        onUseSuggestion={() => void useSuggestion()}
        onCreateRange={(text) => void createRangeNote(text)}
        onCreateProjectNote={(text) => void createProjectNote(text)}
        onDeleteNote={(note) => void removeNote(note)}
        onSeek={(ms) => controllerRef.current?.seekTo(ms)}
        onSetIn={() => setInMs(controllerRef.current?.currentMs() ?? 0)}
        onSetOut={() => setOutMs(controllerRef.current?.currentMs() ?? 0)}
      />
    </div>
  );
}
