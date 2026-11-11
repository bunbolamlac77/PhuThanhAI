import { useCallback, useEffect, useState } from "react";
import type { AnalysisMethod, AnalysisSummary, Asset, SamplingJob } from "../api";
import { cancelJob, fetchAnalysis, getJob, startAnalysis } from "../api";

interface Props {
  asset: Asset;
  online: boolean;
  onCompleted: (assetId: string) => void;
}

const ACTIVE = new Set(["queued", "running", "cancelling"]);

export default function VisionPanel({ asset, online, onCompleted }: Props) {
  const [method, setMethod] = useState<AnalysisMethod>("auto");
  const [job, setJob] = useState<SamplingJob | null>(null);
  const [summary, setSummary] = useState<AnalysisSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setSummary(await fetchAnalysis(asset.asset_id));
    } catch {
      setSummary(null);
    }
  }, [asset.asset_id]);

  useEffect(() => {
    setJob(null);
    setError(null);
    void load();
  }, [asset.asset_id, load]);

  useEffect(() => {
    if (!job || !ACTIVE.has(job.state)) return;
    const timer = setInterval(async () => {
      try {
        const latest = await getJob(job.job_id);
        setJob(latest);
        if (!ACTIVE.has(latest.state)) {
          await load();
          if (latest.state === "complete") onCompleted(asset.asset_id);
        }
      } catch (cause) {
        setError(cause instanceof Error ? cause.message : "Không đọc được job");
      }
    }, 800);
    return () => clearInterval(timer);
  }, [job, asset.asset_id, load, onCompleted]);

  async function start() {
    setBusy(true);
    setError(null);
    try {
      setJob(await startAnalysis(asset.asset_id, method));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Không bắt đầu được phân tích");
    } finally {
      setBusy(false);
    }
  }

  async function cancel() {
    if (!job) return;
    try {
      setJob(await cancelJob(job.job_id));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Không huỷ được");
    }
  }

  const running = job !== null && ACTIVE.has(job.state);
  const percent = Math.round((job?.progress ?? 0) * 100);
  const observed = summary?.frames_observed ?? 0;
  const unknown = summary?.frames_unknown ?? 0;
  const evidence = summary?.evidence;

  return (
    <div className="vision">
      <div className="vision__head">
        <strong>Phân tích thị giác (Qwen3-VL)</strong>
        <span className={`badge ${summary?.analysis_status === "complete" ? "badge--ok" : unknown > 0 ? "badge--warn" : ""}`}>
          {summary?.analysis_status ?? asset.analysis_status ?? "not_started"}
        </span>
      </div>

      <div className="vision__controls">
        <select
          value={method}
          aria-label="Phương pháp phân tích"
          disabled={running}
          onChange={(event) => setMethod(event.target.value as AnalysisMethod)}
        >
          <option value="auto">Tự động</option>
          <option value="single_frame">Từng frame</option>
          <option value="sequential_frames">Chuỗi frame</option>
        </select>
        <button
          type="button"
          disabled={!online || busy || running || asset.sampling_status !== "complete"}
          onClick={() => void start()}
        >
          Phân tích
        </button>
        {running ? (
          <button type="button" onClick={() => void cancel()}>
            Huỷ
          </button>
        ) : null}
        {asset.sampling_status !== "complete" ? <span className="hint">Cần sampling trước</span> : null}
      </div>

      {job ? (
        <div className="vision__progress">
          <div className="progress">
            <div className="progress__bar" style={{ width: `${percent}%` }} />
          </div>
          <span className="hint">
            {job.state} · {percent}%{job.error_code ? ` · ${job.error_code}` : ""}
          </span>
        </div>
      ) : null}

      {error ? <p className="result result--bad">{error}</p> : null}

      {summary && summary.analysis_id ? (
        <div className="vision__summary">
          <div className="vision__stats">
            <span>
              {observed}/{summary.frames_total ?? observed + unknown} frame có quan sát
            </span>
            {unknown > 0 ? <span className="badge badge--danger">{unknown} chưa xác định</span> : null}
            {summary.coverage_status && summary.coverage_status !== "sufficient" ? (
              <span className="badge badge--warn">coverage: {summary.coverage_status}</span>
            ) : null}
            {summary.method ? <span className="badge">{summary.method}</span> : null}
          </div>
          {(summary.warnings ?? []).length > 0 ? (
            <ul className="vision__warnings">
              {summary.warnings.slice(0, 4).map((warning) => (
                <li key={warning}>{warning}</li>
              ))}
            </ul>
          ) : null}
          {evidence ? (
            <details className="vision__evidence">
              <summary>Bằng chứng thị giác ({evidence.frames.length})</summary>
              <ul className="evidence-list">
                {evidence.frames.map((frame) => (
                  <li key={frame.frame_id} className={frame.interpretation_level === "unknown" ? "evidence--unknown" : ""}>
                    <div className="evidence-list__head">
                      <span>{frame.frame_id}</span>
                      <span>{frame.scene_id}</span>
                      <span>{frame.normalized_pts_ms} ms</span>
                      <span className="badge">{frame.interpretation_level}</span>
                    </div>
                    {frame.observation ? (
                      <p>{frame.observation.description}</p>
                    ) : (
                      <p className="hint">Không có quan sát hợp lệ: {frame.uncertainty.join("; ")}</p>
                    )}
                  </li>
                ))}
              </ul>
            </details>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
