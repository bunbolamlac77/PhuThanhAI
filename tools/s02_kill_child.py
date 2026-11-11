#!/usr/bin/env python3
"""Child process for the S02 hard-kill test.

Runs a sampling job and kills itself with SIGKILL either after N extracted frames
or after a delay (to hit the scan stage). The parent then resumes from checkpoint.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from phuthanhai.config import Settings  # noqa: E402
from phuthanhai.library.service import LibraryService  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--filename", required=True)
    parser.add_argument("--profile", default="thorough")
    parser.add_argument("--kill-after-frames", type=int, default=0)
    parser.add_argument("--kill-delay", type=float, default=0.0)
    args = parser.parse_args()

    if args.kill_delay > 0:
        threading.Timer(args.kill_delay, lambda: os.kill(os.getpid(), signal.SIGKILL)).start()

    service = LibraryService(Settings.load())
    try:
        project = service.list_projects()[0]
        assets = service.list_assets(project["project_id"], limit=100)["items"]
        asset = next(item for item in assets if item["filename"] == args.filename)

        def progress(payload: dict) -> None:
            if (
                args.kill_after_frames > 0
                and payload.get("stage") == "extracting"
                and payload.get("done", 0) >= args.kill_after_frames
            ):
                os.kill(os.getpid(), signal.SIGKILL)

        outcome = service.pipeline.run(
            asset,
            Path(asset["path"]),
            profile=args.profile,
            progress=progress,
        )
        print(
            json.dumps(
                {
                    "status": outcome.status,
                    "frames": len(outcome.frames),
                    "extracted": outcome.extracted,
                    "cache_hits": outcome.cache_hits,
                    "cache_dir": str(outcome.cache_dir),
                    "fingerprint": outcome.fingerprint,
                }
            )
        )
        return 0
    finally:
        service.close()


if __name__ == "__main__":
    sys.exit(main())
