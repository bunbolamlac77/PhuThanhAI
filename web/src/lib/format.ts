export function formatTimecode(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || Number.isNaN(ms)) return "00:00:00.000";
  const total = Math.max(0, Math.round(ms));
  const hours = Math.floor(total / 3_600_000);
  const minutes = Math.floor((total % 3_600_000) / 60_000);
  const seconds = Math.floor((total % 60_000) / 1000);
  const millis = total % 1000;
  return `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}.${String(millis).padStart(3, "0")}`;
}

export function formatShort(ms: number | null | undefined): string {
  if (!ms && ms !== 0) return "—";
  const total = Math.max(0, Math.round(ms / 1000));
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

export function formatBytes(bytes: number): string {
  if (!bytes) return "0 B";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  const exponent = Math.min(units.length - 1, Math.floor(Math.log(bytes) / Math.log(1024)));
  return `${(bytes / Math.pow(1024, exponent)).toFixed(1)} ${units[exponent]}`;
}

export function displayDimensions(width: number | null, height: number | null, rotation: number): string {
  if (!width || !height) return "—";
  const swapped = rotation === 90 || rotation === 270;
  const w = swapped ? height : width;
  const h = swapped ? width : height;
  return `${w}×${h}${swapped ? " (xoay)" : ""}`;
}
