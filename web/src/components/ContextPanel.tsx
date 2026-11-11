import { useCallback, useEffect, useRef, useState } from "react";
import type { Asset, ContextOverlay } from "../api";
import {
  assetFrameUrl,
  buildReports,
  confirmContextLabel,
  contextReview,
  fetchContext,
} from "../api";
import SceneContextCard from "./SceneContextCard";

interface Props {
  asset: Asset;
  projectId: string;
  online: boolean;
}

export default function ContextPanel({ asset, projectId, online }: Props) {
  const [overlay, setOverlay] = useState<ContextOverlay | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [frame, setFrame] = useState<string | null>(null);
  const [review, setReview] = useState<Awaited<ReturnType<typeof contextReview>> | null>(null);
  const [report, setReport] = useState<{ files: string[]; elapsed_s: number } | null>(null);
  const assetRef = useRef(asset.asset_id);

  const load = useCallback(async (assetId: string) => {
    try {
      const fresh = await fetchContext(assetId);
      if (assetRef.current !== assetId) return;
      setOverlay(fresh);
      setError(null);
    } catch (cause) {
      if (assetRef.current !== assetId) return;
      setOverlay(null);
      setError(cause instanceof Error ? cause.message : "Chưa có ngữ cảnh");
    }
  }, []);

  useEffect(() => {
    assetRef.current = asset.asset_id;
    setOverlay(null);
    setReview(null);
    setReport(null);
    setFrame(null);
    setError(null);
    if (online && (asset.analysis_status === "complete" || asset.analysis_status === "partial")) {
      void load(asset.asset_id);
    }
  }, [asset.asset_id, asset.analysis_status, online, load]);

  async function apply(status: "confirmed" | "corrected" | "dismissed", labelKey: string, labelVi: string, sceneId: string) {
    if (!overlay || busy) return;
    let text = labelVi;
    if (status === "corrected") {
      const entered = window.prompt("Nhãn tiếng Việt do bạn xác nhận:", labelVi);
      if (!entered || !entered.trim()) return;
      text = entered.trim();
    }
    setBusy(true);
    setError(null);
    try {
      const fresh = await confirmContextLabel(asset.asset_id, {
        label_key: labelKey,
        ...(status === "dismissed" ? {} : { label_vi: text }),
        status,
        scene_id: sceneId,
        frame_id: null,
      });
      if (assetRef.current === asset.asset_id) setOverlay(fresh);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Không lưu được nhãn");
    } finally {
      setBusy(false);
    }
  }

  async function deepReview() {
    if (!overlay || busy) return;
    const ids = Array.from(
      new Set(
        overlay.candidates.flatMap((candidate) => candidate.evidence_frames).slice(0, 3).concat(
          overlay.scenes.flatMap((scene) => scene.frame_ids).slice(0, 1)
        )
      )
    ).slice(0, 3);
    if (ids.length === 0) return;
    setBusy(true);
    setError(null);
    try {
      setReview(await contextReview(asset.asset_id, overlay.profile, ids));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Xác minh chuyên sâu thất bại");
    } finally {
      setBusy(false);
    }
  }

  async function makeReport() {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const built = await buildReports(projectId);
      setReport({ files: built.files, elapsed_s: built.elapsed_s });
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Dựng báo cáo thất bại");
    } finally {
      setBusy(false);
    }
  }

  if (asset.analysis_status !== "complete" && asset.analysis_status !== "partial") {
    return <p className="hint">Chạy phân tích thị giác trước để có ngữ cảnh.</p>;
  }

  return (
    <div className="context-panel">
      <div className="context-panel__head">
        <strong>Ngữ cảnh &amp; xác nhận</strong>
        {overlay ? <span className="badge">{overlay.profile_label_vi}</span> : null}
        {overlay ? <span className="badge">{overlay.profile}</span> : null}
        {overlay ? <span className="hint">raw {overlay.raw_evidence_sha256.slice(0, 10)}</span> : null}
      </div>

      {overlay ? (
        <>
          {overlay.guardrails.length > 0 ? (
            <ul className="context-guardrails">
              {overlay.guardrails.map((rule) => (
                <li key={rule}>{rule}</li>
              ))}
            </ul>
          ) : null}
          <div className="context-actions">
            <button type="button" disabled={!online || busy} onClick={() => void deepReview()}>
              Xác minh chuyên sâu
            </button>
            <button type="button" disabled={!online || busy} onClick={() => void makeReport()}>
              Dựng báo cáo
            </button>
          </div>

          <h4>AI gợi ý theo ngữ cảnh</h4>
          {overlay.scenes.length === 0 ? <p className="hint">Không có cảnh nào có frame.</p> : null}
          {overlay.scenes.map((scene) => (
            <SceneContextCard
              key={scene.scene_id}
              scene={scene}
              onAction={(action, key, label, sceneId) =>
                void apply(action === "confirm" ? "confirmed" : action === "edit" ? "corrected" : "dismissed", key, label, sceneId)
              }
              onViewFrame={(frameId) => setFrame(frameId)}
            />
          ))}

          {overlay.unresolved.length > 0 ? (
            <>
              <h4>Chưa đủ bằng chứng</h4>
              {overlay.unresolved.map((item) => (
                <div key={item.label_key} className="context-unresolved">
                  <strong>{item.label_vi}</strong>
                  <p className="hint">{item.reason_vi}</p>
                  <div className="context-frames">
                    {item.evidence_frames.map((frameId) => (
                      <button key={frameId} type="button" className="link" onClick={() => setFrame(frameId)}>
                        {frameId}
                      </button>
                    ))}
                  </div>
                </div>
              ))}
            </>
          ) : null}

          <h4>Người dùng xác nhận / chỉnh sửa</h4>
          {overlay.user_labels.length === 0 ? <p className="hint">Chưa có nhãn người dùng.</p> : null}
          {overlay.user_labels.map((label) => (
            <div key={`${label.label_key}-${label.annotation_id}`} className="context-label">
              <strong>{label.label_vi}</strong>
              <span className="badge badge--ok">đã xác nhận</span>
              {label.original_candidate ? <span className="hint">sửa từ: {label.original_candidate}</span> : null}
              <span className="hint">annotation {label.annotation_id.slice(0, 8)}</span>
              <button
                type="button"
                className="link"
                onClick={() => void apply("dismissed", label.label_key, label.label_vi, label.scene_id ?? "")}
              >
                Bỏ nhãn
              </button>
            </div>
          ))}
        </>
      ) : (
        <p className="hint">{error ?? "Đang tải ngữ cảnh…"}</p>
      )}

      {review ? (
        <div className="context-review">
          <h4>Xác minh chuyên sâu ({review.cached ? "cache" : "vừa chạy"})</h4>
          <p className="hint">{review.note_vi}</p>
          {review.frames.map((item) => (
            <div key={item.frame_id} className="context-review__frame">
              <strong>{item.frame_id}</strong>
              <span className="hint">@ {item.normalized_pts_ms} ms</span>
              <p>{item.observation.description}</p>
              <div className="context-frames">
                {item.candidates.map((candidate) => (
                  <span key={candidate.label_key} className="badge">
                    {candidate.label_vi}
                  </span>
                ))}
              </div>
            </div>
          ))}
        </div>
      ) : null}

      {report ? (
        <div className="context-report">
          <strong>Đã dựng báo cáo ({report.elapsed_s}s)</strong>
          <ul className="context-files">
            {report.files.map((name) => (
              <li key={name}>
                <a href={`/api/v1/projects/${projectId}/reports/${encodeURIComponent(name)}`} target="_blank" rel="noreferrer">
                  {name}
                </a>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {error && overlay ? <p className="result result--bad">{error}</p> : null}

      {frame ? (
        <div className="frame-modal" role="dialog" aria-label="Frame gốc">
          <button type="button" className="frame-modal__close" onClick={() => setFrame(null)}>
            Đóng
          </button>
          <img src={assetFrameUrl(asset.asset_id, frame)} alt={`Frame ${frame}`} />
          <span className="hint">{frame}</span>
        </div>
      ) : null}
    </div>
  );
}
