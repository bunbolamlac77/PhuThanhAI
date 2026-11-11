#!/usr/bin/env python3
"""Apply a downloaded labels.json to the golden manifest.

Requires a non-empty reviewer name; marks `owner_confirmed` only after a human
runs this tool. Refuses to mark rows that were never rendered/known.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

MANIFEST = Path(__file__).resolve().parents[1] / "artifacts" / "qa" / "S05" / "golden" / "manifest.json"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--labels", required=True)
    parser.add_argument("--confirm-by", required=True)
    args = parser.parse_args()
    if not args.confirm_by.strip():
        print("--confirm-by must name the human reviewer", file=sys.stderr)
        return 2
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    labels = json.loads(Path(args.labels).read_text(encoding="utf-8"))
    if labels.get("reviewer") != args.confirm_by.strip():
        print("reviewer mismatch between labels.json and --confirm-by", file=sys.stderr)
        return 2
    rows = {row["row_id"]: row for row in manifest["rows"]}
    applied = 0
    for supplied in labels.get("rows", []):
        row = rows.get(supplied.get("row_id"))
        if row is None:
            continue
        if supplied.get("review_status") != "confirmed" or not supplied.get("reviewer_label"):
            continue
        row["reviewer_label"] = supplied["reviewer_label"]
        row["label_polarity"] = supplied.get("label_polarity", "unknown")
        row["reviewed_by"] = args.confirm_by.strip()
        row["review_status"] = "confirmed"
        row["owner_confirmed"] = True
        applied += 1
    manifest["reviewer_confirmed"] = sum(1 for row in manifest["rows"] if row.get("owner_confirmed"))
    MANIFEST.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"applied {applied}; confirmed={manifest['reviewer_confirmed']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
