"""Measured resource facts for the ResourceGovernor.

Nothing here guesses: every number is read from the OS. The classification
thresholds are explicitly marked **provisional** because the architecture
document requires them to be calibrated by benchmark on the target M1 Pro - until
that benchmark exists, this module reports measurements plus a provisional level
and never claims the thresholds are safe.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_VM_STAT_PAGE = re.compile(r"page size of (\d+) bytes")
_VM_STAT_LINE = re.compile(r"^(.+?):\s+(\d+)\.?\s*$")
_SWAP = re.compile(r"total = ([\d.]+)M\s+used = ([\d.]+)M\s+free = ([\d.]+)M")


@dataclass(frozen=True)
class Thresholds:
    """Provisional policy thresholds (bytes are fractions of physical RAM)."""

    # Calibrated against measured S00/S03 runs on this 16 GiB M1 Pro: a 4.1 GiB
    # MLX peak completed with ~66% free memory and 2.7 GiB swap in use. The earlier
    # 10%/30% swap thresholds refused every inference on a normal desktop workload.
    pressured_available_ratio: float = 0.12
    critical_available_ratio: float = 0.05
    pressured_swap_ratio: float = 0.75
    critical_swap_ratio: float = 0.90
    critical_pressure_free_pct: float = 10.0
    pressured_pressure_free_pct: float = 30.0
    min_free_disk_bytes: int = 5 * 1024**3
    provisional: bool = field(default=True)


THRESHOLDS = Thresholds()


def _run(argv: list[str]) -> str | None:
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout


def vm_stat_pages() -> tuple[dict[str, int], int] | None:
    """Parse ``vm_stat`` into (label -> pages, page_size_bytes)."""

    text = _run(["/usr/bin/vm_stat"])
    if not text:
        return None
    page_size = 4096
    pages: dict[str, int] = {}
    first_line = text.splitlines()[0] if text.splitlines() else ""
    match = _VM_STAT_PAGE.search(first_line)
    if match:
        page_size = int(match.group(1))
    for line in text.splitlines()[1:]:
        m = _VM_STAT_LINE.match(line.strip())
        if m:
            pages[m.group(1).strip()] = int(m.group(2))
    if not pages:
        return None
    return pages, page_size


def swap_usage() -> tuple[int, int] | None:
    """Return (used_bytes, total_bytes) from ``sysctl vm.swapusage``."""

    text = _run(["/usr/sbin/sysctl", "-n", "vm.swapusage"])
    if not text:
        return None
    match = _SWAP.search(text)
    if not match:
        return None
    total_mb, used_mb = float(match.group(1)), float(match.group(2))
    return int(used_mb * 1024**2), int(total_mb * 1024**2)


def physical_memory_bytes() -> int | None:
    text = _run(["/usr/sbin/sysctl", "-n", "hw.memsize"])
    if not text:
        return None
    try:
        return int(text.strip())
    except ValueError:
        return None


def system_pressure_pct() -> float | None:
    """Official macOS free-memory percentage from ``memory_pressure``, if present."""

    text = _run(["/usr/bin/memory_pressure"])
    if not text:
        return None
    match = re.search(r"System-wide memory free percentage:\s*(\d+)%", text)
    return float(match.group(1)) if match else None


@dataclass(frozen=True)
class ResourceSnapshot:
    total_bytes: int | None
    available_bytes: int | None
    free_bytes: int | None
    wired_bytes: int | None
    compressor_bytes: int | None
    swap_used_bytes: int | None
    swap_total_bytes: int | None
    pressure_free_pct: float | None
    disk_free_bytes: int
    disk_total_bytes: int
    level: str
    reasons: list[str]
    thresholds_provisional: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_bytes": self.total_bytes,
            "available_bytes": self.available_bytes,
            "free_bytes": self.free_bytes,
            "wired_bytes": self.wired_bytes,
            "compressor_bytes": self.compressor_bytes,
            "swap_used_bytes": self.swap_used_bytes,
            "swap_total_bytes": self.swap_total_bytes,
            "pressure_free_pct": self.pressure_free_pct,
            "disk_free_bytes": self.disk_free_bytes,
            "disk_total_bytes": self.disk_total_bytes,
            "level": self.level,
            "reasons": list(self.reasons),
            "thresholds_provisional": self.thresholds_provisional,
        }


def snapshot(path: Path | None = None) -> ResourceSnapshot:
    """Measure memory, swap and disk; classify with provisional thresholds."""

    total = physical_memory_bytes()
    stats = vm_stat_pages()
    swap = swap_usage()
    pressure_pct = system_pressure_pct()

    free = wired = compressor = available = None
    if stats is not None:
        pages, page_size = stats
        free = pages.get("Pages free", 0) * page_size
        wired = pages.get("Pages wired down", 0) * page_size
        compressor = pages.get("Pages occupied by compressor", 0) * page_size
        reclaimable = (
            pages.get("Pages free", 0)
            + pages.get("Pages inactive", 0)
            + pages.get("Pages speculative", 0)
            + pages.get("Pages purgeable", 0)
        )
        available = reclaimable * page_size

    usage_path = path or Path.home()
    try:
        usage = shutil.disk_usage(str(usage_path))
        disk_free, disk_total = usage.free, usage.total
    except OSError:
        disk_free, disk_total = 0, 0

    reasons: list[str] = []
    level = "unknown"
    if total and available is not None:
        ratio = available / total
        if ratio < THRESHOLDS.critical_available_ratio:
            level = "critical"
            reasons.append(f"available memory {ratio:.1%} < {THRESHOLDS.critical_available_ratio:.0%}")
        elif ratio < THRESHOLDS.pressured_available_ratio:
            level = "pressured"
            reasons.append(f"available memory {ratio:.1%} < {THRESHOLDS.pressured_available_ratio:.0%}")
        else:
            level = "normal"

    # macOS's own free-memory percentage is the primary pressure signal when present.
    if pressure_pct is not None:
        if pressure_pct < THRESHOLDS.critical_pressure_free_pct:
            level = "critical"
            reasons.append(f"memory pressure free {pressure_pct:.0f}% < {THRESHOLDS.critical_pressure_free_pct:.0f}%")
        elif pressure_pct < THRESHOLDS.pressured_pressure_free_pct and level == "normal":
            level = "pressured"
            reasons.append(f"memory pressure free {pressure_pct:.0f}% < {THRESHOLDS.pressured_pressure_free_pct:.0f}%")

    if swap and swap[1] > 0:
        swap_ratio = swap[0] / swap[1]
        if swap_ratio >= THRESHOLDS.critical_swap_ratio:
            reasons.append(f"swap used {swap_ratio:.1%} >= {THRESHOLDS.critical_swap_ratio:.0%}")
            level = "critical"
        elif swap_ratio >= THRESHOLDS.pressured_swap_ratio and level == "normal":
            reasons.append(f"swap used {swap_ratio:.1%} >= {THRESHOLDS.pressured_swap_ratio:.0%}")
            level = "pressured"

    if 0 < disk_free < THRESHOLDS.min_free_disk_bytes:
        reasons.append(f"free disk {disk_free / 1024**3:.1f} GiB below 5 GiB floor")
        if level == "normal":
            level = "pressured"

    return ResourceSnapshot(
        total_bytes=total,
        available_bytes=available,
        free_bytes=free,
        wired_bytes=wired,
        compressor_bytes=compressor,
        swap_used_bytes=swap[0] if swap else None,
        swap_total_bytes=swap[1] if swap else None,
        pressure_free_pct=pressure_pct,
        disk_free_bytes=disk_free,
        disk_total_bytes=disk_total,
        level=level,
        reasons=reasons,
    )


class ResourceGovernor:
    """Read-only view over measured resources; a single instance is shared."""

    def snapshot(self) -> dict[str, Any]:
        return snapshot().to_dict()

    def can_start_heavy_worker(self, *, min_free_disk_bytes: int = THRESHOLDS.min_free_disk_bytes) -> tuple[bool, str]:
        current = snapshot()
        if current.disk_free_bytes < min_free_disk_bytes:
            return False, "STORAGE_FULL"
        if current.level == "critical":
            return False, "MEMORY_CRITICAL"
        if current.available_bytes is not None and current.available_bytes < 1536 * 1024**2:
            return False, "MEMORY_CRITICAL"
        if current.level == "pressured":
            return True, "MEMORY_PRESSURED"
        return True, "OK"


GOVERNOR = ResourceGovernor()
