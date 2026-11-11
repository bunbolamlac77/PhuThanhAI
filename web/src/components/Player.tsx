import { useCallback, useEffect, useRef, useState } from "react";
import type { Asset } from "../api";
import { ApiError, assetStreamUrl, createProxy, proxyStreamUrl } from "../api";
import { formatTimecode } from "../lib/format";

export interface PlayerController {
  toggle: () => void;
  seekBy: (seconds: number) => void;
  seekTo: (ms: number) => void;
  currentMs: () => number;
  isPlaying: () => boolean;
}

interface Props {
  asset: Asset;
  onTime: (ms: number) => void;
  onController: (controller: PlayerController | null) => void;
}

type Mode = "direct" | "proxy";

export default function Player({ asset, onTime, onController }: Props) {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const [src, setSrc] = useState(() => assetStreamUrl(asset.asset_id));
  const [mode, setMode] = useState<Mode>("direct");
  const [error, setError] = useState<string | null>(null);
  const [proxyBusy, setProxyBusy] = useState(false);
  const [playing, setPlaying] = useState(false);
  const [rate, setRate] = useState(1);
  const [currentMs, setCurrentMs] = useState(0);

  // Reset when the active clip changes.
  useEffect(() => {
    setSrc(assetStreamUrl(asset.asset_id));
    setMode("direct");
    setError(null);
    setProxyBusy(false);
    setCurrentMs(0);
    setPlaying(false);
  }, [asset.asset_id]);

  const buildController = useCallback((): PlayerController => {
    return {
      toggle: () => {
        const video = videoRef.current;
        if (!video) return;
        if (video.paused) void video.play().catch(() => setError("Không phát được video"));
        else video.pause();
      },
      seekBy: (seconds: number) => {
        const video = videoRef.current;
        if (!video) return;
        video.currentTime = Math.max(0, Math.min(video.duration || 0, video.currentTime + seconds));
      },
      seekTo: (ms: number) => {
        const video = videoRef.current;
        if (!video) return;
        video.currentTime = Math.max(0, ms / 1000);
      },
      currentMs: () => Math.round((videoRef.current?.currentTime ?? 0) * 1000),
      isPlaying: () => Boolean(videoRef.current && !videoRef.current.paused),
    };
  }, []);

  useEffect(() => {
    onController(buildController());
    return () => onController(null);
  }, [buildController, onController]);

  const switchToProxy = useCallback(async () => {
    if (proxyBusy) return;
    setProxyBusy(true);
    setError(null);
    try {
      await createProxy(asset.asset_id);
      setSrc(proxyStreamUrl(asset.asset_id));
      setMode("proxy");
    } catch (cause) {
      const message = cause instanceof ApiError ? cause.message : "Tạo proxy thất bại";
      setError(message);
    } finally {
      setProxyBusy(false);
    }
  }, [asset.asset_id, proxyBusy]);

  const handleError = useCallback(() => {
    if (mode === "direct") {
      void switchToProxy();
    } else {
      setError("Không phát được cả nguồn lẫn proxy");
    }
  }, [mode, switchToProxy]);

  const durationMs = asset.duration_ms ?? 0;

  return (
    <div className="player">
      <div className="player__stage">
        <video
          ref={videoRef}
          src={src}
          preload="metadata"
          playsInline
          onError={handleError}
          onTimeUpdate={(event) => {
            const ms = Math.round(event.currentTarget.currentTime * 1000);
            setCurrentMs(ms);
            onTime(ms);
          }}
          onPlay={() => setPlaying(true)}
          onPause={() => setPlaying(false)}
          onRateChange={(event) => setRate(event.currentTarget.playbackRate)}
        />
        {proxyBusy ? <div className="player__overlay">Đang tạo proxy H.264…</div> : null}
        {error ? <div className="player__overlay player__overlay--error">{error}</div> : null}
      </div>

      <div className="transport">
        <button type="button" onClick={() => buildController().toggle()} aria-label={playing ? "Tạm dừng" : "Phát"}>
          {playing ? "⏸" : "▶"}
        </button>
        <button type="button" onClick={() => buildController().seekBy(-5)} aria-label="Lùi 5 giây">
          ⏪
        </button>
        <button type="button" onClick={() => buildController().seekBy(5)} aria-label="Tiến 5 giây">
          ⏩
        </button>
        <span className="transport__time">
          {formatTimecode(currentMs)} / {formatTimecode(durationMs)}
        </span>
        <select
          value={rate}
          aria-label="Tốc độ phát"
          onChange={(event) => {
            const value = Number(event.target.value);
            if (videoRef.current) videoRef.current.playbackRate = value;
            setRate(value);
          }}
        >
          {[0.5, 0.75, 1, 1.25, 1.5, 2].map((option) => (
            <option key={option} value={option}>
              {option}×
            </option>
          ))}
        </select>
        <span className={mode === "proxy" ? "badge badge--warn" : "badge"}>
          {mode === "proxy" ? "proxy H.264" : "nguồn trực tiếp"}
        </span>
      </div>

      <input
        className="scrubber"
        type="range"
        min={0}
        max={Math.max(durationMs, 1)}
        value={Math.min(currentMs, Math.max(durationMs, 1))}
        aria-label="Tua video"
        onChange={(event) => buildController().seekTo(Number(event.target.value))}
      />
    </div>
  );
}
