import { useCallback, useEffect, useState } from "react";

interface Options {
  itemCount: number;
  itemWidth: number;
  itemHeight: number;
  gap: number;
  overscan?: number;
}

interface Result {
  containerRef: (element: HTMLDivElement | null) => void;
  element: HTMLDivElement | null;
  columns: number;
  rowHeight: number;
  totalHeight: number;
  startIndex: number;
  endIndex: number;
  viewportWidth: number;
  viewportHeight: number;
}

/**
 * Windowed grid: renders only rows intersecting the viewport plus a small
 * overscan, so 5,000 assets never create 5,000 DOM nodes or 5,000 <video> tags.
 *
 * A *callback ref* is used deliberately. The grid is not mounted while the
 * library is empty/loading, so a `useRef` + `[]` effect would measure `null`
 * once and never retry - the real bug that made 1,000 assets render as a single
 * column. The callback re-measures whenever the node actually attaches.
 */
export function useVirtualGrid({ itemCount, itemWidth, itemHeight, gap, overscan = 3 }: Options): Result {
  const [node, setNode] = useState<HTMLDivElement | null>(null);
  const [viewport, setViewport] = useState({ width: 0, height: 0, scrollTop: 0 });

  const containerRef = useCallback((element: HTMLDivElement | null) => {
    setNode(element);
  }, []);

  useEffect(() => {
    if (!node) return;

    const update = () => {
      const rect = node.getBoundingClientRect();
      setViewport({
        width: Math.round(rect.width),
        height: Math.round(rect.height),
        scrollTop: node.scrollTop,
      });
    };

    update();
    const observer = new ResizeObserver(update);
    observer.observe(node);
    node.addEventListener("scroll", update, { passive: true });
    window.addEventListener("resize", update);
    return () => {
      observer.disconnect();
      node.removeEventListener("scroll", update);
      window.removeEventListener("resize", update);
    };
  }, [node]);

  const usable = Math.max(viewport.width, itemWidth);
  const columns = Math.max(1, Math.floor((usable + gap) / (itemWidth + gap)));
  const rowHeight = itemHeight + gap;
  const rowCount = Math.ceil(itemCount / columns);
  const totalHeight = Math.max(0, rowCount * rowHeight - gap);

  const firstVisibleRow = Math.max(0, Math.floor(viewport.scrollTop / rowHeight) - overscan);
  const lastVisibleRow = Math.min(
    rowCount,
    Math.ceil((viewport.scrollTop + viewport.height) / rowHeight) + overscan,
  );

  return {
    containerRef,
    element: node,
    columns,
    rowHeight,
    totalHeight,
    startIndex: Math.min(itemCount, firstVisibleRow * columns),
    endIndex: Math.min(itemCount, lastVisibleRow * columns),
    viewportWidth: viewport.width,
    viewportHeight: viewport.height,
  };
}
