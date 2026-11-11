import { useCallback, useEffect, useRef, useState } from "react";
import type { Asset } from "../api";
import { ApiError, assetStreamUrl, createProxy, proxyStreamUrl, thumbnailUrl } from "../api";
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

function embeddedWebKit(): boolean {
  if (typeof window === "undefined") return false;
  const ua = navigator.userAgent;
  const injected = Boolean((window as unknown as { __PHUTHANHAI_EMBEDDED__?: boolean }).__PHUTHANHAI_EMBEDDED__);
  const hasWebKitBridge = Boolean((window as unknown as { webkit?: { messageHandlers?: unknown } }).webkit?.messageHandlers);
  return injected || (hasWebKitBridge && !/Chrome|Chromium|Edg\//.test(ua));
}

export default function Player({ asset, onTime, onController }: Props) {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
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

  const codec = (asset.codec ?? "").toLowerCase();
  // WKWebView decodes HEVC into frames but composites them black. H.264 paints
  // correctly, so non-H.264 sources use the existing on-demand H.264 proxy.
  const needsProxy = embeddedWebKit() && codec !== "h264" && codec !== "";

  useEffect(() => {
    if (needsProxy && mode === "direct" && !proxyBusy) {
      void switchToProxy();
    }
  }, [needsProxy, mode, proxyBusy, switchToProxy, asset.asset_id]);

  const embedded = embeddedWebKit();

  useEffect(() => {
    if (!embedded) return;
    const video = videoRef.current;
    const canvas = canvasRef.current;
    if (!video || !canvas) return;
    const context = canvas.getContext("2d");
    if (!context) return;
    let raf = 0;
    let last = 0;

    const draw = () => {
      const cw = canvas.clientWidth;
      const ch = canvas.clientHeight;
      if (!cw || !ch) return;
      if (canvas.width !== cw || canvas.height !== ch) {
        canvas.width = cw;
        canvas.height = ch;
      }
      const vw = video.videoWidth;
      const vh = video.videoHeight;
      if (video.readyState < 2 || !vw || !vh) return;
      const scale = Math.min(cw / vw, ch / vh);
      const dw = vw * scale;
      const dh = vh * scale;
      context.fillStyle = "#05080d";
      context.fillRect(0, 0, cw, ch);
      context.drawImage(video, (cw - dw) / 2, (ch - dh) / 2, dw, dh);
    };

    const tick = (time: number) => {
      if (time - last > 33) {
        draw();
        last = time;
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    const redraw = () => draw();
    video.addEventListener("seeked", redraw);
    video.addEventListener("loadeddata", redraw);
    video.addEventListener("pause", redraw);
    return () => {
      cancelAnimationFrame(raf);
      video.removeEventListener("seeked", redraw);
      video.removeEventListener("loadeddata", redraw);
      video.removeEventListener("pause", redraw);
    };
  }, [embedded, src]);

  const durationMs = asset.duration_ms ?? 0;

  return (
    <div className="player">
      <div className="player__stage">
        {embedded ? (
          <>
            <img className="player__poster" src={thumbnailUrl(asset.asset_id)} alt="" aria-hidden="true" />
            <canvas ref={canvasRef} className="player__canvas" aria-hidden="true" />
          </>
        ) : null}
        <video
          key={src}
          style={embedded ? { opacity: 0 } : undefined}
          ref={videoRef}
          src={src}
          poster={thumbnailUrl(asset.asset_id)}
          preload="auto"
          playsInline
          onError={handleError}
          onLoadedMetadata={(event) => {
            // Paint a real frame instead of a black stage before the first play.
            const video = event.currentTarget;
            if (video.currentTime === 0 && Number.isFinite(video.duration) && video.duration > 0) {
              video.currentTime = Math.min(0.05, video.duration / 10);
            }
          }}
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
        {needsProxy && mode === "proxy" ? (
          <span className="hint">WKWebView: dùng proxy H.264 để hiển thị hình</span>
        ) : null}
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
