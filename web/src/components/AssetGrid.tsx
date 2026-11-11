import { useEffect, useState } from "react";
import type { Asset } from "../api";
import { thumbnailUrl } from "../api";
import { formatShort, displayDimensions } from "../lib/format";
import { useVirtualGrid } from "../lib/useVirtualGrid";

interface Props {
  assets: Asset[];
  activeId: string | null;
  selected: Set<string>;
  loading: boolean;
  hasMore: boolean;
  onActivate: (asset: Asset) => void;
  onToggleSelect: (asset: Asset) => void;
  onLoadMore: () => void;
}

const CARD_WIDTH = 142;
const CARD_HEIGHT = 176;
const GAP = 10;

type ThumbnailState = "loading" | "ready" | "error";

function FilmIcon() {
  return (
    <svg width="32" height="32" viewBox="0 0 48 48" fill="none" stroke="currentColor"
      strokeWidth="1.6" aria-hidden="true">
      <rect x="8" y="10" width="32" height="28" rx="4" />
      <path d="M17 10v28M31 10v28M8 19h9M8 29h9M31 19h9M31 29h9" />
      <path d="M22 20l6 4-6 4v-8z" fill="currentColor" stroke="none" />
    </svg>
  );
}

function Thumbnail({ asset }: { asset: Asset }) {
  const [state, setState] = useState<ThumbnailState>("loading");
  const available = asset.source_state === "available";
  const label = !available
    ? "Nguồn không khả dụng"
    : state === "error"
      ? "Không có ảnh xem trước"
      : "Đang tải ảnh";

  return (
    <>
      {state !== "ready" || !available ? (
        <div className="asset-card__fallback" aria-hidden="true">
          <FilmIcon />
          <span>{label}</span>
        </div>
      ) : null}
      {available ? (
        <img
          src={thumbnailUrl(asset.asset_id)}
          className={state === "ready" ? "asset-card__image is-loaded" : "asset-card__image"}
          alt=""
          loading="lazy"
          decoding="async"
          draggable={false}
          hidden={state === "error"}
          onLoad={() => setState("ready")}
          onError={() => setState("error")}
        />
      ) : null}
    </>
  );
}

export default function AssetGrid({
  assets, activeId, selected, loading, hasMore, onActivate, onToggleSelect, onLoadMore,
}: Props) {
  const {
    containerRef, element, columns, rowHeight, totalHeight, startIndex, endIndex,
    viewportWidth, viewportHeight,
  } = useVirtualGrid({
    itemCount: assets.length,
    itemWidth: CARD_WIDTH,
    itemHeight: CARD_HEIGHT,
    gap: GAP,
  });

  useEffect(() => {
    if (!element) return;
    const onScroll = () => {
      if (loading || !hasMore) return;
      const distance = element.scrollHeight - element.scrollTop - element.clientHeight;
      if (distance < 600) onLoadMore();
    };
    element.addEventListener("scroll", onScroll, { passive: true });
    return () => element.removeEventListener("scroll", onScroll);
  }, [element, hasMore, loading, onLoadMore]);

  if (assets.length === 0) {
    return (
      <div className="empty-state studio-library-empty" role="status">
        <div className="studio-library-empty__icon"><FilmIcon /></div>
        <p className="empty-state__title">{loading ? "Đang tải thư viện…" : "Chưa có video"}</p>
        <p>{loading ? "Đang đọc dữ liệu clip." : "Chọn dự án và quét thư mục để thêm footage."}</p>
      </div>
    );
  }

  const visible = assets.slice(startIndex, endIndex);

  return (
    <div
      ref={containerRef}
      className="asset-grid studio-asset-grid"
      role="listbox"
      aria-multiselectable="true"
      aria-label="Danh sách video"
      data-columns={columns}
      data-window={`${startIndex}-${endIndex}`}
      data-total={assets.length}
      data-viewport={`${viewportWidth}x${viewportHeight}`}
    >
      <div className="asset-grid__inner" style={{ height: totalHeight }}>
        {visible.map((asset, offset) => {
          const index = startIndex + offset;
          const row = Math.floor(index / columns);
          const column = index % columns;
          const active = asset.asset_id === activeId;
          const checked = selected.has(asset.asset_id);
          return (
            <div
              key={asset.asset_id}
              role="option"
              tabIndex={0}
              aria-selected={active}
              aria-label={`${asset.filename}, ${formatShort(asset.duration_ms)}`}
              className={[
                "asset-card",
                "studio-asset-card",
                active ? "asset-card--active" : "",
                checked ? "asset-card--selected" : "",
                asset.source_state !== "available" ? "asset-card--missing" : "",
              ].filter(Boolean).join(" ")}
              style={{
                width: CARD_WIDTH,
                height: CARD_HEIGHT,
                transform: `translate(${column * (CARD_WIDTH + GAP)}px, ${row * rowHeight}px)`,
              }}
              onClick={() => onActivate(asset)}
              onKeyDown={(event) => {
                if (event.target !== event.currentTarget) return;
                if (event.key === "Enter") {
                  event.preventDefault();
                  onActivate(asset);
                } else if (event.key === " ") {
                  event.preventDefault();
                  event.stopPropagation();
                  onToggleSelect(asset);
                }
              }}
            >
              <div className="asset-card__thumb">
                <Thumbnail asset={asset} />
                <span className="asset-card__duration">{formatShort(asset.duration_ms)}</span>
                <label className="asset-card__check" onClick={(event) => event.stopPropagation()}>
                  <input
                    type="checkbox"
                    checked={checked}
                    aria-label={`Chọn ${asset.filename}`}
                    onChange={() => onToggleSelect(asset)}
                  />
                </label>
                {asset.review?.favorite ? <span className="asset-card__star">★</span> : null}
              </div>
              <div className="asset-card__name" title={asset.filename}>{asset.filename}</div>
              <div className="asset-card__meta">
                <span>{(asset.codec ?? "Video").toUpperCase()}</span>
                <span>{displayDimensions(asset.width, asset.height, asset.rotation)}</span>
              </div>
              <div className="asset-card__badges">
                {(asset.note_count ?? 0) > 0 ? (
                  <span className="badge badge--note">{asset.note_count} ghi chú</span>
                ) : null}
                {asset.review?.rating != null ? <span className="badge">★ {asset.review.rating}</span> : null}
                {asset.sampling_status === "complete" ? <span className="badge badge--ok">Đã lấy mẫu</span> : null}
                {asset.source_state !== "available" ? <span className="badge badge--danger">Thiếu nguồn</span> : null}
              </div>
            </div>
          );
        })}
        {loading ? <div className="asset-grid__loading">Đang tải thêm video…</div> : null}
      </div>
    </div>
  );
}
