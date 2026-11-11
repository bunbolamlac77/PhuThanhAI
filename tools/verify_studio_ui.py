#!/usr/bin/env python3
"""Verify the S05.3 Studio layout markers without rewriting business logic."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECKS = {
    "web/src/components/InspectorTabs.tsx": ["role=\"tablist\"", "data-inspector-tab", "hidden={active"],
    "web/src/components/AssetGrid.tsx": ["asset-card__fallback", "onError={() => setState(\"error\")}", "Không có ảnh xem trước"],
    "web/src/tabs/LibraryTab.tsx": ["studio-workspace", "InspectorTabs", "studio-filter-details", "button, [role=\"tab\"], summary"],
    "web/src/App.tsx": ["studio-topbar", "studio-footer", "Universal Video Analyzer"],
    "web/src/main.tsx": ["studio-ui.css"],
    "web/src/studio-ui.css": ["grid-template-columns", "prefers-reduced-motion", "studio-inspector"],
}


def main() -> int:
    failures = []
    for relative, needles in CHECKS.items():
        path = ROOT / relative
        if not path.is_file():
            failures.append(f"missing {relative}")
            continue
        text = path.read_text(encoding="utf-8")
        for needle in needles:
            if needle not in text:
                failures.append(f"{relative} missing {needle!r}")
    app = (ROOT / "web/src/App.tsx").read_text(encoding="utf-8")
    for stale in ("S01 ·", "S02–S03", "chưa triển khai."):
        if stale in app:
            failures.append(f"stale footer text in App.tsx: {stale!r}")
    if failures:
        print("\n".join(failures))
        return 1
    print("Studio UI markers OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
