#!/usr/bin/env python3
"""A/B FFmpeg scene-scan cost: decode, scale, stats, scdet, blur, VideoToolbox."""

from __future__ import annotations

import argparse
import json
import resource
import subprocess
import tempfile
import time
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from phuthanhai.analyzer.probe import find_tool  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "qa" / "S05" / "optimization"

FILTERS = {
    "A_decode_only": None,
    "B_scale160": "scale=160:-2",
    "C_scale_signalstats": "scale=160:-2,signalstats",
    "D_scale_stats_scdet": "scale=160:-2,signalstats,scdet=threshold=15",
    "E_full": "scale=160:-2,signalstats,scdet=threshold=15,blurdetect,metadata=mode=print",
}


def run(name: str, clip: Path, vf: str | None, hw: bool) -> dict:
    handle = tempfile.NamedTemporaryFile(suffix=".txt", delete=False)
    handle.close()
    time_path = Path(handle.name)
    binary = find_tool("ffmpeg")
    if not binary:
        raise SystemExit("ffmpeg not found")
    argv = ["/usr/bin/time", "-l", "-o", str(time_path), binary, "-nostdin", "-hide_banner", "-v", "error"]
    if hw:
        argv += ["-hwaccel", "videotoolbox"]
    argv += ["-i", str(clip)]
    if vf:
        argv += ["-vf", vf]
    argv += ["-an", "-f", "null", "-"]
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    sys_before = time.monotonic()
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=600)
    wall = time.monotonic() - sys_before
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    time_text = time_path.read_text(encoding="utf-8", errors="replace") if time_path.is_file() else ""
    time_path.unlink(missing_ok=True)
    return {
        "config": name,
        "exit": proc.returncode,
        "wall_s": round(wall, 3),
        "cpu_s": round((after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime), 3),
        "ffmpeg_max_rss_kb": _max_rss_kb(time_text),
        "stderr_tail": proc.stderr[-300:] if proc.returncode else "",
        "hardware": hw,
        "filter": vf or "decode_only",
        "command": argv,
    }


def _max_rss_kb(stderr: str) -> int | None:
    for line in stderr.splitlines():
        if "maximum resident set size" in line:
            try:
                return int(int(line.split()[0]) / 1024)
            except ValueError:
                return None
        if "peak memory footprint" in line:
            try:
                return int(int(line.split()[0]) / 1024)
            except ValueError:
                return None
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clip", required=True)
    args = ap.parse_args()
    clip = Path(args.clip)
    OUT.mkdir(parents=True, exist_ok=True)
    results = [run(name, clip, vf, False) for name, vf in FILTERS.items()]
    results.append(run("F_videotoolbox_full", clip, FILTERS["E_full"], True))
    (OUT / "scan_ab.json").write_text(
        json.dumps({"clip": clip.name, "results": results}, indent=2), encoding="utf-8"
    )
    for item in results:
        print(f"{item['config']:24} exit={item['exit']} wall={item['wall_s']:7.3f}s "
              f"cpu={item['cpu_s']:7.3f}s rss={item['ffmpeg_max_rss_kb']}kB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
