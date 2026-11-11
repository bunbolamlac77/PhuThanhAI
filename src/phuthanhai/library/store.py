"""Repository layer over the WAL registry.

All SQL lives here so the CLI, the API and tests share one meaning of an asset,
a revision or a note scope. Callers pass a :class:`sqlite3.Connection` inside a
``Database.read()`` / ``Database.write()`` block; this module never opens its own
transaction implicitly, so a scan or a multi-row update stays atomic.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable

from phuthanhai.library.db import natural_key


class StoreError(RuntimeError):
    """Domain error with a stable code, mapped to an HTTP status by the API."""

    def __init__(self, code: str, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class NotFound(StoreError):
    def __init__(self, message: str) -> None:
        super().__init__("NOT_FOUND", message, status=404)


class Conflict(StoreError):
    def __init__(self, message: str) -> None:
        super().__init__("ANNOTATION_CONFLICT", message, status=409)


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="milliseconds")


# --------------------------------------------------------------------------- #
# Projects                                                                     #
# --------------------------------------------------------------------------- #


def create_project(conn: sqlite3.Connection, *, name: str, root_path: str) -> dict[str, Any]:
    import uuid

    if not name.strip():
        raise StoreError("PROJECT_NAME_INVALID", "project name must not be empty")
    project_id = str(uuid.uuid4())
    now = utc_now_iso()
    conn.execute(
        "INSERT INTO projects(project_id, name, root_path, created_at, updated_at, revision) "
        "VALUES (?, ?, ?, ?, ?, 0)",
        (project_id, name.strip(), root_path, now, now),
    )
    return get_project(conn, project_id)


def get_project(conn: sqlite3.Connection, project_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM projects WHERE project_id = ?", (project_id,)).fetchone()
    if row is None:
        raise NotFound(f"project not found: {project_id}")
    return _project_row(row)


def list_projects(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM projects ORDER BY created_at DESC").fetchall()
    return [_project_row(row) for row in rows]


def find_project_by_root(conn: sqlite3.Connection, root_path: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM projects WHERE root_path = ?", (root_path,)).fetchone()
    return _project_row(row) if row else None


def touch_project(conn: sqlite3.Connection, project_id: str) -> None:
    conn.execute(
        "UPDATE projects SET updated_at = ?, revision = revision + 1 WHERE project_id = ?",
        (utc_now_iso(), project_id),
    )


def _project_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "project_id": row["project_id"],
        "name": row["name"],
        # Local-only: used to constrain scans and resolve assets. Never exported
        # into portable contracts; the API model drops it.
        "root_path": row["root_path"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "revision": row["revision"],
        "asset_count": row["asset_count"] if "asset_count" in row.keys() else None,
    }


# --------------------------------------------------------------------------- #
# Assets                                                                       #
# --------------------------------------------------------------------------- #


_ASSET_COLUMNS = (
    "asset_id",
    "project_id",
    "path",
    "filename",
    "sort_key",
    "size_bytes",
    "mtime_ns",
    "dev",
    "ino",
    "duration_ms",
    "width",
    "height",
    "rotation",
    "fps_nominal",
    "codec",
    "has_audio",
    "vfr_hint",
    "color_transfer",
    "preview_status",
    "hash_status",
    "analysis_status",
    "source_state",
    "sha256",
    "error_code",
    "sampling_status",
    "sampling_profile",
    "sampling_fingerprint",
    "sampled_at",
    "sampling_coverage",
    "analysis_id",
    "analysis_fingerprint",
    "analysis_coverage",
    "analysis_updated_at",
    "created_at",
    "updated_at",
)


def find_asset_by_path(conn: sqlite3.Connection, project_id: str, path: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM assets WHERE project_id = ? AND path = ?", (project_id, path)
    ).fetchone()
    return _asset_row(row) if row else None


def upsert_asset(
    conn: sqlite3.Connection,
    *,
    asset_id: str,
    project_id: str,
    path: str,
    filename: str,
    size_bytes: int,
    mtime_ns: int,
    dev: int | None,
    ino: int | None,
    duration_ms: int | None,
    width: int | None,
    height: int | None,
    rotation: int,
    fps_nominal: str | None,
    codec: str | None,
    has_audio: bool,
    vfr_hint: bool,
    color_transfer: str | None,
    source_state: str = "available",
    preview_status: str = "unprobed",
    error_code: str | None = None,
) -> None:
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO assets(
            asset_id, project_id, path, filename, sort_key, size_bytes, mtime_ns, dev, ino,
            duration_ms, width, height, rotation, fps_nominal, codec, has_audio, vfr_hint,
            color_transfer, preview_status, hash_status, analysis_status, source_state,
            sha256, error_code, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'none','not_started',?,NULL,?,?,?)
        ON CONFLICT(project_id, path) DO UPDATE SET
            size_bytes=excluded.size_bytes, mtime_ns=excluded.mtime_ns, dev=excluded.dev,
            ino=excluded.ino, duration_ms=excluded.duration_ms, width=excluded.width,
            height=excluded.height, rotation=excluded.rotation, fps_nominal=excluded.fps_nominal,
            codec=excluded.codec, has_audio=excluded.has_audio, vfr_hint=excluded.vfr_hint,
            color_transfer=excluded.color_transfer, preview_status=excluded.preview_status,
            source_state=excluded.source_state, error_code=excluded.error_code,
            updated_at=excluded.updated_at
        """,
        (
            asset_id,
            project_id,
            path,
            filename,
            natural_key(filename),
            size_bytes,
            mtime_ns,
            dev,
            ino,
            duration_ms,
            width,
            height,
            rotation,
            fps_nominal,
            codec,
            int(has_audio),
            int(vfr_hint),
            color_transfer,
            preview_status,
            source_state,
            error_code,
            now,
            now,
        ),
    )


def get_asset(conn: sqlite3.Connection, asset_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM assets WHERE asset_id = ?", (asset_id,)).fetchone()
    if row is None:
        raise NotFound(f"asset not found: {asset_id}")
    return _asset_row(row)


def set_asset_hash(conn: sqlite3.Connection, asset_id: str, sha256: str) -> None:
    conn.execute(
        "UPDATE assets SET sha256 = ?, hash_status = 'complete', updated_at = ? WHERE asset_id = ?",
        (sha256, utc_now_iso(), asset_id),
    )


def set_sampling_state(
    conn: sqlite3.Connection,
    asset_id: str,
    *,
    status: str,
    profile: str | None,
    fingerprint: str | None,
    sampled_at: str | None,
    coverage: str | None = None,
) -> None:
    conn.execute(
        "UPDATE assets SET sampling_status = ?, sampling_profile = ?, sampling_fingerprint = ?, "
        "sampled_at = ?, sampling_coverage = ?, updated_at = ? WHERE asset_id = ?",
        (status, profile, fingerprint, sampled_at, coverage, utc_now_iso(), asset_id),
    )


def set_analysis_state(
    conn: sqlite3.Connection,
    asset_id: str,
    *,
    status: str,
    analysis_id: str | None,
    fingerprint: str | None,
    coverage: str | None,
) -> None:
    conn.execute(
        "UPDATE assets SET analysis_status = ?, analysis_id = ?, analysis_fingerprint = ?, "
        "analysis_coverage = ?, analysis_updated_at = ?, updated_at = ? WHERE asset_id = ?",
        (status, analysis_id, fingerprint, coverage, utc_now_iso(), utc_now_iso(), asset_id),
    )


def set_source_state(conn: sqlite3.Connection, asset_id: str, state: str, error_code: str | None = None) -> None:
    conn.execute(
        "UPDATE assets SET source_state = ?, error_code = ?, updated_at = ? WHERE asset_id = ?",
        (state, error_code, utc_now_iso(), asset_id),
    )


def count_assets(conn: sqlite3.Connection, project_id: str) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM assets WHERE project_id = ?", (project_id,)).fetchone()
    return int(row["n"])


_SORTS = {
    "natural": "a.sort_key ASC, a.asset_id ASC",
    "natural_desc": "a.sort_key DESC, a.asset_id DESC",
    "duration": "COALESCE(a.duration_ms, 0) ASC, a.asset_id ASC",
    "duration_desc": "COALESCE(a.duration_ms, 0) DESC, a.asset_id DESC",
    "imported": "a.created_at ASC, a.asset_id ASC",
    "imported_desc": "a.created_at DESC, a.asset_id DESC",
}


def list_assets(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    q: str | None = None,
    source_state: str | None = None,
    analysis_status: str | None = None,
    favorite: bool | None = None,
    min_rating: int | None = None,
    has_note: bool | None = None,
    label: str | None = None,
    sort: str = "natural",
    cursor: tuple[str, str] | None = None,
    limit: int = 100,
) -> tuple[list[dict[str, Any]], str | None, int]:
    """Keyset-paginated asset list with filters and note search.

    Returns ``(rows, next_cursor_token, total_matching)``. The cursor is the last
    row's ``(sort_key, asset_id)`` pair, so deep pages never scan from the start.
    """

    if sort not in _SORTS:
        raise StoreError("SORT_INVALID", f"unsupported sort: {sort}")
    limit = max(1, min(limit, 500))
    order = _SORTS[sort]

    where = ["a.project_id = ?"]
    params: list[Any] = [project_id]

    if q:
        needle = f"%{q.strip()}%"
        where.append(
            "(a.filename LIKE ? OR EXISTS ("
            "SELECT 1 FROM annotations n WHERE n.asset_id = a.asset_id AND n.text LIKE ?))"
        )
        params.extend([needle, needle])
    if source_state:
        where.append("a.source_state = ?")
        params.append(source_state)
    if analysis_status:
        where.append("a.analysis_status = ?")
        params.append(analysis_status)
    if favorite is not None:
        where.append("COALESCE(r.favorite, 0) = ?")
        params.append(int(favorite))
    if min_rating is not None:
        where.append("COALESCE(r.rating, 0) >= ?")
        params.append(int(min_rating))
    if has_note is not None:
        exists = "EXISTS (SELECT 1 FROM annotations n WHERE n.asset_id = a.asset_id)"
        where.append(exists if has_note else f"NOT {exists}")
    if label:
        where.append("EXISTS (SELECT 1 FROM json_each(COALESCE(r.labels, '[]')) WHERE json_each.value = ?)")
        params.append(label)

    base_where = " AND ".join(where)
    count_row = conn.execute(
        f"SELECT COUNT(*) AS n FROM assets a "
        f"LEFT JOIN review_overlays r ON r.asset_id = a.asset_id WHERE {base_where}",
        params,
    ).fetchone()
    total = int(count_row["n"])

    page_where = list(where)
    page_params = list(params)
    descending = order.endswith("DESC")
    if cursor is not None and sort in {"natural", "natural_desc"}:
        operator = "<" if descending else ">"
        page_where.append(f"(a.sort_key {operator} ? OR (a.sort_key = ? AND a.asset_id {operator} ?))")
        page_params.extend([cursor[0], cursor[0], cursor[1]])

    sql = (
        "SELECT a.*, r.status AS review_status, r.rating AS review_rating, r.favorite AS review_favorite, "
        "r.labels AS review_labels, r.selected_ranges AS review_ranges, "
        "r.last_playhead_ms AS review_playhead, r.revision AS review_revision, "
        "(SELECT COUNT(*) FROM annotations n WHERE n.asset_id = a.asset_id) AS note_count "
        "FROM assets a LEFT JOIN review_overlays r ON r.asset_id = a.asset_id "
        f"WHERE {' AND '.join(page_where)} ORDER BY {order} LIMIT ?"
    )
    page_params.append(limit + 1)
    rows = conn.execute(sql, page_params).fetchall()

    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor: str | None = None
    if has_more and rows:
        import base64

        last = rows[-1]
        token = json.dumps([last["sort_key"], last["asset_id"]]).encode()
        next_cursor = base64.urlsafe_b64encode(token).decode()
    return [_asset_row(row, with_review=True) for row in rows], next_cursor, total


def decode_cursor(token: str) -> tuple[str, str]:
    import base64

    try:
        payload = json.loads(base64.urlsafe_b64decode(token.encode()).decode())
        return str(payload[0]), str(payload[1])
    except Exception as exc:  # noqa: BLE001
        raise StoreError("CURSOR_INVALID", "pagination cursor is invalid") from exc


def _asset_row(row: sqlite3.Row, *, with_review: bool = False) -> dict[str, Any]:
    data: dict[str, Any] = {key: row[key] for key in _ASSET_COLUMNS}
    data["has_audio"] = bool(data["has_audio"])
    data["vfr_hint"] = bool(data["vfr_hint"])
    data["has_sha256"] = data["sha256"] is not None
    data.pop("sha256", None)
    if with_review:
        data["review"] = {
            "status": row["review_status"] or "unreviewed",
            "rating": row["review_rating"],
            "favorite": bool(row["review_favorite"] or 0),
            "labels": json.loads(row["review_labels"] or "[]"),
            "selected_ranges": json.loads(row["review_ranges"] or "[]"),
            "last_playhead_ms": row["review_playhead"],
            "revision": row["review_revision"] or 1,
        }
        data["note_count"] = int(row["note_count"] or 0)
    return data


# --------------------------------------------------------------------------- #
# Annotations                                                                  #
# --------------------------------------------------------------------------- #


def _annotation_row(row: sqlite3.Row) -> dict[str, Any]:
    wall = None
    if row["wall_time_of_day"]:
        wall = {
            "time_of_day": row["wall_time_of_day"],
            "date": row["wall_date"],
            "timezone": row["wall_timezone"],
            "precision": row["wall_precision"] or "time_of_day",
        }
    return {
        "annotation_id": row["annotation_id"],
        "project_id": row["project_id"],
        "asset_id": row["asset_id"],
        "scope": row["scope"],
        "start_ms": row["start_ms"],
        "end_ms": row["end_ms"],
        "text": row["text"],
        "note_type": row["note_type"],
        "source": row["source"],
        "assertion_status": row["assertion_status"],
        "wall_clock": wall,
        "revision": row["revision"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def create_annotation(
    conn: sqlite3.Connection,
    *,
    annotation_id: str,
    project_id: str,
    asset_id: str | None,
    scope: str,
    start_ms: int | None,
    end_ms: int | None,
    text: str,
    note_type: str = "context",
    wall_clock: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = utc_now_iso()
    wall = wall_clock or {}
    conn.execute(
        """
        INSERT INTO annotations(
            annotation_id, project_id, asset_id, scope, start_ms, end_ms, text, note_type,
            source, assertion_status, wall_time_of_day, wall_date, wall_timezone, wall_precision,
            revision, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,'user_annotation','user_asserted',?,?,?,?,1,?,?)
        """,
        (
            annotation_id,
            project_id,
            asset_id,
            scope,
            start_ms,
            end_ms,
            text,
            note_type,
            wall.get("time_of_day"),
            wall.get("date"),
            wall.get("timezone"),
            wall.get("precision"),
            now,
            now,
        ),
    )
    return get_annotation(conn, annotation_id)


def get_annotation(conn: sqlite3.Connection, annotation_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM annotations WHERE annotation_id = ?", (annotation_id,)).fetchone()
    if row is None:
        raise NotFound(f"annotation not found: {annotation_id}")
    return _annotation_row(row)


def list_annotations(
    conn: sqlite3.Connection, project_id: str, *, asset_id: str | None = None
) -> list[dict[str, Any]]:
    if asset_id:
        rows = conn.execute(
            "SELECT * FROM annotations WHERE project_id = ? AND (asset_id = ? OR asset_id IS NULL) "
            "ORDER BY updated_at DESC",
            (project_id, asset_id),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM annotations WHERE project_id = ? ORDER BY updated_at DESC",
            (project_id,),
        ).fetchall()
    return [_annotation_row(row) for row in rows]


def update_annotation(
    conn: sqlite3.Connection,
    *,
    annotation_id: str,
    expected_revision: int,
    text: str,
    note_type: str,
    start_ms: int | None,
    end_ms: int | None,
    wall_clock: dict[str, Any] | None,
) -> dict[str, Any]:
    wall = wall_clock or {}
    cursor = conn.execute(
        "UPDATE annotations SET text = ?, note_type = ?, start_ms = ?, end_ms = ?, "
        "wall_time_of_day = ?, wall_date = ?, wall_timezone = ?, wall_precision = ?, "
        "revision = revision + 1, updated_at = ? "
        "WHERE annotation_id = ? AND revision = ?",
        (
            text,
            note_type,
            start_ms,
            end_ms,
            wall.get("time_of_day"),
            wall.get("date"),
            wall.get("timezone"),
            wall.get("precision"),
            utc_now_iso(),
            annotation_id,
            expected_revision,
        ),
    )
    if cursor.rowcount == 0:
        existing = conn.execute(
            "SELECT revision FROM annotations WHERE annotation_id = ?", (annotation_id,)
        ).fetchone()
        if existing is None:
            raise NotFound(f"annotation not found: {annotation_id}")
        raise Conflict(
            f"annotation revision {expected_revision} is stale; current revision is {existing['revision']}"
        )
    return get_annotation(conn, annotation_id)


def delete_annotation(conn: sqlite3.Connection, annotation_id: str, *, expected_revision: int) -> None:
    cursor = conn.execute(
        "DELETE FROM annotations WHERE annotation_id = ? AND revision = ?",
        (annotation_id, expected_revision),
    )
    if cursor.rowcount == 0:
        existing = conn.execute(
            "SELECT revision FROM annotations WHERE annotation_id = ?", (annotation_id,)
        ).fetchone()
        if existing is None:
            raise NotFound(f"annotation not found: {annotation_id}")
        raise Conflict("annotation revision is stale")


# --------------------------------------------------------------------------- #
# Reviews                                                                      #
# --------------------------------------------------------------------------- #


def get_review(conn: sqlite3.Connection, asset_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM review_overlays WHERE asset_id = ?", (asset_id,)).fetchone()
    if row is None:
        return {
            "asset_id": asset_id,
            "status": "unreviewed",
            "rating": None,
            "favorite": False,
            "labels": [],
            "selected_ranges": [],
            "last_playhead_ms": None,
            "revision": 1,
        }
    return {
        "asset_id": row["asset_id"],
        "status": row["status"],
        "rating": row["rating"],
        "favorite": bool(row["favorite"]),
        "labels": json.loads(row["labels"] or "[]"),
        "selected_ranges": json.loads(row["selected_ranges"] or "[]"),
        "last_playhead_ms": row["last_playhead_ms"],
        "revision": row["revision"],
    }


def upsert_review(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    asset_id: str,
    expected_revision: int | None,
    status: str,
    rating: int | None,
    favorite: bool,
    labels: Iterable[str],
    selected_ranges: Iterable[dict[str, int]],
    last_playhead_ms: int | None,
) -> dict[str, Any]:
    current = get_review(conn, asset_id)
    if expected_revision is not None and expected_revision != current["revision"]:
        raise Conflict(
            f"review revision {expected_revision} is stale; current revision is {current['revision']}"
        )
    now = utc_now_iso()
    if current["revision"] == 1 and not _review_exists(conn, asset_id):
        conn.execute(
            "INSERT INTO review_overlays(asset_id, project_id, status, rating, favorite, labels, "
            "selected_ranges, last_playhead_ms, revision, updated_at) VALUES (?,?,?,?,?,?,?,?,1,?)",
            (
                asset_id,
                project_id,
                status,
                rating,
                int(favorite),
                json.dumps(list(labels)),
                json.dumps(list(selected_ranges)),
                last_playhead_ms,
                now,
            ),
        )
    else:
        conn.execute(
            "UPDATE review_overlays SET status=?, rating=?, favorite=?, labels=?, selected_ranges=?, "
            "last_playhead_ms=?, revision=revision+1, updated_at=? WHERE asset_id=?",
            (
                status,
                rating,
                int(favorite),
                json.dumps(list(labels)),
                json.dumps(list(selected_ranges)),
                last_playhead_ms,
                now,
                asset_id,
            ),
        )
    return get_review(conn, asset_id)


def _review_exists(conn: sqlite3.Connection, asset_id: str) -> bool:
    return (
        conn.execute("SELECT 1 FROM review_overlays WHERE asset_id = ?", (asset_id,)).fetchone()
        is not None
    )


# --------------------------------------------------------------------------- #
# Jobs                                                                         #
# --------------------------------------------------------------------------- #


def create_job(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    project_id: str,
    asset_id: str | None,
    kind: str,
) -> dict[str, Any]:
    now = utc_now_iso()
    conn.execute(
        "INSERT INTO jobs(job_id, project_id, asset_id, kind, state, progress, error_code, "
        "created_at, updated_at) VALUES (?,?,?,?,'queued',0,NULL,?,?)",
        (job_id, project_id, asset_id, kind, now, now),
    )
    return get_job(conn, job_id)


def update_job(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    state: str | None = None,
    progress: float | None = None,
    error_code: str | None = None,
) -> dict[str, Any]:
    job = get_job(conn, job_id)
    conn.execute(
        "UPDATE jobs SET state = ?, progress = ?, error_code = ?, updated_at = ? WHERE job_id = ?",
        (
            state if state is not None else job["state"],
            progress if progress is not None else job["progress"],
            error_code if error_code is not None else job["error_code"],
            utc_now_iso(),
            job_id,
        ),
    )
    return get_job(conn, job_id)


def get_job(conn: sqlite3.Connection, job_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    if row is None:
        raise NotFound(f"job not found: {job_id}")
    return dict(row)


def list_jobs(conn: sqlite3.Connection, project_id: str, limit: int = 100) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM jobs WHERE project_id = ? ORDER BY created_at DESC LIMIT ?",
        (project_id, max(1, min(limit, 500))),
    ).fetchall()
    return [dict(row) for row in rows]


# --------------------------------------------------------------------------- #
# Cache bookkeeping                                                            #
# --------------------------------------------------------------------------- #


def record_cache_entry(
    conn: sqlite3.Connection, *, path: str, kind: str, size_bytes: int, atime: int, pinned: bool = False
) -> None:
    conn.execute(
        "INSERT INTO cache_entries(path, kind, size_bytes, atime, pinned, created_at) "
        "VALUES (?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET size_bytes=excluded.size_bytes, "
        "atime=excluded.atime, pinned=excluded.pinned",
        (path, kind, size_bytes, atime, int(pinned), utc_now_iso()),
    )


def touch_cache_entry(conn: sqlite3.Connection, path: str, atime: int) -> None:
    conn.execute("UPDATE cache_entries SET atime = ? WHERE path = ?", (atime, path))


def list_cache_entries(conn: sqlite3.Connection, kind: str | None = None) -> list[dict[str, Any]]:
    if kind:
        rows = conn.execute(
            "SELECT * FROM cache_entries WHERE kind = ? ORDER BY atime ASC", (kind,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM cache_entries ORDER BY atime ASC").fetchall()
    return [dict(row) for row in rows]


def delete_cache_entry(conn: sqlite3.Connection, path: str) -> None:
    conn.execute("DELETE FROM cache_entries WHERE path = ?", (path,))


def cache_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT kind, COUNT(*) AS entries, COALESCE(SUM(size_bytes), 0) AS bytes "
        "FROM cache_entries GROUP BY kind"
    ).fetchall()
    return {row["kind"]: {"entries": row["entries"], "bytes": row["bytes"]} for row in rows}


# --------------------------------------------------------------------------- #
# Settings                                                                     #
# --------------------------------------------------------------------------- #


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key, value, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, value, utc_now_iso()),
    )


def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default
