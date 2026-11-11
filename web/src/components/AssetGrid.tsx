import { useEffect } from "react";
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

const CARD_WIDTH = 190;
const CARD_HEIGHT = 188;
const GAP = 8;

export default function AssetGrid({
  assets,
  activeId,
  selected,
  loading,
  hasMore,
  onActivate,
  onToggleSelect,
  onLoadMore,
}: Props) {
  const { containerRef, element, columns, rowHeight, totalHeight, startIndex, endIndex, viewportWidth, viewportHeight } = useVirtualGrid({
    itemCount: assets.length,
    itemWidth: CARD_WIDTH,
    itemHeight: CARD_HEIGHT,
    gap: GAP,
  });

  useEffect(() => {
    if (!element) return;
    const handler = () => {
      if (!hasMore || loading) return;
      if (element.scrollTop + element.clientHeight > element.scrollHeight - 600) {
        onLoadMore();
      }
    };
    element.addEventListener("scroll", handler, { passive: true });
    return () => element.removeEventListener("scroll", handler);
  }, [element, hasMore, loading, onLoadMore]);

  if (!loading && assets.length === 0) {
    return (
      <div className="empty-state">
        <p className="empty-state__title">Chưa có asset nào</p>
        <p>Nhập đường dẫn thư mục footage rồi bấm “Quét thư mục”. Chỉ file video mới được index.</p>
      </div>
    );
  }

  const window = assets.slice(startIndex, endIndex);

  return (
    <div
      className="asset-grid"
      ref={containerRef}
      role="listbox"
      aria-label="Danh sách video"
      data-columns={columns}
      data-window={`${startIndex}-${endIndex}`}
      data-total={assets.length}
      data-viewport={`${viewportWidth}x${viewportHeight}`}
    >
      <div className="asset-grid__inner" style={{ height: totalHeight }}>
        {window.map((asset, offset) => {
          const index = startIndex + offset;
          const row = Math.floor(index / columns);
          const column = index % columns;
          const isActive = asset.asset_id === activeId;
          const isSelected = selected.has(asset.asset_id);
          const noteCount = asset.note_count ?? 0;
          return (
            <div
              key={asset.asset_id}
              className={[
                "asset-card",
                isActive ? "asset-card--active" : "",
                isSelected ? "asset-card--selected" : "",
                asset.source_state !== "available" ? "asset-card--missing" : "",
              ]
                .filter(Boolean)
                .join(" ")}
              style={{
                width: CARD_WIDTH,
                height: CARD_HEIGHT,
                transform: `translate(${column * (CARD_WIDTH + GAP)}px, ${row * rowHeight}px)`,
              }}
              role="option"
              aria-selected={isActive}
              tabIndex={0}
              onClick={() => onActivate(asset)}
              onKeyDown={(event) => {
                if (event.key === "Enter") onActivate(asset);
              }}
            >
              <div className="asset-card__thumb">
                <img src={thumbnailUrl(asset.asset_id)} alt="" loading="lazy" draggable={false} />
                <span className="asset-card__duration">{formatShort(asset.duration_ms)}</span>
                <label className="asset-card__check" onClick={(event) => event.stopPropagation()}>
                  <input
                    type="checkbox"
                    checked={isSelected}
                    onChange={() => onToggleSelect(asset)}
                    aria-label={`Chọn ${asset.filename}`}
                  />
                </label>
                {asset.review?.favorite ? <span className="asset-card__star">★</span> : null}
              </div>
              <div className="asset-card__name" title={asset.filename}>
                {asset.filename}
              </div>
              <div className="asset-card__meta">
                <span>{asset.codec ?? "—"}</span>
                <span>{displayDimensions(asset.width, asset.height, asset.rotation)}</span>
              </div>
              <div className="asset-card__badges">
                {noteCount > 0 ? <span className="badge badge--note">{noteCount} ghi chú</span> : null}
                {asset.review?.rating ? <span className="badge">★ {asset.review.rating}</span> : null}
                {asset.source_state !== "available" ? (
                  <span className="badge badge--danger">{asset.source_state}</span>
                ) : null}
                {asset.error_code ? <span className="badge badge--danger">{asset.error_code}</span> : null}
                {asset.sampling_status === "complete" ? (
                  <span className="badge badge--ok">sampled</span>
                ) : null}
                {asset.sampling_status === "sampling" || asset.sampling_status === "queued" ? (
                  <span className="badge badge--warn">sampling</span>
                ) : null}
              </div>
            </div>
          );
        })}
        {loading ? <div className="asset-grid__loading">Đang tải…</div> : null}
      </div>
    </div>
  );
}
