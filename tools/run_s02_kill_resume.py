#!/usr/bin/env python3
"""Hard-kill / resume evidence for S02 (TASK 6).

Stages exercised:

1. SIGKILL after N extracted frames -> checkpoint must hold only fully committed
   frames; the next process resumes and completes without duplicates.
2. SIGKILL shortly after start (during timeline/scan) -> no checkpoint; resume
   still completes.

Prints JSON evidence and exits non-zero when an invariant is violated.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
VENV_PY = REPO / ".venv" / "bin" / "python"
CHILD = REPO / "tools" / "s02_kill_child.py"
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

from phuthanhai.config import Settings  # noqa: E402
from phuthanhai.library.service import LibraryService  # noqa: E402
from phuthanhai.runtime.checkpoint import read_checkpoint  # noqa: E402
from tests.media_fixtures import get  # noqa: E402


def run_child(env: dict, *, kill_after: int = 0, kill_delay: float = 0.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            str(VENV_PY), str(CHILD),
            "--filename", "clip.mp4",
            "--profile", "thorough",
            "--kill-after-frames", str(kill_after),
            "--kill-delay", str(kill_delay),
        ],
        capture_output=True,
        text=True,
        timeout=300,
        env=env,
        check=False,
    )


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="pth_kill_"))
    data_root = root / "data"
    footage = root / "footage"
    footage.mkdir(parents=True)
    shutil.copy(get("long_nocut"), footage / "clip.mp4")
    env = {**os.environ, "PHUTHANHAI_DATA_DIR": str(data_root)}
    os.environ["PHUTHANHAI_DATA_DIR"] = str(data_root)

    service = LibraryService(Settings.load())
    try:
        project = service.create_project("kill", str(footage))
        service.scan(project["project_id"])
    finally:
        service.close()

    evidence: dict = {"stages": []}
    try:
        # Stage 1: hard kill after 6 extracted frames (checkpoint every 5).
        killed = run_child(env, kill_after=6)
        settings = Settings.load()
        asset_root = settings.data.sampling
        checkpoints = list(asset_root.rglob("checkpoint.json"))
        committed = 0
        if checkpoints:
            checkpoint = read_checkpoint(checkpoints[0])
            committed = len(checkpoint.committed_frame_ids)
        stage1 = {
            "stage": "kill_during_extraction",
            "child_returncode": killed.returncode,
            "child_signalled": killed.returncode < 0,
            "checkpoint_files": len(checkpoints),
            "committed_frames_at_kill": committed,
        }

        resumed = run_child(env)
        parsed: dict = {}
        if resumed.returncode == 0 and resumed.stdout.strip():
            parsed = json.loads(resumed.stdout.strip().splitlines()[-1])
        manifest_paths = list(asset_root.rglob("manifest.json"))
        manifest = json.loads(manifest_paths[0].read_text(encoding="utf-8")) if manifest_paths else {}
        frames = manifest.get("frames", [])
        ids = [frame["frame_id"] for frame in frames]
        files_ok = all(Path(frame["path"]).is_file() for frame in frames)
        stage1.update(
            {
                "resume_exit": resumed.returncode,
                "resume_extracted": parsed.get("extracted"),
                "resume_cache_hits": parsed.get("cache_hits"),
                "final_frames": len(frames),
                "unique_ids": len(set(ids)) == len(ids),
                "all_images_present": files_ok,
                "checkpoint_stage": read_checkpoint(checkpoints[0]).stage if checkpoints else None,
            }
        )
        stage1["passed"] = (
            stage1["child_signalled"]
            and committed >= 5
            and resumed.returncode == 0
            and stage1["unique_ids"]
            and stage1["all_images_present"]
            and len(frames) > committed
        )
        evidence["stages"].append(stage1)

        # Stage 2: kill during timeline/scan (no checkpoint yet), then resume.
        asset_root_2 = root / "data2"
        footage2 = root / "footage2"
        footage2.mkdir()
        shutil.copy(get("cuts"), footage2 / "clip.mp4")
        env2 = {**os.environ, "PHUTHANHAI_DATA_DIR": str(asset_root_2)}
        os.environ["PHUTHANHAI_DATA_DIR"] = str(asset_root_2)
        service2 = LibraryService(Settings.load())
        try:
            project2 = service2.create_project("kill2", str(footage2))
            service2.scan(project2["project_id"])
        finally:
            service2.close()
        killed2 = run_child(env2, kill_delay=0.25)
        resumed2 = run_child(env2)
        manifest2 = list((asset_root_2 / "caches" / "sampling").rglob("manifest.json"))
        frames2 = json.loads(manifest2[0].read_text(encoding="utf-8"))["frames"] if manifest2 else []
        stage2 = {
            "stage": "kill_during_scan",
            "child_returncode": killed2.returncode,
            "child_signalled": killed2.returncode < 0,
            "resume_exit": resumed2.returncode,
            "final_frames": len(frames2),
            "unique_ids": len({f["frame_id"] for f in frames2}) == len(frames2),
        }
        stage2["passed"] = stage2["child_signalled"] and resumed2.returncode == 0 and len(frames2) > 0 and stage2["unique_ids"]
        evidence["stages"].append(stage2)

        evidence["passed"] = all(stage["passed"] for stage in evidence["stages"])
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print(json.dumps(evidence, indent=2))
    return 0 if evidence.get("passed") else 1


if __name__ == "__main__":
    sys.exit(main())
