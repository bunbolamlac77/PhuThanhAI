#!/usr/bin/env python3
"""S05.1: 5,000-asset backend benchmark + virtualized DOM proof."""

from __future__ import annotations

import json
import os
import resource
import statistics
import sys
import tempfile
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "artifacts" / "qa" / "S05"
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "tools"))

from phuthanhai.config import Settings  # noqa: E402
from phuthanhai.library import store  # noqa: E402
from phuthanhai.library.db import Database  # noqa: E402
from phuthanhai.library.service import LibraryService  # noqa: E402

COUNT = 5000


def pct(values: list[float], p: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, int(round((p / 100) * (len(ordered) - 1)))))
    return round(ordered[index], 2)


def backend_bench() -> dict:
    root = Path(tempfile.mkdtemp(prefix="pth_bench5000_"))
    os.environ["PHUTHANHAI_DATA_DIR"] = str(root / "data")
    settings = Settings.load()
    settings.data.ensure()
    database = Database(settings.data.registry_db)
    database.migrate()
    with database.write() as conn:
        project = store.create_project(conn, name="Bench 5000", root_path=str(root / "footage"))
        started = time.perf_counter()
        for index in range(1, COUNT + 1):
            filename = f"C_{index:05d}.MOV"
            store.upsert_asset(
                conn,
                asset_id=str(uuid.uuid4()),
                project_id=project["project_id"],
                path=f"/bench/{filename}",
                filename=filename,
                size_bytes=1000 + index,
                mtime_ns=index,
                dev=None, ino=None,
                duration_ms=10_000 + index,
                width=1920, height=1080, rotation=0,
                fps_nominal="25/1", codec="h264", has_audio=True,
                vfr_hint=False, color_transfer="bt709",
            )
        insert_s = time.perf_counter() - started
    project_id = project["project_id"]

    from fastapi.testclient import TestClient
    from phuthanhai.api.app import create_app

    app = create_app(settings, port=19055, capability="bench")
    list_ms: list[float] = []
    search_ms: list[float] = []
    filter_ms: list[float] = []
    with TestClient(app, base_url="http://127.0.0.1:19055") as client:
        for _ in range(60):
            t0 = time.perf_counter()
            response = client.get("/api/v1/assets", params={"project_id": project_id, "limit": 100})
            list_ms.append((time.perf_counter() - t0) * 1000)
            assert response.status_code == 200
        for _ in range(40):
            t0 = time.perf_counter()
            response = client.get("/api/v1/assets", params={"project_id": project_id, "q": "C_04", "limit": 100})
            search_ms.append((time.perf_counter() - t0) * 1000)
            assert response.status_code == 200
        for _ in range(40):
            t0 = time.perf_counter()
            response = client.get("/api/v1/assets", params={"project_id": project_id, "favorite_only": "true", "limit": 100})
            filter_ms.append((time.perf_counter() - t0) * 1000)
            assert response.status_code == 200
        total = client.get("/api/v1/assets", params={"project_id": project_id, "limit": 1}).json()["total"]

    # Restart: reopen the same database and verify count + paging cursor integrity.
    database.close_current_thread()
    service = LibraryService(Settings.load())
    try:
        page = service.list_assets(project_id, limit=100)
        restored = page["total"]
        integrity = len({item["asset_id"] for item in page["items"]}) == len(page["items"])
    finally:
        service.close()
    main_bytes = settings.data.registry_db.stat().st_size
    wal = Path(str(settings.data.registry_db) + "-wal")
    size_bytes = main_bytes + (wal.stat().st_size if wal.is_file() else 0)
    return {
        "count": COUNT,
        "project_id": project_id,
        "insert_seconds": round(insert_s, 3),
        "total_api": total,
        "restart_total": restored,
        "restart_integrity": integrity,
        "list_p50_ms": pct(list_ms, 50), "list_p95_ms": pct(list_ms, 95),
        "search_p50_ms": pct(search_ms, 50), "search_p95_ms": pct(search_ms, 95),
        "filter_p50_ms": pct(filter_ms, 50), "filter_p95_ms": pct(filter_ms, 95),
        "db_bytes": size_bytes,
        "peak_rss_bytes": int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss),
        "passed": total == COUNT and restored == COUNT and integrity,
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    backend = backend_bench()
    result: dict = {"backend": backend}
    # Virtualized DOM with the same 5,000 rows (hardlinked 1s test video).
    from run_s01_e2e import CHROME, pick_port, run_case, seed_bench

    if CHROME.is_file():
        case = run_case("bench5000", lambda root: seed_bench(root, COUNT), port=pick_port())
        result["browser"] = {k: case.get(k) for k in ("asset_card_nodes", "total_label", "playback_ok", "has_four_tabs", "error_shown", "screenshot_bytes")}
        result["browser"]["passed"] = (
            case.get("asset_card_nodes", 0) > 0
            and case.get("asset_card_nodes", 0) < 120
            and case.get("total_label") == str(COUNT)
            and case.get("playback_ok") is True
            and not case.get("error_shown")
        )
    else:
        result["browser"] = {"passed": None, "skipped": "Chrome missing"}
    result["passed"] = backend["passed"] and result["browser"].get("passed") is True
    (OUT / "bench5000.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
