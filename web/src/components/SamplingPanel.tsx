import { useCallback, useEffect, useState } from "react";
import type { Asset, SamplingJob, SamplingProfile, SamplingSummary } from "../api";
import { cancelJob, fetchSampling, getJob, startSampling } from "../api";

interface Props {
  asset: Asset;
  online: boolean;
  onCompleted: (assetId: string) => void;
}

const PROFILES: Array<{ id: SamplingProfile; label: string; gap: string }> = [
  { id: "quick", label: "Quick", gap: "~12 s" },
  { id: "detailed", label: "Detailed · lấy mẫu khoảng 6 giây/frame", gap: "~6 s" },
  { id: "thorough", label: "Thorough", gap: "~3 s" },
];

const ACTIVE_STATES = new Set(["queued", "running", "cancelling"]);

export default function SamplingPanel({ asset, online, onCompleted }: Props) {
  const [profile, setProfile] = useState<SamplingProfile>(
    (asset.sampling_profile as SamplingProfile | null) ?? "detailed",
  );
  const [job, setJob] = useState<SamplingJob | null>(null);
  const [summary, setSummary] = useState<SamplingSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const loadSummary = useCallback(async () => {
    try {
      setSummary(await fetchSampling(asset.asset_id));
    } catch {
      setSummary(null);
    }
  }, [asset.asset_id]);

  useEffect(() => {
    setJob(null);
    setError(null);
    setProfile((asset.sampling_profile as SamplingProfile | null) ?? "detailed");
    void loadSummary();
  }, [asset.asset_id, asset.sampling_profile, loadSummary]);

  useEffect(() => {
    if (!job || !ACTIVE_STATES.has(job.state)) return;
    const timer = setInterval(async () => {
      try {
        const latest = await getJob(job.job_id);
        setJob(latest);
        if (!ACTIVE_STATES.has(latest.state)) {
          await loadSummary();
          if (latest.state === "complete") onCompleted(asset.asset_id);
        }
      } catch (cause) {
        setError(cause instanceof Error ? cause.message : "Không đọc được trạng thái job");
      }
    }, 700);
    return () => clearInterval(timer);
  }, [job, asset.asset_id, loadSummary, onCompleted]);

  async function start() {
    setBusy(true);
    setError(null);
    try {
      setJob(await startSampling(asset.asset_id, profile));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Không bắt đầu được");
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

  const running = job !== null && ACTIVE_STATES.has(job.state);
  const percent = Math.round((job?.progress ?? 0) * 100);
  const manifest = summary?.manifest ?? null;

  return (
    <div className="sampling">
      <div className="sampling__head">
        <strong>Tiền xử lý S02 (nâng cao)</strong>
        <span className={`badge ${summary?.sampling_status === "complete" ? "badge--ok" : ""}`}>
          {summary?.sampling_status ?? asset.sampling_status ?? "not_sampled"}
        </span>
      </div>

      <div className="sampling__controls">
        <select
          value={profile}
          aria-label="Sampling profile"
          disabled={running}
          onChange={(event) => setProfile(event.target.value as SamplingProfile)}
        >
          {PROFILES.map((entry) => (
            <option key={entry.id} value={entry.id}>
              {entry.label} ({entry.gap})
            </option>
          ))}
        </select>
        <button type="button" disabled={!online || busy || running} onClick={() => void start()}>
          Chuẩn bị phân tích
        </button>
        {running ? (
          <button type="button" onClick={() => void cancel()}>
            Huỷ
          </button>
        ) : null}
      </div>

      {job ? (
        <div className="sampling__progress">
          <div className="progress">
            <div className="progress__bar" style={{ width: `${percent}%` }} />
          </div>
          <span className="hint">
            {job.state} · {percent}%{job.error_code ? ` · ${job.error_code}` : ""}
          </span>
        </div>
      ) : null}

      {error ? <p className="result result--bad">{error}</p> : null}

      {manifest ? (
        <div className="sampling__summary">
          <span>{manifest.frame_count} frame</span>
          <span>{manifest.scene_count} cảnh</span>
          <span>
            gap thực tế {manifest.observed_max_gap_ms ?? "—"} ms (mục tiêu{" "}
            {manifest.expected_max_gap_ms ?? "—"} ms)
          </span>
          {manifest.warnings && manifest.warnings.length > 0 ? (
            <span className="badge badge--warn">{manifest.warnings.length} cảnh báo</span>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
